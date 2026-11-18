"""Read-only diagnostics and asset verification for publication tasks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from PIL import Image


_QUERY = re.compile(r"\?.*$")
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def redact_url(url: str) -> str:
    """Keep origin/path for diagnosis; drop query/fragment credentials."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return _QUERY.sub("", url or "")


def redact_text(text: str, *, limit: int = 1000) -> str:
    """Remove URLs and common credential-shaped values from model-visible text."""
    value = _URL.sub(lambda m: redact_url(m.group(0)), str(text or ""))
    value = re.sub(r"(?i)\b(token|secret|password|api[_-]?key)\s*[=:]\s*[^\s,;]+", r"\1=<redacted>", value)
    return value[:limit]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_assets(package: dict[str, Any], *, roots: list[str] | None = None) -> dict[str, Any]:
    """Return a structured report; never mutates files."""
    allowed_roots = [Path(p).resolve() for p in (roots or ["var/drafts"])]
    results: list[dict[str, Any]] = []
    for image in package.get("images") or []:
        item = {"ordinal": image.get("ordinal"), "path": image.get("path"), "expected": image.get("sha256")}
        path = Path(str(image.get("path") or "")).resolve()
        item["path"] = str(path)
        if not any(path == root or root in path.parents for root in allowed_roots):
            item.update(ok=False, reason="outside_approved_roots")
        elif not path.is_file():
            item.update(ok=False, reason="missing")
        else:
            actual = _sha256(path)
            item["actual"] = actual
            if actual != image.get("sha256"):
                item.update(ok=False, reason="hash_mismatch")
            else:
                try:
                    with Image.open(path) as im:
                        im.verify()
                    item.update(ok=True, reason="ok")
                except Exception as exc:
                    item.update(ok=False, reason=f"decode_failed: {exc}")
        results.append(item)
    return {
        "ok": bool(results) and all(r.get("ok") for r in results),
        "images": results,
        "checked": len(results),
    }


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def manifest_path(package: dict[str, Any], *, backup_root: str | Path = "var/backups/publication_assets") -> Path:
    package_hash = str(package.get("approved_package_hash") or package.get("package_hash") or "")
    if not package_hash:
        package_hash = hashlib.sha256(
            json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    return Path(backup_root).resolve() / package_hash / "manifest.json"


def ensure_asset_manifest(
    package: dict[str, Any], *, target_roots: list[str] | None = None,
    backup_root: str | Path = "var/backups/publication_assets",
) -> dict[str, Any]:
    """Create an immutable, program-owned backup manifest for approved assets.

    The model never supplies backup paths. If a source is already missing we
    record it as unavailable and the restore tool must wait; we never draw a
    replacement image.
    """
    target_roots_resolved = [Path(p).resolve() for p in (target_roots or ["var/drafts"])]
    root = Path(backup_root).resolve()
    path = manifest_path(package, backup_root=root)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("package_hash") == path.parent.name:
                images = data.get("images", [])
                # A manifest is only reusable for the same approved roots. A
                # caller asking about a different root must not inherit the
                # trust of a manifest created earlier for another root.
                for item in images:
                    target = Path(str(item.get("target") or "")).resolve()
                    if not _inside(target, target_roots_resolved):
                        visible = [dict(entry) for entry in images]
                        for entry in visible:
                            entry["available"] = False
                            entry["reason"] = "outside_target_roots"
                        return {
                            "ok": False,
                            "manifest": str(path),
                            "reused": True,
                            "reason": "outside_target_roots",
                            "images": visible,
                        }
                return {"ok": all(item.get("available") for item in images), "manifest": str(path), "reused": True, "images": images}
        except Exception:
            pass
    entries: list[dict[str, Any]] = []
    for image in package.get("images") or []:
        ordinal = int(image.get("ordinal"))
        sha = str(image.get("sha256") or "")
        raw = Path(str(image.get("path") or ""))
        item = {"asset_id": str(ordinal), "ordinal": ordinal, "sha256": sha, "target": str(raw), "backup": None, "available": False}
        if raw.is_symlink():
            item["reason"] = "target_is_symlink"
        else:
            target = raw.resolve()
            item["target"] = str(target)
            if not _inside(target, target_roots_resolved):
                item["reason"] = "outside_target_roots"
            elif not target.is_file():
                item["reason"] = "missing_at_manifest"
            elif _sha256(target) != sha:
                item["reason"] = "hash_mismatch_at_manifest"
            else:
                backup = root / path.parent.name / f"{ordinal:02d}-{sha}.bin"
                backup.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if not backup.exists() or _sha256(backup) != sha:
                        # Keep the temporary name short: the manifest directory
                        # already carries a full package hash, and Windows's
                        # classic MAX_PATH limit is easy to hit with a full
                        # asset hash + uuid suffix.
                        tmp = backup.with_name(f".{ordinal}.tmp-{uuid.uuid4().hex}")
                        try:
                            tmp.write_bytes(target.read_bytes())
                            if _sha256(tmp) != sha:
                                item["reason"] = "backup_hash_mismatch"
                            else:
                                os.replace(tmp, backup)
                        finally:
                            tmp.unlink(missing_ok=True)
                except OSError as exc:
                    item["reason"] = f"backup_error: {exc}"
                item.update(backup=str(backup), available=backup.is_file() and _sha256(backup) == sha)
        entries.append(item)
    data = {"version": 1, "package_hash": path.parent.name, "images": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": all(e["available"] for e in entries), "manifest": str(path), "reused": False, "images": entries}


def restore_from_manifest(
    package: dict[str, Any], asset_ids: list[str], *,
    target_roots: list[str] | None = None,
    backup_root: str | Path = "var/backups/publication_assets",
) -> dict[str, Any]:
    """Restore only exact-hash backups selected by program-owned asset ids."""
    path = manifest_path(package, backup_root=backup_root)
    if not path.is_file():
        return {"ok": False, "reason": "no_backup_manifest", "restored": []}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "reason": f"manifest_unreadable: {exc}", "restored": []}
    wanted = {str(x) for x in asset_ids}
    entries = {str(e.get("asset_id")): e for e in manifest.get("images", [])}
    if not wanted or any(x not in entries for x in wanted):
        return {"ok": False, "reason": "unknown_or_missing_asset_id", "restored": []}
    target_roots_resolved = [Path(p).resolve() for p in (target_roots or ["var/drafts"])]
    backup_root_resolved = Path(backup_root).resolve()
    restored: list[int] = []
    for asset_id in sorted(wanted, key=lambda x: int(x) if x.isdigit() else x):
        entry = entries[asset_id]
        sha = str(entry.get("sha256") or "")
        raw_target = Path(str(entry.get("target") or ""))
        if raw_target.is_symlink():
            return {"ok": False, "reason": "target_is_symlink", "restored": restored}
        target = raw_target.resolve()
        if not _inside(target, target_roots_resolved):
            return {"ok": False, "reason": "target_outside_roots", "restored": restored}
        if target.is_file() and _sha256(target) == sha:
            continue
        raw_backup = Path(str(entry.get("backup") or ""))
        if raw_backup.is_symlink():
            return {"ok": False, "reason": "backup_is_symlink", "restored": restored}
        backup = raw_backup.resolve()
        if not _inside(backup, [backup_root_resolved]) or not backup.is_file():
            return {"ok": False, "reason": "backup_unavailable", "restored": restored}
        if _sha256(backup) != sha:
            return {"ok": False, "reason": "backup_hash_mismatch", "restored": restored}
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.stem[:16]}.tmp-{uuid.uuid4().hex}")
        try:
            data = backup.read_bytes()
            tmp.write_bytes(data)
            if _sha256(tmp) != sha:
                return {"ok": False, "reason": "restore_hash_mismatch", "restored": restored}
            with Image.open(tmp) as im:
                im.verify()
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        if _sha256(target) != sha:
            return {"ok": False, "reason": "restored_hash_mismatch", "restored": restored}
        restored.append(int(asset_id))
    report = verify_assets(package, roots=target_roots)
    return {"ok": bool(report.get("ok")), "reason": "verified" if report.get("ok") else "verify_failed", "restored": restored, "verify": report}


def restore_same_bytes(
    package: dict[str, Any], backups: list[str], *, roots: list[str] | None = None
) -> dict[str, Any]:
    """Copy an exact-hash backup to a missing approved path.

    This is intentionally limited to bytes already in the frozen package.  It
    never searches arbitrary disk locations or follows symlinks, and the
    destination must stay under the approved draft roots.
    """
    allowed_roots = [Path(p).resolve() for p in (roots or ["var/drafts"])]
    restored: list[int] = []
    for image in package.get("images") or []:
        raw_target = Path(str(image.get("path") or ""))
        if raw_target.is_symlink():
            raise ValueError("approved image path is a symlink")
        target = raw_target.resolve()
        if not any(target == root or root in target.parents for root in allowed_roots):
            raise ValueError("approved image path is outside the approved roots")
        if target.exists() and _sha256(target) == image.get("sha256"):
            continue
        for raw in backups:
            src = Path(raw).resolve()
            if src.is_symlink() or not src.is_file():
                continue
            if _sha256(src) == image.get("sha256"):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(src.read_bytes())
                restored.append(int(image.get("ordinal")))
                break
    return {"restored": restored}
