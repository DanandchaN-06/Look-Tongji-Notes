"""Persistent handoff and verification for work performed by external agents."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from scripts.timeline_tools import _parse_timeline_text

from . import agent_bridge
from .contracts import WorkflowRecord
from .course_service import CourseService, CourseServiceError
from .settings_service import SettingsService
from .storage import ConsoleStorage


class AgentHandoffService:
    def __init__(self, storage: ConsoleStorage, courses: CourseService, settings: SettingsService, reader_service=None) -> None:
        self.storage = storage
        self.courses = courses
        self.settings = settings
        self.reader_service = reader_service

    def create(self, payload: dict[str, Any], idempotency_key: str) -> tuple[WorkflowRecord, bool, str]:
        if not isinstance(payload, dict) or set(payload) != {"lecture_id"}:
            raise ValueError("外部 Agent 交接只接受明确的 lecture_id")
        lecture_id = payload.get("lecture_id")
        if not isinstance(lecture_id, str) or not lecture_id.strip() or len(lecture_id) > 128:
            raise ValueError("必须选择一个明确课次")
        _validate_idempotency_key(idempotency_key)

        course, lecture = self.courses.get_owned_lecture(lecture_id.strip())
        course_id, sub_id = course.platform_course_id or "", lecture.platform_sub_id or ""
        if not course_id or not sub_id:
            raise CourseServiceError("NOT_SUPPORTED", "此课次没有可用于 Agent 交接的平台身份。", status=409)
        settings = self.settings.get()
        if settings.get("note", {}).get("route") != "external_agent":
            raise CourseServiceError("WRONG_ROUTE", "设置中的笔记路线不是外部 Agent。", status=409)

        artifacts = agent_bridge.detect_artifacts(course_id, sub_id)
        if not artifacts.get("lecture_dir") or not artifacts.get("has_transcript"):
            raise CourseServiceError("TRANSCRIPT_REQUIRED", "请先为这个课次完成字幕采集，再创建 Agent 交接。", status=409)
        note_settings = settings.get("note", {})
        handoff = agent_bridge.build_agent_instruction(
            kind="note", course_id=course_id, sub_id=sub_id,
            note_style=note_settings.get("style", "standard"),
            include_timeline=bool(note_settings.get("include_timeline", True)),
            artifacts=artifacts,
            course_context={
                "course_title": course.title,
                "session_title": lecture.title,
                "date": lecture.class_date or "",
                "teacher": course.teacher or "",
            },
        )
        instruction = str(handoff.get("instruction") or "").strip()
        if not instruction:
            raise CourseServiceError("TRANSCRIPT_REQUIRED", "当前素材不足以生成 Agent 交接指令。", status=409)

        public_settings = dict(settings)
        public_settings.pop("revision_id", None)
        target = {
            "course_id": course.id,
            "course_title": course.title,
            "lecture_id": lecture.id,
            "lecture_title": lecture.title,
            "class_date": lecture.class_date,
            "platform_course_id": course_id,
            "platform_sub_id": sub_id,
            "source": course.source,
            "operation_label": "外部 Agent 写作",
        }
        request_hash = hashlib.sha256(json.dumps(
            {"lecture_id": lecture.id, "operation": "agent_note"},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        workflow, created = self.storage.create_idempotent_workflow(
            account_id=course.account_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            lecture_id=lecture.id,
            operation="agent_note",
            settings_snapshot=public_settings,
            target=target,
        )
        if created:
            step = self.storage.upsert_step(
                workflow_id=workflow.id,
                name="agent_note",
                ordinal=0,
                state="waiting_agent",
                artifact={
                    "title": str(handoff.get("title") or "生成学习笔记"),
                    "instruction": instruction,
                    "warnings": [str(item) for item in handoff.get("warnings", []) if isinstance(item, str)],
                },
            )
            del step
            self.storage.update_workflow_state(workflow.id, "waiting_agent")
            workflow = self.storage.get_workflow(workflow.id)  # type: ignore[assignment]
        else:
            workflow = self.storage.get_workflow(workflow.id) or workflow
            instruction = self.instruction(workflow.id)
        return workflow, created, instruction

    def instruction(self, workflow_id: str) -> str:
        workflow = self._get_owned_workflow(workflow_id)
        step = next((item for item in workflow.steps if item.name == "agent_note"), None)
        instruction = step.artifact.get("instruction") if step else None
        return instruction if isinstance(instruction, str) else ""

    def verify(self, workflow_id: str) -> dict[str, Any]:
        workflow = self._get_owned_workflow(workflow_id)
        if workflow.operation != "agent_note":
            raise CourseServiceError("WRONG_OPERATION", "此任务不是 Agent 写作交接。", status=409)
        if workflow.state != "waiting_agent":
            return {"ready": workflow.state == "succeeded", "message": "此交接已处理。", "workflow": workflow}
        course_id = str(workflow.target.get("platform_course_id") or "")
        sub_id = str(workflow.target.get("platform_sub_id") or "")
        revision = self.storage.get_settings_revision(workflow.settings_revision_id or "")
        settings = revision.public_settings.get("note", {}) if revision else {}
        style = str(settings.get("style") or "standard")
        include_timeline = bool(settings.get("include_timeline", True))
        checked = self._verify_files(course_id, sub_id, style, include_timeline)
        if checked["ready"]:
            bridge_result = agent_bridge.verify_agent_output(
                kind="note", course_id=course_id, sub_id=sub_id,
                note_style=style, include_timeline=include_timeline,
            )
            checked = checked if bridge_result.get("ready") else {
                "ready": False, "missing": bridge_result.get("missing", ["Agent 成果校验"]),
            }

        step = next((item for item in workflow.steps if item.name == "agent_note"), None)
        if step is None:
            raise CourseServiceError("WORKFLOW_INVALID", "Agent 交接步骤记录丢失。", status=409)
        if checked["ready"]:
            artifact = dict(step.artifact)
            artifact.update({"validated": True, "notes_name": checked["notes_name"], "timeline_name": checked.get("timeline_name")})
            self.storage.upsert_step(
                workflow_id=workflow.id, name=step.name, ordinal=step.ordinal,
                state="succeeded", artifact=artifact, error_code=None, error={},
            )
            updated = self.storage.update_workflow_state(workflow.id, "succeeded")
            if self.reader_service is not None:
                try:
                    if revision and revision.public_settings.get("reader", {}).get("auto_update", True):
                        self.reader_service.request_build()
                except Exception:
                    pass
            return {
                "ready": True,
                "message": "笔记与时间轴文件通过课次身份、格式和时间范围检查；内容仍需人工复核。",
                "workflow": updated or workflow,
            }
        self.storage.upsert_step(
            workflow_id=workflow.id, name=step.name, ordinal=step.ordinal,
            state="waiting_agent", artifact=step.artifact,
            error_code="AGENT_OUTPUT_PENDING",
            error={"message": "尚未找到通过校验的本课次成果：" + "、".join(checked.get("missing", []))},
        )
        return {
            "ready": False,
            "message": "Agent 任务仍在等待；指令复制或保存不会被视为完成。",
            "workflow": self.storage.get_workflow(workflow.id) or workflow,
        }

    @staticmethod
    def _verify_files(course_id: str, sub_id: str, style: str, include_timeline: bool) -> dict[str, Any]:
        raw = agent_bridge.find_lecture_dir(course_id, sub_id)
        if raw is None or not raw.is_dir():
            return {"ready": False, "missing": ["当前课次资料目录"]}
        manifest_path = raw / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ready": False, "missing": ["当前课次 manifest"]}
        if not isinstance(manifest, dict) or str(manifest.get("course_id") or "") != course_id or str(manifest.get("sub_id") or "") != sub_id:
            return {"ready": False, "missing": ["与当前目标匹配的课次身份"]}
        base = f"{course_id}_{sub_id}"
        note_suffix = "notes" if style == "standard" else "dialogue"
        note_path = raw / f"{base}_{note_suffix}.md"
        if note_path.is_symlink() or not note_path.is_file():
            return {"ready": False, "missing": ["本课次学习笔记"]}
        try:
            note_text = note_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            note_text = ""
        if len(note_text) < 20 or re.match(r"(?i)\s*(?:todo|待生成|占位|暂无内容)", note_text):
            return {"ready": False, "missing": ["非空且非占位学习笔记"]}

        result: dict[str, Any] = {"ready": True, "notes_name": note_path.name, "timeline_name": None}
        if include_timeline:
            timeline_path = raw / f"{base}_timeline.txt"
            if timeline_path.is_symlink() or not timeline_path.is_file():
                return {"ready": False, "missing": ["本课次时间轴"]}
            try:
                timeline_text = timeline_path.read_text(encoding="utf-8")
                entries = _parse_timeline_text(timeline_text)
                artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
                raw_srt = artifacts.get("subtitle_srt") or artifacts.get("transcript_srt") or f"{base}.srt"
                raw_srt_path = Path(str(raw_srt))
                srt_candidate = raw / raw_srt_path if not raw_srt_path.is_absolute() else raw_srt_path
                if srt_candidate.is_symlink():
                    return {"ready": False, "missing": ["安全的字幕文件"]}
                srt_path = srt_candidate.resolve(strict=True)
                srt_path.relative_to(raw.resolve())
                if not srt_path.is_file():
                    return {"ready": False, "missing": ["可核验的 SRT 字幕时间范围"]}
                from .note_service import _parse_srt
                transcript_meta = artifacts.get("transcript_meta") or str(srt_path.with_suffix(".json"))
                meta_input = Path(str(transcript_meta))
                meta_candidate = raw / meta_input if not meta_input.is_absolute() else meta_input
                if meta_candidate.is_symlink():
                    return {"ready": False, "missing": ["安全的字幕完成记录"]}
                meta_path = meta_candidate.resolve(strict=True)
                meta_path.relative_to(raw.resolve())
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                hashes = meta.get("artifact_sha256") if isinstance(meta, dict) else None
                if (
                    not isinstance(meta, dict) or meta.get("complete") is not True
                    or str(meta.get("course_id") or "") != course_id
                    or str(meta.get("sub_id") or "") != sub_id
                    or not isinstance(hashes, dict)
                    or hashes.get("subtitle_srt") != hashlib.sha256(srt_path.read_bytes()).hexdigest()
                ):
                    return {"ready": False, "missing": ["与当前课次匹配的完整字幕记录"]}
                cues = _parse_srt(srt_path.read_text(encoding="utf-8-sig"))
                duration = cues[-1].end_ms / 1000
                previous_end = -1
                for entry in entries:
                    if entry.start_sec < previous_end or entry.end_sec > duration + 1:
                        return {"ready": False, "missing": ["单调且未超出字幕的时间轴"]}
                    previous_end = entry.end_sec
            except (OSError, UnicodeError, ValueError, TypeError):
                return {"ready": False, "missing": ["格式正确且未超出字幕的时间轴"]}
            result["timeline_name"] = timeline_path.name
        return result

    def _get_owned_workflow(self, workflow_id: str) -> WorkflowRecord:
        account_ids = [self.courses.local_account_id]
        account = self.courses._ensure_platform_account()
        if account and account.id not in account_ids:
            account_ids.append(account.id)
        for account_id in account_ids:
            workflow = self.storage.get_workflow_for_account(workflow_id, account_id)
            if workflow:
                return workflow
        raise KeyError(workflow_id)


def _validate_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or not (8 <= len(value) <= 128) or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("缺少有效的 Idempotency-Key 请求头")
