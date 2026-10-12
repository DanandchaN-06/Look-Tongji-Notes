"""Thin HTTP contracts for the first V2 course endpoints."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from .artifact_service import ArtifactService
from .agent_service import AgentHandoffService
from .course_service import CourseService, CourseServiceError
from .contracts import CourseRecord, LectureRecord
from .settings_service import SettingsService
from .reader_service import ReaderBuildService
from .migration import MigrationService, StaleMigrationPreview
from .storage import IdempotencyConflict
from .workflow_service import WorkflowService


_COURSE_LECTURES = re.compile(r"^/api/v2/courses/([A-Za-z0-9_-]+)/lectures$")
_LECTURE_ARTIFACTS = re.compile(r"^/api/v2/courses/([A-Za-z0-9_-]+)/lectures/([A-Za-z0-9_-]+)/artifacts$")
_WORKFLOW_DETAIL = re.compile(r"^/api/v2/workflows/([A-Za-z0-9-]+)$")
_WORKFLOW_ACTION = re.compile(r"^/api/v2/workflows/([A-Za-z0-9-]+)/(retry|cancel)$")
_AGENT_INSTRUCTION = re.compile(r"^/api/v2/workflows/([A-Za-z0-9-]+)/agent-instruction$")
_AGENT_VERIFY = re.compile(r"^/api/v2/workflows/([A-Za-z0-9-]+)/verify-agent$")
_WORKFLOW_LOG = re.compile(r"^/api/v2/workflows/([A-Za-z0-9-]+)/attempts/([A-Za-z0-9-]+)/log$")


def _error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    actions: list[str] | None = None,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "actions": actions or [],
            "detail": detail,
        },
    }


class V2Router:
    def __init__(self, courses: CourseService, settings: SettingsService | None = None, workflows: WorkflowService | None = None, reader_service: ReaderBuildService | None = None, migration_service: MigrationService | None = None) -> None:
        self.courses = courses
        self.settings = settings
        self.workflows = workflows
        self.artifacts = ArtifactService(courses.workspace_root)
        self.reader = reader_service
        self.migrations = migration_service
        if workflows is not None and reader_service is not None:
            workflows.reader_service = self.reader
        self.agents = AgentHandoffService(workflows.storage, courses, settings, self.reader) if workflows is not None and settings is not None else None

    def get(self, path: str, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        try:
            if path == "/api/v2/settings" and self.settings is not None:
                return 200, {"ok": True, "data": self.settings.get()}
            if path == "/api/v2/reader" and self.reader is not None:
                return 200, {"ok": True, "data": self.reader.status()}
            if path == "/api/v2/workflows" and self.workflows is not None:
                return 200, {"ok": True, "data": [self._public_workflow(item) for item in self.workflows.list()]}
            instruction_match = _AGENT_INSTRUCTION.fullmatch(path)
            if instruction_match and self.agents is not None:
                instruction = self.agents.instruction(instruction_match.group(1))
                if not instruction:
                    return 404, _error("NOT_FOUND", "没有找到 Agent 交接指令。")
                return 200, {"ok": True, "data": {"instruction": instruction}}
            log_match = _WORKFLOW_LOG.fullmatch(path)
            if log_match and self.workflows is not None:
                offset = int((query.get("offset") or ["0"])[0])
                data = self.workflows.read_log(*log_match.groups(), offset=offset)
                return 200, {"ok": True, "data": asdict(data)}
            workflow_match = _WORKFLOW_DETAIL.fullmatch(path)
            if workflow_match and self.workflows is not None:
                workflow = self.workflows.get(workflow_match.group(1))
                if workflow is None:
                    return 404, _error("NOT_FOUND", "没有找到这个任务。")
                return 200, {"ok": True, "data": self._public_workflow(workflow)}
            if path == "/api/v2/courses":
                source = (query.get("source") or ["local"])[0]
                keyword = (query.get("q") or [""])[0]
                return 200, {"ok": True, "data": self.courses.list_courses(source=source, query=keyword)}
            artifact_match = _LECTURE_ARTIFACTS.fullmatch(path)
            if artifact_match:
                course_id, lecture_id = artifact_match.groups()
                data = self.courses.list_lectures(course_id)
                lecture_data = next((item for item in data["lectures"] if item["id"] == lecture_id), None)
                lecture = LectureRecord(**lecture_data) if lecture_data else None
                if lecture is None:
                    return 404, _error("NOT_FOUND", "没有找到这个课次。", actions=["open_artifacts"])
                course = CourseRecord(**data["course"])
                return 200, {"ok": True, "data": self.artifacts.for_lecture(course, lecture)}
            match = _COURSE_LECTURES.fullmatch(path)
            if match:
                data = self.courses.list_lectures(match.group(1))
                course = dict(data["course"])
                course.pop("account_id", None)
                lectures = []
                for record in data["lectures"]:
                    item = dict(record)
                    item.pop("manifest_path", None)
                    lectures.append(item)
                return 200, {"ok": True, "data": {"course": course, "lectures": lectures}}
            return 404, _error("NOT_FOUND", "没有找到这个接口。")
        except CourseServiceError as error:
            return error.status, self._service_error(error)
        except KeyError:
            return 404, _error("NOT_FOUND", "没有找到这个任务或日志。")
        except ValueError as error:
            return 400, _error("INVALID_INPUT", str(error))
        except Exception:
            return 500, _error("UNEXPECTED_ERROR", "读取设置失败。")

    def post(self, path: str, body: dict[str, Any], *, idempotency_key: str = "") -> tuple[int, dict[str, Any]]:
        try:
            if path == "/api/v2/reader/build" and self.reader is not None:
                if body:
                    return 400, _error("INVALID_INPUT", "阅读站点构建不接受请求参数。")
                return 202, {"ok": True, "data": self.reader.request_build()}
            if path == "/api/v2/migrations/preview" and self.migrations is not None:
                if body:
                    return 400, _error("INVALID_INPUT", "只读迁移预览不接受请求参数。")
                return 200, {"ok": True, "data": self.migrations.preview()}
            if path == "/api/v2/migrations/apply" and self.migrations is not None:
                if set(body) != {"preview_id"} or not isinstance(body.get("preview_id"), str):
                    return 400, _error("INVALID_INPUT", "请提交一个有效的预览 ID。")
                result = self.migrations.apply(body["preview_id"])
                return 200, {"ok": True, "data": {"preview_id": result["preview_id"], "state": result["state"], "backup_created": bool(result.get("backup_path"))}}
            if path == "/api/v2/reader/open" and self.reader is not None:
                if body:
                    return 400, _error("INVALID_INPUT", "打开阅读页不接受请求参数。")
                return 200, {"ok": True, "data": self.reader.open_reader()}
            action_match = _WORKFLOW_ACTION.fullmatch(path)
            if action_match and self.workflows is not None:
                workflow_id, action = action_match.groups()
                if body:
                    return 400, _error("INVALID_INPUT", "此操作不接受请求参数。")
                if action == "cancel":
                    workflow = self.workflows.cancel(workflow_id)
                    return 200, {"ok": True, "data": self._public_workflow(workflow)}
                workflow, created = self.workflows.retry(workflow_id, idempotency_key)
                return (201 if created else 200), {"ok": True, "data": self._public_workflow(workflow), "created": created}
            verify_match = _AGENT_VERIFY.fullmatch(path)
            if verify_match and self.agents is not None:
                if body:
                    return 400, _error("INVALID_INPUT", "此校验不接受请求参数。")
                result = self.agents.verify(verify_match.group(1))
                result["workflow"] = self._public_workflow(result["workflow"])
                return 200, {"ok": True, "data": result}
            if path == "/api/v2/workflows" and self.workflows is not None:
                if body.get("operation") == "agent_note" and self.agents is not None:
                    handoff_body = {key: body[key] for key in ("lecture_id",) if key in body}
                    workflow, created, instruction = self.agents.create(handoff_body, idempotency_key)
                    return (201 if created else 200), {
                        "ok": True, "data": self._public_workflow(workflow),
                        "created": created, "instruction": instruction,
                    }
                workflow, created = self.workflows.create(body, idempotency_key)
                return (201 if created else 200), {"ok": True, "data": self._public_workflow(workflow), "created": created}
            if path == "/api/v2/settings/test/text_model" and self.settings is not None:
                if body:
                    return 400, _error("INVALID_INPUT", "此测试不接受请求参数。")
                return 200, {"ok": True, "data": self.settings.test_text_model()}
            if path == "/api/v2/settings" and self.settings is not None:
                if not body:
                    return 400, _error("INVALID_INPUT", "至少需要提交一个设置字段。")
                return 200, {"ok": True, "data": self.settings.save(body)}
            if path != "/api/v2/courses/sync":
                return 404, _error("NOT_FOUND", "没有找到这个接口。")
            if body:
                return 400, _error("INVALID_INPUT", "课程同步不接受自定义参数。")
            return 200, {"ok": True, "data": self.courses.sync_courses()}
        except CourseServiceError as error:
            return error.status, self._service_error(error)
        except StaleMigrationPreview as error:
            return 409, _error("STALE_PREVIEW", str(error), actions=["preview_again"])
        except IdempotencyConflict as error:
            return 409, _error("IDEMPOTENCY_CONFLICT", str(error))
        except KeyError:
            return 404, _error("NOT_FOUND", "没有找到这个任务。")
        except ValueError as error:
            return 400, _error("INVALID_INPUT", str(error))
        except Exception:
            return 500, _error("UNEXPECTED_ERROR", "保存设置失败。")

    @staticmethod
    def _public_workflow(workflow: Any) -> dict[str, Any]:
        result = asdict(workflow)
        result.get("target", {}).pop("schedule_account_id", None)
        for step in result.get("steps", []):
            step.get("artifact", {}).pop("instruction", None)
            for attempt in step.get("attempts", []):
                attempt.pop("log_path", None)
                attempt.pop("runner_task_id", None)
                attempt.pop("process_pid", None)
        return result

    @staticmethod
    def _service_error(error: CourseServiceError) -> dict[str, Any]:
        actions = {
            "AUTH_REQUIRED": ["open_settings"],
            "COURSE_SYNC_FAILED": ["retry", "open_settings"],
            "NOT_FOUND": ["open_artifacts"],
        }.get(error.code, [])
        return _error(
            error.code,
            error.message,
            retryable=error.retryable,
            actions=actions,
            detail=error.message,
        )
