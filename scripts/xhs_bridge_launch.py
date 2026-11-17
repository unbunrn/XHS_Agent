"""Launch the local Xiaohongshu bridge as a detached, windowless process.

This private helper is called by scripts/start-xhs.ps1.  It mirrors
scripts/launch.py, but for the community Xiaohongshu bridge binary:

  * the token is loaded through bot.config.load_config and passed to the
    bridge only through the child environment as AUTH_TOKEN;
  * the exact v2.7.2 executable hash is verified before execution;
  * stdout/stderr are appended to the bridge log directory;
  * the bridge is forced to listen on 127.0.0.1;
  * a PID file and an identity JSON (including the real process creation
    time) are written for stop/health verification;
  * if the bridge exits immediately, the partial logs are shown and the state
    files are removed so a later start is not blocked by a dead PID.

Stdlib only.  It never publishes or logs in, and only probes the loopback
port before launching; it does not contact external services.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

EXPECTED_SHA256 = "1f62e1fe137d0293595197b3a76e280e4114293b425e9c4082a334d1f7fb5316"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _tail(path: Path, lines: int = 30) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _write_text_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def _write_identity(path: Path, payload: dict) -> None:
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _remove_state(pid_file: Path, identity_file: Path) -> None:
    for path in (pid_file, identity_file):
        try:
            path.unlink()
        except OSError:
            pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _process_creation_time_ticks(process_id: int) -> int | None:
    """Return the Windows process creation time as FILETIME ticks (100 ns)."""
    if os.name != "nt":
        return None

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(process_id))
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return ticks if ticks > 0 else None
    finally:
        kernel32.CloseHandle(handle)


def _load_token(project_root: Path) -> str | None:
    """Load the bridge token through the project's own config loader."""
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    # Imported lazily so the helper stays importable during isolated tests.
    from bot.config import load_config

    config_path = project_root / "config" / "bot.toml"
    config = load_config(config_path, root=project_root)
    token = config.secrets.xiaohongshu_token
    if token and token.strip():
        return token.strip()
    return None


def _port_in_use(port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True, help="bridge executable")
    parser.add_argument("--project-root", required=True, help="bot project root")
    parser.add_argument("--bridge-root", required=True, help="working/state directory")
    parser.add_argument("--pid-file", required=True)
    parser.add_argument("--identity-file", required=True)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--version", default="")
    parser.add_argument("--show-browser", action="store_true")
    args = parser.parse_args(argv)

    if not (1 <= args.port <= 65535):
        print("port must be between 1 and 65535", file=sys.stderr)
        return 2

    exe = Path(args.exe)
    if not exe.is_absolute():
        exe = (Path.cwd() / exe).resolve()
    if not exe.is_file():
        print(f"bridge executable not found: {exe}", file=sys.stderr)
        return 2

    actual_sha256 = _sha256_file(exe)
    if actual_sha256.lower() != EXPECTED_SHA256:
        print(
            "bridge executable SHA256 mismatch: "
            f"expected {EXPECTED_SHA256}, got {actual_sha256}; refusing to execute",
            file=sys.stderr,
        )
        return 2

    bridge_root = Path(args.bridge_root)
    if not bridge_root.is_absolute():
        bridge_root = (Path.cwd() / bridge_root).resolve()
    bridge_root.mkdir(parents=True, exist_ok=True)

    project_root = Path(args.project_root)
    if not project_root.is_absolute():
        project_root = (Path.cwd() / project_root).resolve()
    if not project_root.is_dir():
        print(f"project root not found: {project_root}", file=sys.stderr)
        return 2
    project_root = project_root.resolve()

    pid_file = Path(args.pid_file)
    if not pid_file.is_absolute():
        pid_file = (Path.cwd() / pid_file).resolve()
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    identity_file = Path(args.identity_file)
    if not identity_file.is_absolute():
        identity_file = (Path.cwd() / identity_file).resolve()
    identity_file.parent.mkdir(parents=True, exist_ok=True)

    log_dir = Path(args.log_dir)
    if not log_dir.is_absolute():
        log_dir = (Path.cwd() / log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    token = _load_token(project_root)
    if not token:
        print(
            "bridge token is missing; set XHS_MCP_AUTH_TOKEN / XHS_AUTH_TOKEN "
            "or xiaohongshu_token in config/secrets.toml (loaded through "
            "bot.config.load_config). Refusing to start an unauthenticated bridge.",
            file=sys.stderr,
        )
        return 2

    # Child-local state must stay under the project, including cookies and
    # temporary files.  These paths are fixed under var/xhs-bridge.
    state_dir = project_root / "var" / "xhs-bridge"
    cookies_path = state_dir / "cookies.json"
    cache_dir = state_dir / "cache"
    tmp_dir = state_dir / "tmp"
    for child_path in (state_dir, cookies_path, cache_dir, tmp_dir):
        if not _is_within(child_path, project_root):
            print(f"refusing child path outside project: {child_path}", file=sys.stderr)
            return 2
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Refuse a duplicate bridge rather than letting two browsers fight over
    # the same cookie/cache directory and port.
    if _port_in_use(args.port):
        print(
            f"port {args.port} is already in use; refusing to start a duplicate bridge",
            file=sys.stderr,
        )
        return 3

    out_path = log_dir / "bridge.out.log"
    err_path = log_dir / "bridge.err.log"
    out = open(out_path, "ab", buffering=0)
    err = open(err_path, "ab", buffering=0)

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

    env = dict(os.environ)
    # Only the explicitly selected token may reach the bridge.  The token is
    # never placed on the command line or written to the identity/log files.
    for name in ("XHS_MCP_AUTH_TOKEN", "XHS_AUTH_TOKEN", "AUTH_TOKEN", "XHS_BRIDGE_TOKEN"):
        env.pop(name, None)
    env["AUTH_TOKEN"] = token
    env["COOKIES_PATH"] = str(cookies_path)
    env["LOCALAPPDATA"] = str(cache_dir)
    env["TEMP"] = str(tmp_dir)
    env["TMP"] = str(tmp_dir)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")

    # http.Server.Addr receives this string directly, so the host must be part
    # of the argument.  A bare ":18060" would listen on all interfaces.
    port_argument = f"127.0.0.1:{args.port}"
    command = [
        str(exe),
        "-port",
        port_argument,
        "-headless=" + ("false" if args.show_browser else "true"),
    ]

    try:
        process = subprocess.Popen(
            command,
            cwd=str(bridge_root),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=creationflags,
            close_fds=True,
            env=env,
        )
    except OSError as exc:
        print(f"failed to launch xhs bridge: {exc}", file=sys.stderr)
        return 1
    finally:
        out.close()
        err.close()

    creation_ticks = _process_creation_time_ticks(process.pid)
    if not creation_ticks:
        print(
            "failed to read the bridge process creation time; "
            "refusing to record an unverifiable identity",
            file=sys.stderr,
        )
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        _remove_state(pid_file, identity_file)
        return 1
    creation_time = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(
        microseconds=creation_ticks // 10
    )

    try:
        _write_text_atomic(pid_file, str(process.pid), encoding="ascii")
        _write_identity(
            identity_file,
            {
                "name": "xhs-bridge",
                "pid": int(process.pid),
                "executable": str(exe),
                "command": command,
                "command_line": subprocess.list2cmdline(command),
                "project_root": str(project_root),
                "working_directory": str(bridge_root),
                "port": int(args.port),
                "port_argument": port_argument,
                "host": "127.0.0.1",
                "creation_time": creation_time.isoformat(),
                "creation_time_utc_ticks": int(creation_ticks),
                "version": args.version,
                "headless": not args.show_browser,
                "token_configured": True,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except OSError as exc:
        # Without an identity file stop/health must refuse to trust the PID.
        print(f"failed to record bridge identity: {exc}", file=sys.stderr)
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        _remove_state(pid_file, identity_file)
        return 1

    time.sleep(2.0)
    code = process.poll()
    if code is not None:
        print(f"xhs bridge exited immediately (exit {code})", file=sys.stderr)
        tail = _tail(err_path) or _tail(out_path)
        if tail:
            print(tail, file=sys.stderr)
        _remove_state(pid_file, identity_file)
        return 1

    print(f"xhs bridge started (PID {process.pid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
