"""Transactional program-file replacement with a recoverable backup."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable


_PROTECTED_ROOTS = {".git", "state", "runtime", "browsers", "raw", "courses", "site"}


def _relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("升级清单包含无效相对路径")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("升级清单不能越出程序目录")
    if ":" in relative.parts[0] or relative.parts[0].casefold() in _PROTECTED_ROOTS:
        raise ValueError("升级清单不能包含用户数据或运行时目录")
    if any(part == ".env" or part.startswith(".env.") for part in relative.parts):
        raise ValueError("升级清单不能覆盖本地凭据")
    return relative


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _reject_links(path: Path, root: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("升级目标或来源包含符号链接")


def install_program_files(
    source_root: str | Path,
    target_root: str | Path,
    relative_files: Iterable[str],
    backup_root: str | Path,
) -> Path:
    """Copy an explicit program-file allowlist and roll back on any failure.

    The backup remains after success so a caller can roll back deliberately.
    No unlisted target files are deleted.
    """
    source = Path(source_root).expanduser().resolve(strict=True)
    target = Path(target_root).expanduser().resolve(strict=True)
    backup_base = Path(backup_root).expanduser().resolve()
    if not source.is_dir() or not target.is_dir():
        raise ValueError("升级来源和目标必须是现有目录")
    if _inside(backup_base, target) or _inside(backup_base, source):
        raise ValueError("备份目录必须位于来源和目标程序目录之外")

    entries: list[tuple[PurePosixPath, Path, Path]] = []
    seen: set[str] = set()
    for value in relative_files:
        relative = _relative_path(value)
        key = relative.as_posix().casefold()
        if key in seen:
            raise ValueError("升级清单包含重复路径")
        seen.add(key)
        src = source.joinpath(*relative.parts)
        dst = target.joinpath(*relative.parts)
        _reject_links(src, source)
        _reject_links(dst, target)
        if not src.is_file():
            raise ValueError("升级来源缺少清单文件")
        if not _inside(src.resolve(strict=True), source) or (dst.exists() and not _inside(dst.resolve(), target)):
            raise ValueError("升级路径越出程序目录")
        entries.append((relative, src, dst))

    backup_base.mkdir(parents=True, exist_ok=True)
    backup = backup_base / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") / uuid.uuid4().hex
    backup.mkdir(parents=True)
    manifest: list[dict[str, object]] = []
    for relative, _src, dst in entries:
        existed = dst.is_file()
        item: dict[str, object] = {"file": relative.as_posix(), "existed": existed}
        if existed:
            saved = backup.joinpath(*relative.parts)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, saved)
            digest = _sha256(dst)
            if _sha256(saved) != digest:
                raise RuntimeError("程序文件备份校验失败")
            item["sha256"] = digest
        manifest.append(item)
    manifest_path = backup / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    applied: list[tuple[PurePosixPath, Path, bool]] = []
    try:
        for relative, src, dst in entries:
            dst.parent.mkdir(parents=True, exist_ok=True)
            existed = dst.is_file()
            descriptor, temp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".update", dir=dst.parent)
            os.close(descriptor)
            staging = Path(temp_name)
            try:
                shutil.copy2(src, staging)
                with staging.open("rb+") as stream:
                    os.fsync(stream.fileno())
                os.replace(staging, dst)
                applied.append((relative, dst, existed))
            finally:
                staging.unlink(missing_ok=True)
    except Exception:
        for relative, dst, existed in reversed(applied):
            if existed:
                saved = backup.joinpath(*relative.parts)
                descriptor, temp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".rollback", dir=dst.parent)
                os.close(descriptor)
                staging = Path(temp_name)
                try:
                    shutil.copy2(saved, staging)
                    os.replace(staging, dst)
                finally:
                    staging.unlink(missing_ok=True)
            else:
                dst.unlink(missing_ok=True)
        raise
    return backup


def restore_program_files(backup_root: str | Path, target_root: str | Path) -> None:
    """Restore one verified program-file backup without touching user data."""
    backup = Path(backup_root).expanduser().resolve(strict=True)
    target = Path(target_root).expanduser().resolve(strict=True)
    manifest_path = backup / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("升级备份清单无法读取") from error
    if not isinstance(manifest, list):
        raise ValueError("升级备份清单格式无效")

    entries: list[tuple[PurePosixPath, Path, bool]] = []
    seen: set[str] = set()
    for item in manifest:
        if not isinstance(item, dict) or not isinstance(item.get("existed"), bool):
            raise ValueError("升级备份清单条目无效")
        relative = _relative_path(item.get("file"))
        key = relative.as_posix().casefold()
        if key in seen:
            raise ValueError("升级备份包含重复路径")
        seen.add(key)
        saved = backup.joinpath(*relative.parts)
        destination = target.joinpath(*relative.parts)
        _reject_links(saved, backup)
        _reject_links(destination, target)
        if item["existed"]:
            if not saved.is_file() or _sha256(saved) != item.get("sha256"):
                raise ValueError("升级备份文件校验失败")
        elif saved.exists():
            raise ValueError("升级备份清单与文件不一致")
        entries.append((relative, saved, item["existed"]))

    rollback = Path(tempfile.mkdtemp(prefix="restore-current-", dir=backup))
    current: dict[str, bool] = {}
    applied: list[tuple[PurePosixPath, Path]] = []
    try:
        for relative, _saved, _existed in entries:
            destination = target.joinpath(*relative.parts)
            current[relative.as_posix()] = destination.is_file()
            if destination.is_file():
                saved_current = rollback.joinpath(*relative.parts)
                saved_current.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, saved_current)
        try:
            for relative, saved, existed in entries:
                destination = target.joinpath(*relative.parts)
                if existed:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    descriptor, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".restore", dir=destination.parent)
                    os.close(descriptor)
                    staging = Path(temp_name)
                    try:
                        shutil.copy2(saved, staging)
                        os.replace(staging, destination)
                        applied.append((relative, destination))
                    finally:
                        staging.unlink(missing_ok=True)
                else:
                    destination.unlink(missing_ok=True)
                    applied.append((relative, destination))
        except Exception:
            for relative, destination in reversed(applied):
                saved_current = rollback.joinpath(*relative.parts)
                if current[relative.as_posix()]:
                    descriptor, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".rollback", dir=destination.parent)
                    os.close(descriptor)
                    staging = Path(temp_name)
                    try:
                        shutil.copy2(saved_current, staging)
                        os.replace(staging, destination)
                    finally:
                        staging.unlink(missing_ok=True)
                else:
                    destination.unlink(missing_ok=True)
            raise
    finally:
        shutil.rmtree(rollback)


def backup_sqlite_database(source: str | Path, destination: str | Path) -> Path:
    """Take a consistent SQLite backup and verify it before program upgrade."""
    source_path = Path(source).expanduser().resolve(strict=True)
    destination_path = Path(destination).expanduser().resolve()
    if not source_path.is_file() or source_path == destination_path:
        raise ValueError("SQLite 备份来源或目标无效")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source_db = sqlite3.connect(str(source_path))
    target_db = sqlite3.connect(destination_path)
    try:
        source_db.execute("PRAGMA query_only = ON")
        source_db.backup(target_db)
        result = target_db.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("SQLite 备份完整性检查失败")
    finally:
        target_db.close()
        source_db.close()
    return destination_path
