"""Launch the bot worker or console as a detached, windowless process.

The PowerShell wrapper calls this instead of Start-Process so the child is not
tied to the caller's console. Only stdlib is used.

Usage:
    python scripts/launch.py --config <abs> --name worker \
        --log-dir var/logs --pid-file var/run/worker.pid -- worker
"""

# =============================================================================
# 中文导读：在 Windows 上“无窗口”启动后台进程
# =============================================================================
# 直接 Start-Process 启动的 Python 会带一个控制台窗口，
# 而且子进程可能跟着调用方的窗口一起退出。
# 这个脚本用 subprocess.Popen + CREATE_NO_WINDOW 启动：
#   - 没有可见窗口；
#   - 标准输入接到空设备，不会卡在等待输入；
#   - stdout/stderr 重定向到 var/logs/*.log；
#   - 把 PID 写进 var/run/*.pid，方便 stop 脚本按号停止；
#   - 启动后等 1.3 秒检查一次，如果立刻退出就把错误日志尾部打印出来。
# 只用标准库，确保在最小 Python 环境里也能运行。
# =============================================================================

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _identity_path(pid_file: Path) -> Path:
    # worker.pid -> worker.json, next to the PID file.
    return pid_file.with_suffix(".json")


def _write_identity(
    *,
    pid_file: Path,
    name: str,
    pid: int,
    executable: str,
    command: list[str],
    root: Path,
    config: Path,
) -> Path:
    """Record enough identity to verify the process before a later stop."""
    path = _identity_path(pid_file)
    payload = {
        "name": name,
        "pid": int(pid),
        "executable": executable,
        "command": list(command),
        "command_line": subprocess.list2cmdline(command),
        "project_root": str(root),
        "cwd": str(root),
        "config": str(config),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _tail(path: Path, lines: int = 30) -> str:
    # 读取日志文件最后若干行，用于启动失败时把关键错误显示出来。
    # 文件不存在/没权限时返回空字符串，不让“打印日志”本身再抛异常。
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def main(argv: list[str] | None = None) -> int:
    # 参数分两部分：launch.py 自己的参数，以及最后的 bot 子命令。
    # REMAINDER 把 "--" 之后的参数原样收集起来。
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--name", required=True, choices=["worker", "console", "gateway"])
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--pid-file", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("bot_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    bot_args = list(args.bot_args)
    # argparse.REMAINDER 有时会把分隔符 "--" 也收进来，这里去掉它。
    if bot_args and bot_args[0] == "--":
        bot_args = bot_args[1:]
    if not bot_args:
        # 没有 bot 子命令就不知道要启动什么，直接报错。
        print("missing bot command", file=sys.stderr)
        return 2

    root = Path.cwd().resolve()
    # 配置文件路径：相对路径按当前工作目录解析，最后统一成绝对路径，
    # 因为子进程的 cwd 是 root，相对路径容易产生歧义。
    config = Path(args.config)
    if not config.is_absolute():
        config = (root / config).resolve()
    if not config.is_file():
        print(f"config not found: {config}", file=sys.stderr)
        return 2

    log_dir = Path(args.log_dir)
    # 日志目录和 PID 目录都允许相对路径，按项目根目录解析并自动创建。
    if not log_dir.is_absolute():
        log_dir = root / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    pid_file = Path(args.pid_file)
    if not pid_file.is_absolute():
        pid_file = root / pid_file
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    out_path = log_dir / f"{args.name}.out.log"
    err_path = log_dir / f"{args.name}.err.log"
    # "ab" = 追加写二进制；buffering=0 让子进程输出尽快落盘，
    # 出问题时能马上在日志里看到。
    out = open(out_path, "ab", buffering=0)
    err = open(err_path, "ab", buffering=0)
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        # Do NOT use CREATE_NO_WINDOW here.  On the embedded Python shipped
        # with this deployment it makes the child fail during DLL init with
        # STATUS_DLL_INIT_FAILED (0xC0000142).  Hide the inherited console
        # window via STARTUPINFO instead; the child still starts normally.
        # 中文：这套内嵌 Python 在 CREATE_NO_WINDOW 下会 0xC0000142；
        # 改用 STARTUPINFO 隐藏窗口，避免启动失败。
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    env = dict(os.environ)
    # 强制子进程用 UTF-8 输出并且不缓冲，避免中文日志在 Windows 上乱码/延迟。
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    local_tmp = root / "var" / "tmp"
    local_tmp.mkdir(parents=True, exist_ok=True)
    env["TEMP"] = str(local_tmp)
    env["TMP"] = str(local_tmp)
    env["TMPDIR"] = str(local_tmp)
    node_exe = Path(r"D:\Dev\Tools\node-v22.23.2-win-x64\node.exe")
    if "PLAYWRIGHT_NODEJS_PATH" not in env and node_exe.is_file():
        env["PLAYWRIGHT_NODEJS_PATH"] = str(node_exe)

    # Prefer an absolute executable path in the identity record.  This is what
    # stop.ps1 compares against the live process image before killing anything.
    if Path(args.python).exists():
        executable = str(Path(args.python).resolve())
    else:
        found = shutil.which(args.python)
        executable = str(Path(found).resolve()) if found else args.python
    command = [executable, "-m", "bot", "--config", str(config), *bot_args]
    # Popen 启动后立刻返回；这里不等待子进程结束，所以是“后台运行”。
    try:
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=creationflags,
            startupinfo=startupinfo,
            close_fds=True,
            env=env,
        )
    except OSError as exc:
        print(f"failed to launch {args.name}: {exc}", file=sys.stderr)
        return 1

    pid_file.write_text(str(process.pid), encoding="ascii")
    try:
        _write_identity(
            pid_file=pid_file,
            name=args.name,
            pid=process.pid,
            executable=executable,
            command=command,
            root=root,
            config=config,
        )
    except OSError as exc:
        # Without an identity file, stop.ps1 must refuse to trust the PID.
        # Do not leave an unmanageable process behind.
        print(f"failed to record process identity: {exc}", file=sys.stderr)
        process.terminate()
        return 1
    # 等一小会儿再看进程是否还活着。很多配置错误会让进程立即退出，
    # 这种情况下直接把日志尾部显示出来，比让用户自己去翻文件友好。
    time.sleep(1.3)
    code = process.poll()
    if code is not None:
        print(f"{args.name} exited immediately (exit {code})", file=sys.stderr)
        tail = _tail(err_path) or _tail(out_path)
        if tail:
            print(tail, file=sys.stderr)
        try:
            # 启动失败就不该留下一个指向死进程的 PID 文件或身份记录。
            pid_file.unlink()
        except OSError:
            pass
        try:
            _identity_path(pid_file).unlink()
        except OSError:
            pass
        return 1
    print(f"{args.name} started (PID {process.pid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
