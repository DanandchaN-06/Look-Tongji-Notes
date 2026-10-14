"""Create idempotent, persistent workflows around the existing safe CLI runner."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Any, Callable

from .artifact_service import ArtifactService
from .contracts import WorkflowRecord
from .course_service import CourseService, CourseServiceError
from .runner import TaskManager, build_argv
from .settings_service import SettingsService
from .storage import ConsoleStorage, IdempotencyConflict


OPERATIONS = {"transcribe": "只取字幕", "slide": "只取课件"}


def probe_process(pid: int) -> bool | None:
    """Return whether a PID may still exist; never signal or terminate it."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


class WorkflowService:
    def __init__(
        self,
        storage: ConsoleStorage,
        courses: CourseService,
        settings: SettingsService,
        runner: TaskManager,
        *,
        log_root: Path | None = None,
        artifact_validator: Callable[[str, str, str], bool] | None = None,
        process_probe: Callable[[int], bool | None] = probe_process,
        poll_seconds: float = 0.1,
    ) -> None:
        self.storage = storage
        self.courses = courses
        self.settings = settings
        self.runner = runner
        self.log_root = log_root or (storage.db_path.parent / "workflow-logs")
        self.artifact_validator = artifact_validator or self._validate_output
        self.process_probe = process_probe
        self.poll_seconds = max(0.01, poll_seconds)
        self.reader_service = None
        self._create_lock = threading.Lock()
        self._monitor_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._monitor_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="workflow-monitor")
        self._monitor_futures: set[Future] = set()

    def create(self, payload: dict[str, Any], idempotency_key: str) -> tuple[WorkflowRecord, bool]:
        if not isinstance(payload, dict) or set(payload) != {"lecture_id", "operation"}:
            raise ValueError("任务请求只接受 lecture_id 和 operation")
        lecture_id = payload.get("lecture_id")
        operation = payload.get("operation")
        if not isinstance(lecture_id, str) or not lecture_id.strip() or len(lecture_id) > 128:
            raise ValueError("必须选择一个明确课次")
        if operation not in OPERATIONS:
            raise ValueError("当前只支持只取字幕或只取课件")
        if not isinstance(idempotency_key, str) or not (8 <= len(idempotency_key) <= 128) or any(ord(c) < 33 or ord(c) > 126 for c in idempotency_key):
            raise ValueError("缺少有效的 Idempotency-Key 请求头")

        with self._create_lock:
            course, lecture = self.courses.get_owned_lecture(lecture_id.strip())
            if course.identity_kind != "platform" or not course.platform_course_id or lecture.identity_kind != "platform" or not lecture.platform_sub_id:
                raise CourseServiceError("NOT_SUPPORTED", "导入课次还没有平台身份，暂不能直接采集。", status=409)
            if lecture.playback_available is False:
                raise CourseServiceError("NOT_PROVIDED", "平台没有为这个课次提供回放。", status=409)

            settings = self.settings.get()
            settings.pop("revision_id", None)
            params: dict[str, Any] = {
                "course_id": course.platform_course_id,
                "sub_id": lecture.platform_sub_id,
                "defer_site_build": True,
            }
            if operation == "slide":
                slides = settings["slides"]
                params.update({
                    "concurrency": slides["concurrency"],
                    "retries": slides["retries"],
                    "timeout": slides["timeout_seconds"],
                })
            argv = build_argv(operation, params)
            target = {
                "course_id": course.id,
                "schedule_account_id": course.account_id,
                "course_title": course.title,
                "lecture_id": lecture.id,
                "lecture_title": lecture.title,
                "class_date": lecture.class_date,
                "platform_course_id": course.platform_course_id,
                "platform_sub_id": lecture.platform_sub_id,
                "source": course.source,
                "operation_label": OPERATIONS[operation],
                "cli_kind": operation,
                "cli_args": argv,
                "runner_params": params,
                "execute_settings": {
                    "slides": settings["slides"] if operation == "slide" else {},
                    "tasks": settings["tasks"],
                },
            }
            request_hash = hashlib.sha256(json.dumps(
                {"lecture_id": lecture.id, "operation": operation},
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            account_id = course.account_id
            workflow, created = self.storage.create_idempotent_workflow(
                account_id=account_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                lecture_id=lecture.id,
                operation=operation,
                settings_snapshot=settings,
                target=target,
            )
            if not created:
                return workflow, False

            step = self.storage.upsert_step(
                workflow_id=workflow.id, name=operation, ordinal=0, state="queued",
                artifact={"expected": "subtitle_srt" if operation == "transcribe" else "slide_index"},
            )
            log_path = self.log_root / workflow.id / "attempt-1.log"
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.touch(exist_ok=True)
            except OSError:
                self.storage.upsert_step(
                    workflow_id=workflow.id, name=operation, ordinal=0, state="failed",
                    artifact={"expected": "subtitle_srt" if operation == "transcribe" else "slide_index"},
                    error_code="LOG_STORAGE_FAILED", error={"message": "无法创建任务日志文件。"},
                )
                self.storage.update_workflow_state(workflow.id, "failed")
                return self.storage.get_workflow(workflow.id), True  # type: ignore[return-value]
            attempt = self.storage.create_attempt(step_id=step.id, state="running", log_path=str(log_path))
            try:
                task = self.runner.start(
                    operation, params, account_id=account_id,
                    parallelism=int(settings.get("tasks", {}).get("course_parallelism", 2)),
                )
            except Exception:
                self.storage.update_attempt(
                    attempt.id, state="failed", exit_code=-1, progress={},
                    error_code="RUNNER_START_FAILED", error={"message": "无法启动采集任务。", "retryable": False},
                )
                self.storage.upsert_step(
                    workflow_id=workflow.id, name=operation, ordinal=0, state="failed",
                    artifact={"expected": "subtitle_srt" if operation == "transcribe" else "slide_index"},
                    error_code="RUNNER_START_FAILED", error={"message": "无法启动采集任务。"},
                )
                self.storage.update_workflow_state(workflow.id, "failed")
                return self.storage.get_workflow(workflow.id), True  # type: ignore[return-value]
            self.storage.update_attempt(attempt.id, state="running", progress={}, runner_task_id=task.id)
            try:
                self._submit_monitor(workflow.id, operation, course.platform_course_id, lecture.platform_sub_id, attempt.id, task)
            except RuntimeError:
                self.runner.cancel(task.id)
                self.storage.update_attempt(
                    attempt.id, state="failed", exit_code=-1, progress={},
                    error_code="MONITOR_START_FAILED", error={"message": "任务监控无法启动。", "retryable": False},
                )
                self.storage.upsert_step(
                    workflow_id=workflow.id, name=operation, ordinal=0, state="failed",
                    artifact={"expected": "subtitle_srt" if operation == "transcribe" else "slide_index"},
                    error_code="MONITOR_START_FAILED", error={"message": "任务监控无法启动，已请求停止采集。"},
                )
                self.storage.update_workflow_state(workflow.id, "failed")
            return self.storage.get_workflow(workflow.id), True  # type: ignore[return-value]

    def list(self, *, limit: int = 100) -> list[WorkflowRecord]:
        account_ids = [self.courses.local_account_id]
        account = self.courses._ensure_platform_account()
        if account and account.id not in account_ids:
            account_ids.append(account.id)
        unique: dict[str, WorkflowRecord] = {}
        for account_id in account_ids:
            for workflow in self.storage.list_workflows_for_account(account_id, limit=limit):
                unique[workflow.id] = workflow
        records = sorted(unique.values(), key=lambda item: (item.created_at, item.id), reverse=True)
        return records[:max(1, min(int(limit), 500))]

    def get(self, workflow_id: str) -> WorkflowRecord | None:
        for account_id in self._account_ids():
            workflow = self.storage.get_workflow_for_account(workflow_id, account_id)
            if workflow:
                return workflow
        return None

    def read_log(self, workflow_id: str, attempt_id: str, *, offset: int = 0, max_bytes: int = 64 * 1024):
        workflow = self.get(workflow_id)
        if workflow is None:
            raise KeyError(workflow_id)
        attempt = next((a for step in workflow.steps for a in step.attempts if a.id == attempt_id), None)
        if attempt is None or not attempt.log_path:
            raise KeyError(attempt_id)
        return self.storage.read_log_offset(attempt.log_path, offset=offset, max_bytes=max_bytes)

    def cancel(self, workflow_id: str) -> WorkflowRecord:
        with self._create_lock:
            workflow = self.get(workflow_id)
            if workflow is None:
                raise KeyError(workflow_id)
            if workflow.state not in {"queued", "running"}:
                if workflow.state == "blocked":
                    raise CourseServiceError("PROCESS_MAY_BE_RUNNING", "上次进程状态未确认，取消不会触碰外部进程。", status=409)
                return workflow
            attempt = self._latest_attempt(workflow)
            if attempt is None or not attempt.runner_task_id:
                raise CourseServiceError("PROCESS_STATUS_UNKNOWN", "无法确认此任务对应的执行进程，暂不能发送取消。", status=409)
            if attempt.state != "running":
                # The monitor may be in a bounded backoff between attempts.
                self.storage.update_workflow_state(workflow.id, workflow.state, cancel_requested=True)
                return self.storage.get_workflow(workflow.id) or workflow
            task = self.runner.get(attempt.runner_task_id)
            if task is None:
                self.storage.update_workflow_state(workflow.id, "blocked")
                if workflow.steps:
                    step = workflow.steps[-1]
                    self.storage.upsert_step(
                        workflow_id=workflow.id, name=step.name, ordinal=step.ordinal, state="blocked",
                        artifact=step.artifact, error_code="PROCESS_STATUS_UNKNOWN",
                        error={"message": "任务对应的进程不在当前控制台中；为避免重复写入，暂不执行取消或重试。"},
                    )
                raise CourseServiceError("PROCESS_STATUS_UNKNOWN", "任务对应的进程不在当前控制台中，已转为需处理。", status=409)
            self.storage.update_workflow_state(workflow.id, workflow.state, cancel_requested=True)
            self.runner.cancel(attempt.runner_task_id)
            return self.storage.get_workflow(workflow.id) or workflow

    def retry(self, workflow_id: str, idempotency_key: str) -> tuple[WorkflowRecord, bool]:
        if not isinstance(idempotency_key, str) or not (8 <= len(idempotency_key) <= 128) or any(ord(c) < 33 or ord(c) > 126 for c in idempotency_key):
            raise ValueError("缺少有效的 Idempotency-Key 请求头")
        with self._create_lock:
            workflow = self.get(workflow_id)
            if workflow is None:
                raise KeyError(workflow_id)
            course, lecture = self.courses.get_owned_lecture(workflow.lecture_id)
            retry_hash = hashlib.sha256(f"{workflow.id}\0retry".encode("utf-8")).hexdigest()
            retry_storage_key = f"retry:{workflow.id}:{hashlib.sha256(idempotency_key.encode('ascii')).hexdigest()}"
            previous_attempt = self.storage.find_idempotent_attempt(
                account_id=course.account_id, idempotency_key=retry_storage_key, request_hash=retry_hash
            )
            if previous_attempt:
                return self.storage.get_workflow(workflow.id) or workflow, False
            if workflow.state == "blocked":
                raise CourseServiceError("PROCESS_MAY_BE_RUNNING", "上次进程状态未确认，不能重复启动该课次。", status=409)
            if workflow.state in {"queued", "running"}:
                raise CourseServiceError("WORKFLOW_ACTIVE", "任务仍在运行或排队，请等待结束后再恢复。", status=409)
            if workflow.state not in {"failed", "cancelled", "interrupted"}:
                raise CourseServiceError("WORKFLOW_NOT_RETRYABLE", "此任务当前状态不支持重试。", status=409)
            if not workflow.steps:
                raise CourseServiceError("WORKFLOW_INVALID", "任务没有可恢复的执行步骤。", status=409)

            step = workflow.steps[-1]
            operation = workflow.operation
            target = workflow.target
            params = target.get("runner_params")
            if not isinstance(params, dict):
                params = {
                    "course_id": target.get("platform_course_id"),
                    "sub_id": target.get("platform_sub_id"),
                }
                settings_revision = self.storage.get_settings_revision(workflow.settings_revision_id or "")
                if operation == "slide" and settings_revision:
                    slides = settings_revision.public_settings.get("slides", {})
                    params.update({
                        "concurrency": slides.get("concurrency", 4),
                        "retries": slides.get("retries", 3),
                        "timeout": slides.get("timeout_seconds", 30),
                    })
            if not isinstance(params.get("course_id"), str) or not isinstance(params.get("sub_id"), str):
                raise CourseServiceError("WORKFLOW_INVALID", "任务快照中缺少平台课程或课次 ID。", status=409)
            argv = build_argv(operation, params)
            attempt_number = max((item.number for item in step.attempts), default=0) + 1
            log_path = self.log_root / workflow.id / f"attempt-{attempt_number}.log"
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.touch(exist_ok=True)
            except OSError as error:
                raise CourseServiceError("DISK_WRITE_FAILED", "无法创建新的任务日志。", status=500) from error
            attempt, created = self.storage.create_idempotent_retry_attempt(
                account_id=course.account_id,
                idempotency_key=retry_storage_key,
                request_hash=retry_hash,
                workflow_id=workflow.id,
                step_id=step.id,
                log_path=str(log_path),
            )
            if not created:
                return self.storage.get_workflow(workflow.id) or workflow, False

            if self.artifact_validator(operation, str(params["course_id"]), str(params["sub_id"])):
                with log_path.open("a", encoding="utf-8") as stream:
                    stream.write("[console] 复用已校验的课次成果，没有重新下载。\n")
                self.storage.update_attempt(
                    attempt.id, state="succeeded", exit_code=0,
                    progress={"label": "复用已校验成果"},
                )
                self.storage.upsert_step(
                    workflow_id=workflow.id, name=step.name, ordinal=step.ordinal,
                    state="succeeded", artifact={"validated": True, "reused": True},
                )
                self.storage.update_workflow_state(workflow.id, "succeeded", cancel_requested=False)
                self._request_reader_update()
                return self.storage.get_workflow(workflow.id) or workflow, True

            try:
                task = self.runner.start(
                    operation, params, account_id=course.account_id,
                    parallelism=int(workflow.target.get("execute_settings", {}).get("tasks", {}).get("course_parallelism", 2)),
                )
            except Exception:
                self.storage.update_attempt(
                    attempt.id, state="failed", exit_code=-1, progress={},
                    error_code="RUNNER_START_FAILED", error={"message": "无法启动恢复任务。", "retryable": False},
                )
                self.storage.upsert_step(
                    workflow_id=workflow.id, name=step.name, ordinal=step.ordinal, state="failed",
                    error_code="RUNNER_START_FAILED", error={"message": "无法启动恢复任务。"},
                )
                self.storage.update_workflow_state(workflow.id, "failed")
                return self.storage.get_workflow(workflow.id) or workflow, True
            self.storage.update_attempt(attempt.id, state="running", progress={}, runner_task_id=task.id)
            try:
                self._submit_monitor(workflow.id, operation, str(params["course_id"]), str(params["sub_id"]), attempt.id, task)
            except RuntimeError:
                self.runner.cancel(task.id)
                self.storage.update_attempt(
                    attempt.id, state="failed", exit_code=-1, progress={},
                    error_code="MONITOR_START_FAILED", error={"message": "恢复任务监控无法启动。", "retryable": False},
                )
                self.storage.upsert_step(
                    workflow_id=workflow.id, name=step.name, ordinal=step.ordinal, state="failed",
                    error_code="MONITOR_START_FAILED", error={"message": "恢复任务监控无法启动，已请求停止。"},
                )
                self.storage.update_workflow_state(workflow.id, "failed")
            return self.storage.get_workflow(workflow.id) or workflow, True

    @staticmethod
    def _latest_attempt(workflow: WorkflowRecord):
        return max((attempt for step in workflow.steps for attempt in step.attempts), key=lambda item: item.number, default=None)

    def recover_orphans(self) -> list[str]:
        recovered: list[str] = []
        for workflow in self.storage.list_active_workflows():
            attempt = self._latest_attempt(workflow)
            if attempt is None:
                continue
            step = self._latest_step(workflow)
            if workflow.state == "queued" and attempt.state != "running":
                if step:
                    self.storage.upsert_step(
                        workflow_id=workflow.id, name=step.name, ordinal=step.ordinal, state="interrupted",
                        artifact=step.artifact, error_code="INTERRUPTED",
                        error={"message": "服务在两次执行之间关闭；可手动恢复任务。"},
                    )
                self.storage.update_workflow_state(workflow.id, "interrupted", cancel_requested=False)
                recovered.append(workflow.id)
                continue
            if attempt.state != "running":
                if workflow.state == "running" and attempt.state in {"succeeded", "failed", "cancelled", "interrupted"}:
                    final_state = attempt.state
                    if attempt.state == "succeeded":
                        params = workflow.target.get("runner_params", {})
                        valid = self.artifact_validator(
                            workflow.operation,
                            str(params.get("course_id", workflow.target.get("platform_course_id", ""))),
                            str(params.get("sub_id", workflow.target.get("platform_sub_id", ""))),
                        )
                        if not valid:
                            final_state = "failed"
                            self.storage.update_attempt(
                                attempt.id, state="failed", exit_code=attempt.exit_code,
                                progress=attempt.progress, error_code="INVALID_ARTIFACT",
                                error={"message": "重启恢复时未找到通过校验的成果。", "retryable": False},
                            )
                    if step:
                        self.storage.upsert_step(
                            workflow_id=workflow.id, name=step.name, ordinal=step.ordinal,
                            state=final_state, artifact=step.artifact,
                            error_code=attempt.error_code or ("INVALID_ARTIFACT" if final_state == "failed" else None),
                            error=attempt.error or ({"message": "重启恢复时未找到通过校验的成果。"} if final_state == "failed" else {}),
                        )
                    self.storage.update_workflow_state(workflow.id, final_state, cancel_requested=False)
                    recovered.append(workflow.id)
                continue
            if attempt.process_pid is None:
                live = None
            else:
                live = self.process_probe(attempt.process_pid)
            if live is False:
                self.storage.update_attempt(attempt.id, state="interrupted", exit_code=None, progress=attempt.progress)
                if step:
                    self.storage.upsert_step(
                        workflow_id=workflow.id, name=step.name, ordinal=step.ordinal, state="interrupted",
                        artifact=step.artifact, error_code="INTERRUPTED",
                        error={"message": "服务重启后未发现此任务的执行进程；可创建新的恢复 attempt。"},
                    )
                self.storage.update_workflow_state(workflow.id, "interrupted", cancel_requested=False)
            else:
                message = "旧任务进程仍可能存活，需人工确认；系统不会自动重启或终止它。" if live else "旧任务进程状态无法确认，需人工处理后才能重试。"
                if step:
                    self.storage.upsert_step(
                        workflow_id=workflow.id, name=step.name, ordinal=step.ordinal, state="blocked",
                        artifact=step.artifact, error_code="PROCESS_MAY_BE_RUNNING" if live else "PROCESS_STATUS_UNKNOWN",
                        error={"message": message},
                    )
                self.storage.update_workflow_state(workflow.id, "blocked")
            recovered.append(workflow.id)
        return recovered

    @staticmethod
    def _latest_step(workflow: WorkflowRecord):
        return max(workflow.steps, key=lambda item: item.ordinal, default=None)

    def _account_ids(self) -> list[str]:
        account_ids = [self.courses.local_account_id]
        account = self.courses._ensure_platform_account()
        if account and account.id not in account_ids:
            account_ids.append(account.id)
        return account_ids

    def shutdown(self, timeout: float = 5.0) -> None:
        """Wait for workflow monitors after TaskManager has stopped its owned children."""
        self._shutdown.set()
        deadline = time.monotonic() + max(0.0, timeout)
        with self._monitor_lock:
            monitors = list(self._monitor_futures)
        for monitor in monitors:
            try:
                monitor.result(timeout=max(0.0, deadline - time.monotonic()))
            except TimeoutError:
                pass
            except Exception:
                pass
        self._monitor_pool.shutdown(wait=False, cancel_futures=False)

    def _submit_monitor(self, *args: Any) -> None:
        future = self._monitor_pool.submit(self._monitor, *args)
        with self._monitor_lock:
            self._monitor_futures.add(future)
        future.add_done_callback(self._discard_monitor)

    def _discard_monitor(self, future: Future) -> None:
        with self._monitor_lock:
            self._monitor_futures.discard(future)

    def _monitor(self, workflow_id: str, operation: str, course_id: str, lecture_id: str, attempt_id: str, task: Any) -> None:
        try:
            current_attempt_id, current_task = attempt_id, task
            while True:
                self._monitor_task(workflow_id, operation, course_id, lecture_id, current_attempt_id, current_task)
                if self._shutdown.is_set():
                    break
                workflow = self.storage.get_workflow(workflow_id)
                attempt = self._latest_attempt(workflow) if workflow else None
                if not workflow or not attempt or workflow.state != "failed" or not attempt.error.get("retryable"):
                    break
                params = workflow.target.get("runner_params")
                if isinstance(params, dict) and self.artifact_validator(
                    operation, str(params.get("course_id", "")), str(params.get("sub_id", ""))
                ):
                    number = attempt.number + 1
                    log_path = self.log_root / workflow_id / f"attempt-{number}.log"
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a", encoding="utf-8") as stream:
                        stream.write("[console] 复用已校验的课次成果，没有重复下载。\n")
                    self.storage.create_attempt(
                        step_id=attempt.step_id, state="succeeded", exit_code=0,
                        progress={"label": "复用已校验成果"}, log_path=str(log_path),
                    )
                    self.storage.upsert_step(
                        workflow_id=workflow_id, name=operation, ordinal=0, state="succeeded",
                        artifact={"validated": True, "reused": True},
                    )
                    self.storage.update_workflow_state(workflow_id, "succeeded", cancel_requested=False)
                    break
                settings = workflow.target.get("execute_settings", {}).get("tasks", {})
                try:
                    max_attempts = max(1, min(3, int(settings.get("retry_limit", 3))))
                except (TypeError, ValueError):
                    max_attempts = 3
                if attempt.number >= max_attempts:
                    break
                delay = min(2.0, 0.5 * (2 ** (attempt.number - 1)))
                self.storage.update_workflow_state(workflow_id, "queued")
                step = self._latest_step(workflow)
                if step:
                    self.storage.upsert_step(
                        workflow_id=workflow_id, name=step.name, ordinal=step.ordinal, state="queued",
                        artifact={"retry_in_seconds": delay}, error_code=attempt.error_code,
                        error={"message": f"{attempt.error.get('message', '暂时失败')}；{delay:g} 秒后自动尝试第 {attempt.number + 1} 次。", "retryable": True},
                    )
                cancelled = False
                interrupted = False
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    current = self.storage.get_workflow(workflow_id)
                    if current is None or current.state != "queued":
                        interrupted = True
                        break
                    if current.cancel_requested:
                        cancelled = True
                        break
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                if cancelled:
                    if step:
                        self.storage.upsert_step(
                            workflow_id=workflow_id, name=step.name, ordinal=step.ordinal, state="cancelled",
                            artifact=step.artifact, error_code="CANCELLED", error={"message": "自动重试已取消。"},
                        )
                    self.storage.update_workflow_state(workflow_id, "cancelled", cancel_requested=False)
                    break
                if interrupted:
                    break
                if not isinstance(params, dict):
                    self.storage.update_workflow_state(workflow_id, "failed")
                    if step:
                        self.storage.upsert_step(
                            workflow_id=workflow_id, name=step.name, ordinal=step.ordinal, state="failed",
                            artifact=step.artifact, error_code="WORKFLOW_INVALID",
                            error={"message": "任务快照缺少自动恢复所需的执行参数。"},
                        )
                    break
                next_number = attempt.number + 1
                log_path = self.log_root / workflow_id / f"attempt-{next_number}.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.touch(exist_ok=True)
                next_attempt = self.storage.create_attempt(
                    step_id=step.id if step else attempt.step_id, state="running", log_path=str(log_path)
                )
                try:
                    current_task = self.runner.start(
                        operation, params, account_id=str(workflow.target.get("schedule_account_id") or ""),
                        parallelism=int(settings.get("course_parallelism", 2)),
                    )
                except Exception:
                    self.storage.update_attempt(
                        next_attempt.id, state="failed", exit_code=-1, progress={},
                        error_code="RUNNER_START_FAILED", error={"message": "无法启动自动恢复任务。", "retryable": False},
                    )
                    if step:
                        self.storage.upsert_step(
                            workflow_id=workflow_id, name=step.name, ordinal=step.ordinal, state="failed",
                            artifact=step.artifact, error_code="RUNNER_START_FAILED",
                            error={"message": "无法启动自动恢复任务。"},
                        )
                    self.storage.update_workflow_state(workflow_id, "failed")
                    break
                current_attempt_id = next_attempt.id
                self.storage.update_attempt(
                    current_attempt_id, state="running", progress={}, runner_task_id=current_task.id
                )
        except Exception:
            try:
                failed_task = locals().get("current_task", task)
                failed_attempt_id = locals().get("current_attempt_id", attempt_id)
                self.runner.cancel(failed_task.id)
                self.storage.update_attempt(
                    failed_attempt_id, state="failed", exit_code=None, progress={},
                    error_code="MONITOR_FAILED", error={"message": "任务监控中断，已请求停止采集。", "retryable": False},
                )
                self.storage.upsert_step(
                    workflow_id=workflow_id, name=operation, ordinal=0, state="failed",
                    error_code="MONITOR_FAILED", error={"message": "任务监控中断，已请求停止采集。"},
                )
                self.storage.update_workflow_state(workflow_id, "failed")
            except Exception:
                pass
        finally:
            self.storage.close_thread_connection()

    def _monitor_task(self, workflow_id: str, operation: str, course_id: str, lecture_id: str, attempt_id: str, task: Any) -> None:
        offset = 0
        workflow = self.storage.get_workflow(workflow_id)
        record = next((
            item for step in (workflow.steps if workflow else [])
            for item in step.attempts if item.id == attempt_id
        ), None)
        log_path = Path(record.log_path) if record and record.log_path else self.log_root / workflow_id / "attempt-1.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        last_saved_state = None
        last_saved_progress = None
        last_saved_at = 0.0
        failure_lines: list[str] = []
        while True:
            snapshot = task.snapshot()
            tail = task.tail(offset)
            lines = tail.get("lines", [])
            if lines:
                failure_lines.extend(str(line) for line in lines)
                failure_lines = failure_lines[-100:]
                with log_path.open("a", encoding="utf-8", newline="\n") as stream:
                    if tail.get("truncated"):
                        stream.write("[console] 早期内存日志已裁剪；后续偏移保持绝对位置。\n")
                    stream.write("".join(f"{line}\n" for line in lines))
                    stream.flush()
            offset = int(tail.get("offset", offset))
            state = snapshot.get("status", "running")
            progress = snapshot.get("progress") or {}
            now = time.monotonic()
            if state != last_saved_state or progress != last_saved_progress or now - last_saved_at >= 1.0:
                persisted_state = "queued" if state == "queued" else "running"
                self.storage.update_workflow_state(workflow_id, persisted_state)
                process = getattr(task, "_proc", None)
                process_pid = getattr(process, "pid", None)
                self.storage.update_attempt(
                    attempt_id,
                    state="running",
                    progress=progress,
                    runner_task_id=getattr(task, "id", None),
                    process_pid=process_pid if isinstance(process_pid, int) else None,
                )
                self.storage.upsert_step(
                    workflow_id=workflow_id, name=operation, ordinal=0, state=persisted_state,
                    artifact={"progress": progress},
                )
                last_saved_state, last_saved_progress, last_saved_at = state, progress, now
            if state not in {"queued", "running"}:
                break
            time.sleep(self.poll_seconds)

        succeeded = state == "succeeded" and self.artifact_validator(operation, course_id, lecture_id)
        error: dict[str, Any] = {}
        if succeeded:
            final_state, code, message = "succeeded", None, ""
        elif state == "succeeded":
            final_state, code, message = "failed", "INVALID_ARTIFACT", "任务进程成功退出，但没有发现通过校验的目标成果。"
            error = {"message": message, "retryable": False}
        else:
            if state == "cancelled":
                final_state, code, message = "cancelled", "CANCELLED", "任务已取消。"
                error = {}
            else:
                final_state = "failed"
                failure_text = "\n".join([str(snapshot.get("error") or ""), *failure_lines])
                code, message, retryable = classify_runner_failure(operation, failure_text)
                error = {"message": message, "retryable": retryable}
        exit_code = snapshot.get("returncode")
        self.storage.update_attempt(
            attempt_id, state=final_state, exit_code=exit_code,
            progress=snapshot.get("progress") or {}, error_code=code, error=error,
        )
        self.storage.upsert_step(
            workflow_id=workflow_id, name=operation, ordinal=0, state=final_state,
            artifact={"validated": succeeded}, error_code=code,
            error={"message": message} if state == "succeeded" and message else error,
        )
        self.storage.update_workflow_state(workflow_id, final_state, cancel_requested=False)
        if final_state == "succeeded":
            self._request_reader_update()

    def _request_reader_update(self) -> None:
        if self.reader_service is None:
            return
        try:
            if self.settings.get().get("reader", {}).get("auto_update", True):
                self.reader_service.request_build()
        except Exception:
            # The lecture task remains successful; reader status exposes the
            # independent build failure and a manual retry remains available.
            return

    def _validate_output(self, operation: str, course_id: str, lecture_id: str) -> bool:
        found = self.courses.find_local_lecture(course_id, lecture_id)
        expected = "transcript" if operation == "transcribe" else "slides"
        if found is not None:
            course, lecture = found
            result = ArtifactService(self.courses.workspace_root).for_lecture(course, lecture)
            if any(item["kind"] == expected and item["state"] == "available" for item in result["items"]):
                return True
        return ArtifactService(self.courses.workspace_root).has_identity_artifact(
            course_id, lecture_id, expected
        )


def classify_runner_failure(operation: str, detail: str) -> tuple[str, str, bool]:
    """Map only explicit, known CLI diagnostics; unknown failures stay unknown."""
    status = re.search(r"\bHTTP\s+(401|403|412|429|5\d\d)\b", detail, re.IGNORECASE)
    if status:
        code = status.group(1)
        if code in {"401", "403"}:
            return "AUTH_REQUIRED", "平台拒绝了请求（认证或权限无效）；请检查设置后手动恢复。", False
        if code == "412" and operation == "transcribe":
            return "ASR_REJECTED", "识别服务拒绝请求（HTTP 412）；不会自动重试，可检查服务状态后手动恢复。", False
        return "NETWORK_TEMPORARY", f"外部服务暂时不可用（HTTP {code}）。", code in {"429", "500", "502", "503", "504"}
    if re.search(r"\b(TimeoutError|ConnectionError|ReadTimeout|ConnectTimeout)\b", detail):
        return "NETWORK_TEMPORARY", "网络连接暂时失败，可在网络恢复后重试。", True
    if operation == "transcribe" and re.search(r"\bNO_AUDIO\b|无音轨", detail, re.IGNORECASE):
        return "NO_AUDIO", "此课次没有可识别的音轨。", False
    if operation == "slide" and re.search(r"\bSLIDES_UNAVAILABLE\b|无课件|没有可用截图", detail, re.IGNORECASE):
        return "SLIDES_UNAVAILABLE", "此课次没有可用课件截图。", False
    return "UNEXPECTED_ERROR", "采集任务失败；可查看脱敏日志。", False
