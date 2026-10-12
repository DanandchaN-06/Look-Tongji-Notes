from __future__ import annotations

import json
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path

from console.artifact_service import ArtifactService
from console.course_service import CourseService, CourseServiceError
from console.routes_v2 import V2Router
from console.storage import ConsoleStorage, IdempotencyConflict
from console.workflow_service import WorkflowService, classify_runner_failure


class FakeSettings:
    retry_limit = 3

    def get(self):
        return {
            "revision_id": None,
            "slides": {"concurrency": 2, "retries": 3, "timeout_seconds": 30},
            "tasks": {"course_parallelism": 2, "asr_parallelism": 1, "retry_limit": self.retry_limit},
            "note": {"route": "external_agent", "style": "standard", "include_timeline": True},
            "text_model": {"configured": False, "key_configured": False},
        }


class FakeTask:
    def __init__(self):
        self.id = f"task-{id(self)}"
        self.status = "succeeded"
        self.error = ""
        self._proc = None

    def snapshot(self):
        return {
            "status": self.status,
            "returncode": 0 if self.status == "succeeded" else 1 if self.status == "failed" else None,
            "error": self.error,
            "progress": {"label": "任务已结束"},
        }

    def tail(self, offset=0):
        lines = ["synthetic runner started", "synthetic runner finished"]
        return {"offset": len(lines), "lines": lines[offset:], "truncated": False}


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.tasks = {}
        self.cancelled = []
        self.initial_status = "succeeded"
        self.initial_error = ""
        self.outcomes = []
        self.lock = threading.Lock()

    def start(self, kind, params, **_schedule):
        with self.lock:
            self.calls.append((kind, dict(params)))
            task = FakeTask()
            status, error = self.outcomes.pop(0) if self.outcomes else (self.initial_status, self.initial_error)
            task.status = status
            task.error = error
            self.tasks[task.id] = task
        return task

    def get(self, task_id):
        return self.tasks.get(task_id)

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        task = self.tasks.get(task_id)
        if task:
            task.status = "cancelled"


class WorkflowServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.storage = ConsoleStorage(self.root / "db.sqlite3")
        self.account = self.storage.upsert_account(provider="tongji", platform_account_id="synthetic-user")
        self.course = self.storage.upsert_course(
            account_id=self.account.id, platform_course_id="course-101", title="Frozen course title", source="platform"
        )
        self.lecture = self.storage.upsert_lecture(
            course_id=self.course.id, platform_sub_id="lecture-202", title="Frozen lecture title",
            class_date="2026-09-14", playback_available=True,
        )
        self.courses = CourseService(
            self.storage,
            adapter=object(),
            account_identity=lambda: ("tongji", "synthetic-user"),
            workspace_root=lambda: self.root / "workspace",
        )
        self.runner = FakeRunner()
        self.settings = FakeSettings()
        self.valid = True
        self.service = WorkflowService(
            self.storage, self.courses, self.settings, self.runner,
            log_root=self.root / "logs", artifact_validator=lambda *_: self.valid, poll_seconds=0.01,
        )
        self.services = [self.service]

    def tearDown(self):
        for service in self.services:
            service.shutdown()
        self.storage.close_thread_connection()
        self.temp.cleanup()

    def create(self, operation="transcribe", key="request-key-1"):
        return self.service.create({"lecture_id": self.lecture.id, "operation": operation}, key)

    def wait_terminal(self, workflow_id):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            workflow = self.storage.get_workflow(workflow_id)
            if workflow and workflow.state not in {"queued", "running"}:
                return workflow
            time.sleep(0.01)
        self.fail("workflow did not reach a terminal state")

    def test_duplicate_submit_starts_once(self):
        first, created_first = self.create()
        second, created_second = self.create()
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertIsNotNone(first.settings_revision_id)
        self.assertEqual(self.wait_terminal(first.id).state, "succeeded")

    def test_same_key_with_different_body_conflicts(self):
        workflow, _ = self.create()
        with self.assertRaises(IdempotencyConflict):
            self.create(operation="slide")
        self.assertEqual(len(self.runner.calls), 1)
        self.wait_terminal(workflow.id)

    def test_task_title_is_frozen_at_creation(self):
        workflow, _ = self.create()
        self.storage.upsert_course(
            account_id=self.account.id, platform_course_id="course-101", title="Renamed later", source="platform"
        )
        reread = self.storage.get_workflow(workflow.id)
        self.assertEqual(reread.target["course_title"], "Frozen course title")
        self.assertEqual(reread.target["lecture_title"], "Frozen lecture title")
        self.wait_terminal(workflow.id)

    def test_invalid_artifact_never_succeeds(self):
        self.valid = False
        workflow, _ = self.create()
        finished = self.wait_terminal(workflow.id)
        self.assertEqual(finished.state, "failed")
        self.assertEqual(finished.steps[0].error_code, "INVALID_ARTIFACT")

    def test_output_validation_finds_manifest_without_local_index_entry(self):
        workspace = self.root / "workspace"
        raw = workspace / "raw" / "Frozen course title" / "2026-09-14 Frozen lecture title" / "原始数据"
        slides = raw / "slides"
        slides.mkdir(parents=True)
        (slides / "slide-001.png").write_bytes(b"synthetic-image")
        (slides / "index.json").write_text(
            json.dumps({"items": [{"filename": "slide-001.png"}]}), encoding="utf-8"
        )
        transcript = raw / "lecture.srt"
        transcript.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nSynthetic words\n", encoding="utf-8"
        )
        metadata = raw / "lecture.json"
        metadata.write_text(
            json.dumps({
                "complete": True,
                "course_id": "course-101",
                "sub_id": "lecture-202",
                "artifact_sha256": {
                    "subtitle_srt": hashlib.sha256(transcript.read_bytes()).hexdigest()
                },
            }),
            encoding="utf-8",
        )
        (raw / "manifest.json").write_text(
            json.dumps({
                "course_id": "course-101",
                "sub_id": "lecture-202",
                "playback_available": True,
                "artifacts": {
                    "slide_index": str(slides / "index.json"),
                    "subtitle_srt": str(transcript),
                    "transcript_meta": str(metadata),
                },
            }),
            encoding="utf-8",
        )
        validator = WorkflowService(
            self.storage,
            self.courses,
            self.settings,
            self.runner,
            log_root=self.root / "fallback-logs",
        )
        self.services.append(validator)

        self.assertTrue(validator._validate_output("slide", "course-101", "lecture-202"))
        self.assertTrue(validator._validate_output("transcribe", "course-101", "lecture-202"))
        rows = ArtifactService(self.courses.workspace_root).for_lecture(self.course, self.lecture)["items"]
        states = {item["kind"]: item["state"] for item in rows}
        self.assertEqual(states["transcript"], "available")
        self.assertEqual(states["slides"], "available")

    def test_output_validation_rejects_ambiguous_manifests(self):
        workspace = self.root / "workspace"
        for session in ("first", "duplicate"):
            raw = workspace / "raw" / "Frozen course title" / session / "原始数据"
            slides = raw / "slides"
            slides.mkdir(parents=True)
            (slides / "slide-001.png").write_bytes(b"synthetic-image")
            (slides / "index.json").write_text(
                json.dumps({"items": [{"filename": "slide-001.png"}]}), encoding="utf-8"
            )
            (raw / "manifest.json").write_text(
                json.dumps({
                    "course_id": "course-101",
                    "sub_id": "lecture-202",
                    "artifacts": {"slide_index": str(slides / "index.json")},
                }),
                encoding="utf-8",
            )
        validator = WorkflowService(
            self.storage,
            self.courses,
            self.settings,
            self.runner,
            log_root=self.root / "ambiguous-logs",
        )
        self.services.append(validator)

        self.assertFalse(validator._validate_output("slide", "course-101", "lecture-202"))

    def test_attempt_log_uses_absolute_byte_offsets(self):
        workflow, _ = self.create()
        finished = self.wait_terminal(workflow.id)
        attempt = finished.steps[0].attempts[0]
        first = self.service.read_log(workflow.id, attempt.id, offset=0, max_bytes=12)
        second = self.service.read_log(workflow.id, attempt.id, offset=first.next_offset, max_bytes=1024)
        self.assertEqual(first.offset, 0)
        self.assertGreater(second.offset, first.next_offset - 1)
        self.assertIn("synthetic runner finished", second.text)

    def test_v2_route_returns_public_workflow_without_absolute_log_path(self):
        router = V2Router(self.courses, workflows=self.service)
        body = {"lecture_id": self.lecture.id, "operation": "transcribe"}
        status, created = router.post("/api/v2/workflows", body, idempotency_key="route-key-01")
        self.assertEqual(status, 201)
        self.assertNotIn(str(self.root), str(created))
        public_attempt = created["data"]["steps"][0]["attempts"][0]
        self.assertNotIn("runner_task_id", public_attempt)
        self.assertNotIn("process_pid", public_attempt)
        workflow_id = created["data"]["id"]
        duplicate_status, duplicate = router.post("/api/v2/workflows", body, idempotency_key="route-key-01")
        conflict_status, conflict = router.post(
            "/api/v2/workflows", {"lecture_id": self.lecture.id, "operation": "slide"}, idempotency_key="route-key-01"
        )
        self.assertEqual(duplicate_status, 200)
        self.assertEqual(duplicate["data"]["id"], workflow_id)
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.runner.calls), 1)
        listed_status, listed = router.get("/api/v2/workflows", {})
        self.assertEqual(listed_status, 200)
        self.assertEqual(listed["data"][0]["id"], workflow_id)
        self.wait_terminal(workflow_id)

    def test_retry_keeps_successful_steps(self):
        self.valid = False
        workflow, _ = self.create()
        self.assertEqual(self.wait_terminal(workflow.id).state, "failed")
        self.valid = True

        retried, created = self.service.retry(workflow.id, "retry-key-001")

        self.assertTrue(created)
        self.assertEqual(retried.state, "succeeded")
        self.assertEqual([attempt.state for attempt in retried.steps[0].attempts], ["failed", "succeeded"])
        self.assertEqual(retried.steps[0].attempts[0].error_code, "INVALID_ARTIFACT")
        self.assertIn("通过校验", retried.steps[0].attempts[0].error["message"])
        self.assertTrue(retried.steps[0].artifact["reused"])
        self.assertEqual(len(self.runner.calls), 1)

    def test_retry_idempotency_does_not_create_duplicate_attempt(self):
        self.valid = False
        workflow, _ = self.create()
        self.wait_terminal(workflow.id)
        first, created = self.service.retry(workflow.id, "retry-key-002")
        self.wait_terminal(first.id)
        second, created_again = self.service.retry(workflow.id, "retry-key-002")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(second.steps[0].attempts), 2)
        self.assertEqual(len(self.runner.calls), 2)

    def test_cancel_kills_only_owned_tree(self):
        self.runner.initial_status = "running"
        workflow, _ = self.create()
        task = next(iter(self.runner.tasks.values()))

        self.service.cancel(workflow.id)
        self.assertEqual(self.wait_terminal(workflow.id).state, "cancelled")

        attempt = self.storage.get_workflow(workflow.id).steps[0].attempts[0]
        self.assertEqual(self.runner.cancelled, [attempt.runner_task_id])

    def test_v2_retry_route_requires_idempotency_and_returns_public_record(self):
        router = V2Router(self.courses, workflows=self.service)
        self.valid = False
        workflow, _ = self.create()
        self.wait_terminal(workflow.id)

        status_missing, _ = router.post(f"/api/v2/workflows/{workflow.id}/retry", {})
        status_retry, response = router.post(
            f"/api/v2/workflows/{workflow.id}/retry", {}, idempotency_key="route-retry-01"
        )

        self.assertEqual(status_missing, 400)
        self.assertEqual(status_retry, 201)
        attempt = response["data"]["steps"][0]["attempts"][-1]
        self.assertNotIn("runner_task_id", attempt)
        self.assertNotIn("process_pid", attempt)
        self.wait_terminal(workflow.id)

    def test_v2_cancel_route_requests_cancel_for_owned_task(self):
        self.runner.initial_status = "running"
        router = V2Router(self.courses, workflows=self.service)
        workflow, _ = self.create()

        status, response = router.post(f"/api/v2/workflows/{workflow.id}/cancel", {})

        self.assertEqual(status, 200)
        self.assertIn(response["data"]["state"], {"queued", "running"})
        self.assertTrue(response["data"]["cancel_requested"])
        self.assertEqual(len(self.runner.cancelled), 1)
        self.assertEqual(self.wait_terminal(workflow.id).state, "cancelled")

    def test_restart_marks_orphaned_attempt_interrupted(self):
        workflow, _ = self.create()
        self.wait_terminal(workflow.id)
        attempt_id = workflow.steps[0].attempts[0].id
        self.storage.update_workflow_state(workflow.id, "running")
        self.storage.update_attempt(attempt_id, state="running", process_pid=4567)
        self.storage.upsert_step(workflow_id=workflow.id, name="transcribe", ordinal=0, state="running")
        service = WorkflowService(
            self.storage, self.courses, self.settings, self.runner,
            log_root=self.root / "logs", artifact_validator=lambda *_: False,
            process_probe=lambda _pid: False,
        )

        recovered = service.recover_orphans()

        reread = self.storage.get_workflow(workflow.id)
        self.assertIn(workflow.id, recovered)
        self.assertEqual(reread.state, "interrupted")
        self.assertEqual(reread.steps[0].attempts[0].state, "interrupted")

    def test_restart_repairs_running_workflow_with_terminal_attempt(self):
        self.valid = False
        workflow, _ = self.create()
        self.wait_terminal(workflow.id)
        self.storage.update_workflow_state(workflow.id, "running")

        recovered = self.service.recover_orphans()

        reread = self.storage.get_workflow(workflow.id)
        self.assertIn(workflow.id, recovered)
        self.assertEqual(reread.state, "failed")
        self.assertEqual(reread.steps[0].attempts[0].state, "failed")

    def test_restart_does_not_duplicate_live_child(self):
        workflow, _ = self.create()
        self.wait_terminal(workflow.id)
        attempt_id = workflow.steps[0].attempts[0].id
        self.storage.update_workflow_state(workflow.id, "running")
        self.storage.update_attempt(attempt_id, state="running", process_pid=4568)
        self.storage.upsert_step(workflow_id=workflow.id, name="transcribe", ordinal=0, state="running")
        service = WorkflowService(
            self.storage, self.courses, self.settings, self.runner,
            log_root=self.root / "logs", process_probe=lambda _pid: True,
        )

        service.recover_orphans()
        with self.assertRaises(CourseServiceError) as raised:
            service.retry(workflow.id, "retry-key-003")

        self.assertEqual(raised.exception.code, "PROCESS_MAY_BE_RUNNING")
        self.assertEqual(self.storage.get_workflow(workflow.id).state, "blocked")
        self.assertEqual(len(self.runner.calls), 1)

    def test_asr_rejection_is_not_auto_retried(self):
        self.runner.initial_status = "failed"
        self.runner.initial_error = "HTTP 412"
        workflow, _ = self.create()
        failed = self.wait_terminal(workflow.id)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.storage.get_workflow(workflow.id).steps[0].attempts), 1)
        self.assertEqual(failed.steps[0].error_code, "ASR_REJECTED")
        self.assertFalse(failed.steps[0].error["retryable"])

    def test_temporary_network_failure_retries_with_bounded_attempts(self):
        self.valid = False
        self.runner.outcomes = [
            ("failed", "TimeoutError"),
            ("failed", "ConnectionError"),
            ("failed", "TimeoutError"),
        ]
        workflow, _ = self.create()

        deadline = time.monotonic() + 3
        failed = None
        while time.monotonic() < deadline:
            current = self.storage.get_workflow(workflow.id)
            if current and current.state == "failed" and len(current.steps[0].attempts) == 3:
                failed = current
                break
            time.sleep(0.01)
        self.assertIsNotNone(failed, repr(current))
        time.sleep(0.05)

        self.assertEqual(failed.state, "failed")
        self.assertEqual(len(failed.steps[0].attempts), 3)
        self.assertEqual(len(self.runner.calls), 3)
        self.assertEqual(failed.steps[0].error_code, "NETWORK_TEMPORARY")
        self.assertTrue(failed.steps[0].attempts[-1].error["retryable"])

    def test_cached_transcript_skips_media_retry(self):
        self.runner.initial_status = "failed"
        self.runner.initial_error = "TimeoutError"
        validation_calls = 0

        def validator(*_args):
            nonlocal validation_calls
            validation_calls += 1
            return validation_calls >= 1

        service = WorkflowService(
            self.storage, self.courses, self.settings, self.runner,
            log_root=self.root / "logs", artifact_validator=validator, poll_seconds=0.01,
        )
        self.services.append(service)
        workflow, _ = service.create({"lecture_id": self.lecture.id, "operation": "transcribe"}, "cache-retry-01")
        deadline = time.monotonic() + 2
        finished = None
        while time.monotonic() < deadline:
            current = self.storage.get_workflow(workflow.id)
            if current and current.state == "succeeded":
                finished = current
                break
            time.sleep(0.01)
        self.assertIsNotNone(finished)
        time.sleep(0.05)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual([attempt.state for attempt in finished.steps[0].attempts], ["failed", "succeeded"])
        self.assertTrue(finished.steps[0].artifact["reused"])

    def test_auth_rejection_is_not_automatically_retried(self):
        self.runner.initial_status = "failed"
        self.runner.initial_error = "HTTP 403"
        workflow, _ = self.create()

        failed = self.wait_terminal(workflow.id)

        self.assertEqual(failed.steps[0].error_code, "AUTH_REQUIRED")
        self.assertFalse(failed.steps[0].error["retryable"])
        self.assertEqual(len(failed.steps[0].attempts), 1)
        self.assertEqual(len(self.runner.calls), 1)

    def test_no_audio_and_missing_slides_are_not_retryable(self):
        self.assertEqual(classify_runner_failure("transcribe", "NO_AUDIO"), (
            "NO_AUDIO", "此课次没有可识别的音轨。", False,
        ))
        self.assertEqual(classify_runner_failure("slide", "SLIDES_UNAVAILABLE"), (
            "SLIDES_UNAVAILABLE", "此课次没有可用课件截图。", False,
        ))

    def test_restart_interrupts_retry_backoff_without_restarting_child(self):
        self.settings.retry_limit = 1
        self.runner.initial_status = "failed"
        self.runner.initial_error = "TimeoutError"
        workflow, _ = self.create()
        self.wait_terminal(workflow.id)
        self.storage.update_workflow_state(workflow.id, "queued")
        service = WorkflowService(
            self.storage, self.courses, self.settings, self.runner,
            log_root=self.root / "logs", process_probe=lambda _pid: None,
        )

        recovered = service.recover_orphans()

        self.assertIn(workflow.id, recovered)
        self.assertEqual(self.storage.get_workflow(workflow.id).state, "interrupted")
        self.assertEqual(len(self.runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
