"""Read-only status and preview for artifacts already stored in a workspace."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .contracts import CourseRecord, LectureRecord


ARTIFACT_LABELS = {
    "transcript": "字幕",
    "slides": "课件截图",
    "notes": "学习笔记",
    "timeline": "时间轴",
}


class ArtifactService:
    _MAX_MANIFESTS_TO_SCAN = 20_000
    _MAX_MANIFEST_BYTES = 2 * 1024 * 1024

    def __init__(self, workspace_root):
        self.workspace_root = workspace_root

    def for_lecture(self, course: CourseRecord, lecture: LectureRecord) -> dict[str, Any]:
        root = self.workspace_root()
        if root is None:
            return self._empty(lecture, "未配置资料目录")
        try:
            root = Path(root).expanduser().resolve(strict=True)
            manifest_input = Path(lecture.manifest_path) if lecture.manifest_path else None
            source = "本地资料索引"
            if manifest_input is None:
                if not course.platform_course_id or not lecture.platform_sub_id:
                    return self._empty(lecture, "尚未采集")
                found = self._find_identity_manifest(
                    root, course.platform_course_id, lecture.platform_sub_id
                )
                if found is None:
                    return self._empty(lecture, "尚未采集")
                manifest_path, manifest = found
                source = "课程资料清单"
            else:
                if self._is_link_or_reparse(manifest_input):
                    return self._empty(lecture, "资料索引不可用")
                manifest_path = manifest_input.resolve(strict=True)
                manifest_path.relative_to(root)
                if self._is_link_or_reparse(manifest_path) or not manifest_path.is_file():
                    return self._empty(lecture, "尚未采集")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return self._empty(lecture, "资料索引不可用")
        if not isinstance(manifest, dict):
            return self._empty(lecture, "资料索引不可用")
        if lecture.platform_sub_id and str(manifest.get("sub_id") or "") != lecture.platform_sub_id:
            return self._empty(lecture, "资料身份与课次不匹配")
        if course.platform_course_id and str(manifest.get("course_id") or "") and str(manifest.get("course_id")) != course.platform_course_id:
            return self._empty(lecture, "资料身份与课次不匹配")

        raw_root = manifest_path.parent
        artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
        base = str(manifest.get("base_name") or "").strip()
        if Path(base).name != base or "/" in base or "\\" in base:
            base = ""
        rows = [
            self._transcript_row(artifacts, root, course.platform_course_id, lecture.platform_sub_id, lecture.playback_available),
            self._slides_row(artifacts.get("slide_index"), root),
            self._notes_row(raw_root, base),
            self._file_row("timeline", artifacts.get("timeline_txt"), root, False),
        ]
        preview = self._notes_preview(raw_root, base)
        return {
            "lecture_id": lecture.id,
            "course_id": lecture.course_id,
            "items": rows,
            "notes_preview": preview,
            "source": source,
        }

    def has_identity_artifact(self, course_id: str, lecture_id: str, kind: str) -> bool:
        """Validate an artifact from its manifest when the local DB index is absent."""
        if kind not in {"transcript", "slides"}:
            return False
        try:
            configured = self.workspace_root()
            if configured is None:
                return False
            root = Path(configured).expanduser().resolve(strict=True)
        except (OSError, ValueError):
            return False
        found = self._find_identity_manifest(root, course_id, lecture_id)
        if found is None:
            return False
        _manifest_path, manifest = found
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            return False
        if kind == "transcript":
            row = self._transcript_row(
                artifacts,
                root,
                course_id,
                lecture_id,
                manifest.get("playback_available") is True,
            )
        else:
            row = self._slides_row(artifacts.get("slide_index"), root)
        return row.get("state") == "available"

    @classmethod
    def _find_identity_manifest(
        cls, root: Path, course_id: str, lecture_id: str
    ) -> tuple[Path, dict[str, Any]] | None:
        raw_root = root / "raw"
        if cls._is_link_or_reparse(raw_root) or not raw_root.is_dir():
            return None
        matches: list[tuple[Path, dict[str, Any]]] = []
        scanned = 0
        for parent, directories, names in os.walk(raw_root, followlinks=False):
            parent_path = Path(parent)
            directories[:] = [
                name for name in directories
                if not cls._is_link_or_reparse(parent_path / name)
            ]
            if parent_path.name != "原始数据" or "manifest.json" not in names:
                continue
            manifest_path = parent_path / "manifest.json"
            try:
                if cls._is_link_or_reparse(manifest_path):
                    continue
                scanned += 1
                if scanned > cls._MAX_MANIFESTS_TO_SCAN:
                    return None
                if manifest_path.stat().st_size > cls._MAX_MANIFEST_BYTES:
                    continue
                manifest_path.resolve(strict=True).relative_to(root)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if (
                isinstance(manifest, dict)
                and str(manifest.get("course_id") or "") == course_id
                and str(manifest.get("sub_id") or "") == lecture_id
            ):
                matches.append((manifest_path, manifest))
                if len(matches) > 1:
                    return None

        if len(matches) != 1:
            return None
        return matches[0]

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            return path.is_symlink() or bool(attributes & 1024)
        except OSError:
            return True

    @staticmethod
    def _empty(lecture: LectureRecord, reason: str) -> dict[str, Any]:
        items = []
        for kind, label in ARTIFACT_LABELS.items():
            state = "not_provided" if kind == "transcript" and not lecture.playback_available else "not_collected"
            items.append({"kind": kind, "label": label, "state": state, "message": reason})
        return {"lecture_id": lecture.id, "course_id": lecture.course_id, "items": items, "notes_preview": "", "source": "本地资料索引"}

    @staticmethod
    def _file_row(kind: str, raw_path: Any, root: Path, playback_available: bool) -> dict[str, str]:
        label = ARTIFACT_LABELS[kind]
        if not isinstance(raw_path, str) or not raw_path.strip():
            state = "not_provided" if kind == "transcript" and not playback_available else "not_collected"
            return {"kind": kind, "label": label, "state": state, "message": "平台未提供回放" if state == "not_provided" else "尚未采集"}
        try:
            input_path = Path(raw_path)
            candidate = root / input_path if not input_path.is_absolute() else input_path
            if candidate.is_symlink():
                raise ValueError("symbolic links are not accepted")
            path = candidate.resolve(strict=True)
            path.relative_to(root)
            if not path.is_file() or path.stat().st_size == 0:
                raise OSError("empty artifact")
        except (OSError, ValueError):
            return {"kind": kind, "label": label, "state": "expired", "message": "索引记录的文件已不存在或不可用"}
        return {"kind": kind, "label": label, "state": "available", "message": "可用", "name": path.name}

    @classmethod
    def _transcript_row(
        cls,
        artifacts: dict[str, Any],
        root: Path,
        course_id: str | None,
        lecture_id: str | None,
        playback_available: bool,
    ) -> dict[str, str]:
        raw_path = artifacts.get("subtitle_srt") or artifacts.get("transcript_txt")
        row = cls._file_row("transcript", raw_path, root, playback_available)
        if row["state"] != "available":
            return row
        try:
            path = Path(str(raw_path))
            candidate = root / path if not path.is_absolute() else path
            path = candidate.resolve(strict=True)
            meta_raw = artifacts.get("transcript_meta")
            meta_input = Path(str(meta_raw)) if isinstance(meta_raw, str) and meta_raw.strip() else path.with_suffix(".json")
            meta_candidate = root / meta_input if not meta_input.is_absolute() else meta_input
            if meta_candidate.is_symlink():
                raise ValueError("metadata symlink")
            meta_path = meta_candidate.resolve(strict=True)
            meta_path.relative_to(root)
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict) or metadata.get("complete") is not True:
                raise ValueError("incomplete transcript")
            if course_id and str(metadata.get("course_id") or "") != course_id:
                raise ValueError("course mismatch")
            if lecture_id and str(metadata.get("sub_id") or "") != lecture_id:
                raise ValueError("lecture mismatch")
            hashes = metadata.get("artifact_sha256")
            hash_key = "subtitle_srt" if path.suffix.lower() == ".srt" else "transcript_txt"
            expected_hash = hashes.get(hash_key) if isinstance(hashes, dict) else None
            if not isinstance(expected_hash, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                raise ValueError("transcript hash mismatch")
            if hash_key == "subtitle_srt" and not cls._valid_srt(path.read_text(encoding="utf-8", errors="replace")):
                raise ValueError("invalid SRT")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"kind": "transcript", "label": ARTIFACT_LABELS["transcript"], "state": "expired", "message": "字幕与课次完成记录不匹配或文件已损坏"}
        return row

    @staticmethod
    def _valid_srt(text: str) -> bool:
        blocks = [block.strip() for block in re.split(r"\r?\n\s*\r?\n", text.strip()) if block.strip()]
        if not blocks:
            return False
        timestamp = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})$", re.ASCII)

        def milliseconds(value: str) -> int | None:
            match = timestamp.fullmatch(value)
            if not match:
                return None
            hours, minutes, seconds, millis = map(int, match.groups())
            if minutes >= 60 or seconds >= 60:
                return None
            return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis

        for index, block in enumerate(blocks, 1):
            lines = block.splitlines()
            if len(lines) < 3 or lines[0].strip() != str(index):
                return False
            match = re.fullmatch(r"([^ ]+)\s+-->\s+([^ ]+)", lines[1].strip())
            if not match:
                return False
            start, end = milliseconds(match.group(1)), milliseconds(match.group(2))
            if start is None or end is None or end <= start or not "\n".join(lines[2:]).strip():
                return False
        return True

    @classmethod
    def _slides_row(cls, raw_path: Any, root: Path) -> dict[str, str]:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return {"kind": "slides", "label": ARTIFACT_LABELS["slides"], "state": "not_collected", "message": "尚未采集"}
        row = cls._file_row("slides", raw_path, root, True)
        if row["state"] == "available":
            try:
                data = json.loads((root / raw_path).read_text(encoding="utf-8"))
                slides = data.get("items") if isinstance(data, dict) else None
                if not isinstance(slides, list) or not slides:
                    return {"kind": "slides", "label": ARTIFACT_LABELS["slides"], "state": "expired", "message": "课件索引没有有效截图"}
            except (OSError, ValueError):
                return {"kind": "slides", "label": ARTIFACT_LABELS["slides"], "state": "expired", "message": "课件索引不可用"}
            succeeded = sum(1 for item in slides if isinstance(item, dict) and item.get("filename"))
            if succeeded == 0:
                return {"kind": "slides", "label": ARTIFACT_LABELS["slides"], "state": "expired", "message": "课件索引没有有效截图"}
            failed = data.get("failures") if isinstance(data, dict) else None
            row["message"] = f"{succeeded} 张截图可用" + ("（部分下载失败）" if isinstance(failed, list) and failed else "")
        return row

    @classmethod
    def _notes_row(cls, raw_root: Path, base: str) -> dict[str, str]:
        for name in ([f"{base}_notes.md", "notes.md"] if base else ["notes.md"]):
            path = raw_root / name
            if path.is_file() and not path.is_symlink():
                try:
                    text = path.read_bytes()[:4096].decode("utf-8", errors="replace").strip()
                    placeholder = re.match(r"(?i)(?:待生成|占位(?:内容)?|todo|暂无内容)[\s\S]*", text)
                    if path.stat().st_size > 0 and text and not placeholder:
                        return {"kind": "notes", "label": ARTIFACT_LABELS["notes"], "state": "available", "message": "可用", "name": path.name}
                except OSError:
                    pass
        return {"kind": "notes", "label": ARTIFACT_LABELS["notes"], "state": "not_collected", "message": "尚未整理"}

    @classmethod
    def _notes_preview(cls, raw_root: Path, base: str) -> str:
        row = cls._notes_row(raw_root, base)
        name = row.get("name")
        if not name:
            return ""
        try:
            text = (raw_root / name).read_bytes()[:4800].decode("utf-8", errors="replace").strip()
        except OSError:
            return ""
        return text[:1200]
