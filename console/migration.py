"""Preview, back up, and idempotently index legacy workspace material."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Any, Callable

from .storage import ConsoleStorage, stable_account_id


class StaleMigrationPreview(ValueError):
    """Raised when source files changed after a preview was created."""


class MigrationService:
    MAX_FILES = 20_000
    MAX_JSON_BYTES = 2 * 1024 * 1024
    LEGACY_STATES = {"done", "failed", "pending", "in_progress", "running"}

    def __init__(
        self,
        storage: ConsoleStorage,
        workspace_root: Callable[[], Path | None],
        *,
        backup_root: Path | None = None,
        backup_sources: Callable[[], list[Path]] | None = None,
    ) -> None:
        self.storage = storage
        self.workspace_root = workspace_root
        self.backup_root = (backup_root or storage.db_path.parent / "migration-backups").resolve()
        self.backup_sources = backup_sources or (lambda: [])
        self.local_account_id = stable_account_id("local", "workspace")

    def preview(self) -> dict[str, Any]:
        scan = self._scan()
        preview_id = str(uuid.uuid4())
        result = {
            "preview_id": preview_id,
            "fingerprint": scan["fingerprint"],
            "course_count": sum(item["identity"] not in scan["conflicted_course_identities"] for item in scan["courses"]),
            "lecture_count": sum(
                item["course_identity"] not in scan["conflicted_course_identities"]
                and f"{item['course_identity']}\0{item['identity']}" not in scan["conflicted_lecture_identities"]
                for item in scan["lectures"]
            ),
            "conflicts": scan["conflicts"],
            "mappings": self._public_mappings(scan),
            "legacy_batches": scan["legacy_batches"],
            "sources": [item["path"] for item in scan["files"]],
        }
        saved = {**result, "_scan": scan}
        with self.storage.transaction() as connection:
            connection.execute(
                "INSERT INTO migration_runs(id, state, preview_json, backup_path, created_at) VALUES (?, 'preview', ?, NULL, ?)",
                (preview_id, json.dumps(saved, ensure_ascii=False, sort_keys=True), self._now()),
            )
        return result

    @staticmethod
    def _public_mappings(scan: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for course in scan["courses"]:
            if course["identity"] in scan["conflicted_course_identities"]:
                continue
            lectures = [
                {
                    "identity_kind": lecture["identity_kind"], "identity": lecture["identity"],
                    "title": lecture["title"], "class_date": lecture.get("class_date"),
                }
                for lecture in scan["lectures"]
                if lecture["course_identity"] == course["identity"]
                and f"{lecture['course_identity']}\0{lecture['identity']}" not in scan["conflicted_lecture_identities"]
            ]
            result.append({
                "identity_kind": course["identity_kind"], "course_identity": course["identity"],
                "title": course["title"], "lectures": lectures,
            })
        return result

    def apply(self, preview_id: str) -> dict[str, Any]:
        row = self.storage.connection.execute(
            "SELECT state, preview_json, backup_path FROM migration_runs WHERE id = ?", (preview_id,)
        ).fetchone()
        if row is None:
            raise ValueError("没有找到这个迁移预览。")
        if row["state"] == "applied":
            return {"preview_id": preview_id, "state": "applied", "backup_path": row["backup_path"]}
        saved = json.loads(row["preview_json"])
        scan = saved.get("_scan")
        current = self._scan()
        if not isinstance(scan, dict) or current["fingerprint"] != saved.get("fingerprint"):
            raise StaleMigrationPreview("资料在预览后发生变化，请重新生成预览。")

        backup_path = self._create_backup(preview_id, scan)
        try:
            with self.storage.transaction():
                self.storage.upsert_account(provider="local", platform_account_id="workspace")
                course_ids: dict[str, str] = {}
                for item in scan["courses"]:
                    if item["identity"] in scan["conflicted_course_identities"]:
                        continue
                    if item["identity_kind"] == "platform":
                        course = self.storage.upsert_course(
                            account_id=self.local_account_id,
                            platform_course_id=item["identity"], title=item["title"],
                            teacher=item.get("teacher"), source="local",
                        )
                    else:
                        course = self.storage.upsert_import_course(
                            account_id=self.local_account_id, import_key=item["identity"], title=item["title"],
                            teacher=item.get("teacher"),
                        )
                    course_ids[item["identity"]] = course.id
                for item in scan["lectures"]:
                    if item["course_identity"] not in course_ids or item["identity"] in scan["conflicted_lecture_identities"]:
                        continue
                    course_id = course_ids[item["course_identity"]]
                    manifest_path = str(Path(scan["workspace"]) / item["path"])
                    common = {
                        "course_id": course_id, "title": item["title"], "class_date": item.get("class_date"),
                        "playback_available": item.get("playback_available", False), "manifest_path": manifest_path,
                    }
                    if item["identity_kind"] == "platform":
                        self.storage.upsert_lecture(platform_sub_id=item["identity"], **common)
                    else:
                        self.storage.upsert_import_lecture(import_key=item["identity"], **common)
                with self.storage.transaction() as connection:
                    connection.execute(
                        "UPDATE migration_runs SET state = 'applied', backup_path = ? WHERE id = ?",
                        (str(backup_path), preview_id),
                    )
        except Exception:
            shutil.rmtree(backup_path, ignore_errors=True)
            raise
        return {"preview_id": preview_id, "state": "applied", "backup_path": str(backup_path)}

    @staticmethod
    def backup_database_path(backup_path: str | Path) -> Path:
        return Path(backup_path) / "console.sqlite3"

    @classmethod
    def restore_backup(
        cls, backup_path: str | Path, *, database_path: str | Path, workspace_root: str | Path,
        configuration_paths: list[str | Path] | None = None,
    ) -> None:
        """Restore a verified snapshot into an explicitly selected offline target."""
        backup = Path(backup_path).resolve(strict=True)
        manifest_path = backup / "backup-manifest.json"
        if cls._linked(manifest_path):
            raise ValueError("备份清单不可用。")
        manifest = cls._read_json(manifest_path)
        database_snapshot = backup / "console.sqlite3"
        if cls._linked(database_snapshot) or not database_snapshot.is_file():
            raise ValueError("备份缺少任务数据库快照。")
        check = sqlite3.connect(str(database_snapshot))
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份数据库校验失败。")
        finally:
            check.close()
        for item in manifest.get("files", []):
            relative = Path(str(item.get("path") or ""))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("备份文件路径无效。")
            source = backup / relative
            if cls._linked(source) or not source.is_file() or cls._sha256(source) != item.get("sha256"):
                raise ValueError("备份文件校验失败。")
            if relative.parts and relative.parts[0] == "workspace":
                target_root = Path(workspace_root).expanduser().resolve()
                target = (target_root / Path(*relative.parts[1:])).resolve()
                target.relative_to(target_root)
                if any(cls._linked(parent) for parent in (target_root, *target.parents) if parent.exists()):
                    raise ValueError("恢复目标包含链接目录。")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            elif relative.parts and relative.parts[0] == "config":
                try:
                    index = int(relative.parts[1].split("-", 1)[0])
                    target = Path((configuration_paths or [])[index]).expanduser().resolve()
                except (IndexError, ValueError):
                    raise ValueError("恢复配置时必须提供原配置文件位置。") from None
                if target.exists() and cls._linked(target):
                    raise ValueError("恢复配置目标不可用。")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        target_db = Path(database_path).expanduser().resolve()
        target_db.parent.mkdir(parents=True, exist_ok=True)
        temporary_db = target_db.with_name(f".{target_db.name}.{uuid.uuid4().hex}.restore")
        try:
            shutil.copy2(database_snapshot, temporary_db)
            os.replace(temporary_db, target_db)
        finally:
            temporary_db.unlink(missing_ok=True)

    def _scan(self) -> dict[str, Any]:
        configured = self.workspace_root()
        if configured is None:
            raise ValueError("请先在设置中配置资料目录。")
        try:
            root = Path(configured).expanduser().resolve(strict=True)
        except OSError as error:
            raise ValueError("资料目录当前不可用，请在设置中检查保存位置。") from error
        raw_root = root / "raw"
        if self._linked(raw_root) or not raw_root.is_dir():
            legacy_batches, batch_files, batch_conflicts = self._scan_batches(root)
            return self._scan_result(root, [], [], batch_conflicts, batch_files, legacy_batches=legacy_batches)

        files: list[dict[str, str]] = []
        conflicts: list[dict[str, str]] = []
        courses_by_identity: dict[str, dict[str, Any]] = {}
        lectures_by_identity: dict[str, dict[str, Any]] = {}
        course_variants: dict[str, set[str]] = {}
        lecture_variants: dict[str, set[str]] = {}
        for parent, directories, names in os.walk(raw_root, followlinks=False):
            directories[:] = [name for name in directories if not self._linked(Path(parent) / name)]
            if Path(parent).name == "slides" and "index.json" in names:
                slide_index = Path(parent) / "index.json"
                if not self._linked(slide_index) and len(files) < self.MAX_FILES:
                    files.append({"path": slide_index.relative_to(root).as_posix(), "sha256": self._sha256(slide_index)})
            if Path(parent).name != "原始数据" or "manifest.json" not in names:
                continue
            path = Path(parent) / "manifest.json"
            if self._linked(path) or len(files) >= self.MAX_FILES:
                continue
            relative = path.relative_to(root).as_posix()
            digest = self._sha256(path)
            try:
                path.resolve(strict=True).relative_to(root)
                payload = self._read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                files.append({"path": relative, "sha256": digest})
                conflicts.append({"kind": "unreadable_manifest", "identity": relative, "reason": "索引清单无法读取，原文件保留且未导入。"})
                continue
            files.append({"path": relative, "sha256": digest})
            course_dir, session_dir = path.parent.parent.parent, path.parent.parent
            platform_course_id = str(payload.get("course_id") or "").strip()
            platform_sub_id = str(payload.get("sub_id") or "").strip()
            if platform_course_id:
                course_identity, course_kind = platform_course_id, "platform"
            else:
                course_identity, course_kind = self._import_key(root, course_dir, "course"), "import"
            course_title = str(payload.get("course_title") or course_dir.name).strip()
            course_signature = json.dumps([
                course_title, str(payload.get("teacher") or payload.get("lecturer_name") or "").strip(),
                str(payload.get("term") or "").strip(),
            ], ensure_ascii=False)
            course_variants.setdefault(course_identity, set()).add(course_signature)
            courses_by_identity.setdefault(course_identity, {
                "identity": course_identity, "identity_kind": course_kind,
                "title": course_title, "teacher": str(payload.get("teacher") or payload.get("lecturer_name") or "").strip() or None,
            })
            if platform_sub_id:
                lecture_identity, lecture_kind = platform_sub_id, "platform"
            else:
                lecture_identity, lecture_kind = self._import_key(root, session_dir, "lecture"), "import"
            lecture_key = f"{course_identity}\0{lecture_identity}"
            lecture_title = str(payload.get("session_title") or session_dir.name).strip()
            signature = json.dumps([lecture_title, payload.get("date"), payload.get("playback_available")], ensure_ascii=False)
            lecture_variants.setdefault(lecture_key, set()).add(signature)
            lectures_by_identity.setdefault(lecture_key, {
                "identity": lecture_identity, "identity_kind": lecture_kind, "course_identity": course_identity,
                "title": lecture_title, "class_date": str(payload.get("date") or "").strip() or None,
                "playback_available": payload.get("playback_available") is True,
                "path": relative,
            })

        legacy_batches, batch_files, batch_conflicts = self._scan_batches(root)
        files.extend(batch_files)
        conflicts.extend(batch_conflicts)
        conflicted_courses = {key for key, values in course_variants.items() if len(values) > 1}
        existing_courses = {
            row["identity"]: (row["title"], row["teacher"], row["term"])
            for row in self.storage.connection.execute(
                "SELECT COALESCE(platform_course_id, import_key) AS identity, title, teacher, term FROM courses WHERE account_id = ?",
                (self.local_account_id,),
            ).fetchall()
        }
        for item in courses_by_identity.values():
            existing = existing_courses.get(item["identity"])
            if existing and existing != (item["title"], item.get("teacher"), item.get("term")):
                conflicted_courses.add(item["identity"])
                conflicts.append({"kind": "course_identity", "identity": item["identity"], "reason": "本地已有课程名称与待迁移资料不同，需人工核对。"})
        for identity in sorted(conflicted_courses):
            if not any(item.get("kind") == "course_identity" and item.get("identity") == identity for item in conflicts):
                conflicts.append({"kind": "course_identity", "identity": identity, "reason": "同一课程身份包含不同课程信息，需人工核对。"})
        conflicted_lectures = {key for key, values in lecture_variants.items() if len(values) > 1}
        existing_lectures = {
            (row["course_identity"], row["identity"]): (row["title"], row["class_date"])
            for row in self.storage.connection.execute(
                """SELECT c.platform_course_id AS course_identity, l.platform_sub_id AS identity, l.title, l.class_date
                   FROM lectures l JOIN courses c ON c.id = l.course_id
                   WHERE c.account_id = ? AND c.platform_course_id IS NOT NULL AND l.platform_sub_id IS NOT NULL
                   UNION ALL
                   SELECT c.import_key AS course_identity, l.import_key AS identity, l.title, l.class_date
                   FROM lectures l JOIN courses c ON c.id = l.course_id
                   WHERE c.account_id = ? AND c.import_key IS NOT NULL AND l.import_key IS NOT NULL""",
                (self.local_account_id, self.local_account_id),
            ).fetchall()
        }
        for item in lectures_by_identity.values():
            existing = existing_lectures.get((item["course_identity"], item["identity"]))
            if existing and existing != (item["title"], item.get("class_date")):
                key = f"{item['course_identity']}\0{item['identity']}"
                conflicted_lectures.add(key)
                conflicts.append({"kind": "lecture_identity", "identity": hashlib.sha256(key.encode()).hexdigest()[:16], "reason": "本地已有课次名称与待迁移资料不同，需人工核对。"})
        for identity in sorted(conflicted_lectures):
            conflicts.append({"kind": "lecture_identity", "identity": hashlib.sha256(identity.encode()).hexdigest()[:16], "reason": "同一课次身份在多个文件中内容不同，需人工核对。"})
        return self._scan_result(
            root,
            list(courses_by_identity.values()),
            list(lectures_by_identity.values()),
            conflicts,
            files,
            legacy_batches=legacy_batches,
            conflicted_course_identities=conflicted_courses,
            conflicted_lecture_identities=conflicted_lectures,
        )

    def _scan_batches(self, root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
        candidates = [root / "batch_state.json"]
        batch_root = root / "state" / "batches"
        if batch_root.is_dir() and not self._linked(batch_root):
            candidates.extend(sorted(batch_root.glob("*.json")))
        reports, files, conflicts = [], [], []
        for path in candidates[: self.MAX_FILES]:
            if not path.is_file() or self._linked(path):
                continue
            try:
                payload = self._read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                relative = path.relative_to(root).as_posix()
                files.append({"path": relative, "sha256": self._sha256(path)})
                conflicts.append({"kind": "unreadable_batch", "identity": relative, "reason": "旧批次记录无法读取，原文件保留且未推断任务状态。"})
                continue
            relative = path.relative_to(root).as_posix()
            files.append({"path": relative, "sha256": self._sha256(path)})
            lectures = []
            for row in payload.get("lectures", []) if isinstance(payload.get("lectures"), list) else []:
                if not isinstance(row, dict):
                    continue
                state = str(row.get("status") or "").strip()
                item = {"sub_id": str(row.get("sub_id") or ""), "status": state if state in self.LEGACY_STATES else "unknown"}
                if item["status"] == "unknown":
                    item["needs_review"] = True
                lectures.append(item)
            reports.append({"path": relative, "course_id": str(payload.get("course_id") or ""), "lectures": lectures})
        return reports, files, conflicts

    def _scan_result(self, root, courses, lectures, conflicts, files, *, legacy_batches=None, conflicted_course_identities=None, conflicted_lecture_identities=None):
        files.sort(key=lambda item: item["path"])
        rows = [dict(row) for row in self.storage.connection.execute(
            """SELECT c.platform_course_id, c.import_key, c.title AS course_title,
                      l.platform_sub_id, l.import_key AS lecture_import_key, l.title AS lecture_title,
                      l.class_date, l.manifest_path
               FROM courses c LEFT JOIN lectures l ON l.course_id = c.id
               WHERE c.account_id = ? ORDER BY c.id, l.id""",
            (self.local_account_id,),
        ).fetchall()]
        fingerprint = hashlib.sha256(json.dumps({"files": files, "indexed": rows}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {
            "workspace": str(root), "fingerprint": fingerprint, "courses": courses, "lectures": lectures,
            "conflicts": conflicts, "files": files, "legacy_batches": legacy_batches or [],
            "conflicted_course_identities": sorted(conflicted_course_identities or []),
            "conflicted_lecture_identities": sorted(conflicted_lecture_identities or []),
        }

    def _create_backup(self, preview_id: str, scan: dict[str, Any]) -> Path:
        destination = self.backup_root / preview_id
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        try:
            target_db = destination / "console.sqlite3"
            source = self.storage.connection
            target = sqlite3.connect(str(target_db))
            try:
                source.backup(target)
            finally:
                target.close()
            try:
                os.chmod(target_db, 0o600)
            except OSError:
                pass
            files = []
            for item in scan["files"]:
                source_path = Path(scan["workspace"]) / item["path"]
                if self._sha256(source_path) != item["sha256"]:
                    raise StaleMigrationPreview("资料在备份期间发生变化，请重新生成预览。")
                relative = Path("workspace") / item["path"]
                output = destination / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, output)
                if self._sha256(output) != item["sha256"]:
                    raise StaleMigrationPreview("资料在备份期间发生变化，请重新生成预览。")
                try:
                    os.chmod(output, 0o600)
                except OSError:
                    pass
                files.append({"path": relative.as_posix(), "sha256": item["sha256"]})
            for index, source_path in enumerate(self.backup_sources()):
                source_path = Path(source_path).expanduser()
                if source_path.is_file() and not self._linked(source_path):
                    relative = Path("config") / f"{index:02d}-{source_path.name}"
                    output = destination / relative
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_path, output)
                    try:
                        os.chmod(output, 0o600)
                    except OSError:
                        pass
                    files.append({"path": relative.as_posix(), "sha256": self._sha256(output)})
            manifest = {"preview_id": preview_id, "source_fingerprint": scan["fingerprint"], "files": files}
            (destination / "backup-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.chmod(destination / "backup-manifest.json", 0o600)
                os.chmod(destination, 0o700)
            except OSError:
                pass
            return destination
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if path.stat().st_size > MigrationService.MAX_JSON_BYTES:
            raise ValueError("legacy metadata file is too large")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("legacy metadata must be an object")
        return data

    @staticmethod
    def _linked(path: Path) -> bool:
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))
        except OSError:
            return True

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(128 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _import_key(root: Path, path: Path, prefix: str) -> str:
        digest = hashlib.sha256(path.relative_to(root).as_posix().encode("utf-8")).hexdigest()
        return f"{prefix}:{digest}"

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
