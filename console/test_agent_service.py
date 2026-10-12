from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from console.agent_service import AgentHandoffService
from console.course_service import CourseService
from console.routes_v2 import V2Router
from console.storage import ConsoleStorage


class FakeSettings:
    def get(self):
        return {
            "revision_id": None,
            "note": {"route": "external_agent", "style": "standard", "include_timeline": True},
            "text_model": {"configured": False},
        }


class AgentHandoffServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.storage = ConsoleStorage(self.root / "db.sqlite3")
        self.account = self.storage.upsert_account(provider="tongji", platform_account_id="synthetic-agent-user")
        self.course = self.storage.upsert_course(
            account_id=self.account.id, platform_course_id="course-101", title="Synthetic course", source="platform"
        )
        self.lecture = self.storage.upsert_lecture(
            course_id=self.course.id, platform_sub_id="lecture-202", title="Synthetic lecture", playback_available=True
        )
        self.courses = CourseService(
            self.storage, adapter=object(),
            account_identity=lambda: ("tongji", "synthetic-agent-user"),
            workspace_root=lambda: self.root / "workspace",
        )
        self.service = AgentHandoffService(self.storage, self.courses, FakeSettings())
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.manifest = self.raw / "manifest.json"
        self.srt = self.raw / "course-101_lecture-202.srt"
        self.srt.write_text("1\n00:00:00,000 --> 00:01:00,000\nsynthetic transcript\n", encoding="utf-8")
        self.artifacts = {
            "lecture_dir": str(self.raw), "has_transcript": True,
            "transcript_txt": str(self.srt), "transcript_srt": str(self.srt),
            "slides_dir": "", "materials": [], "base_name": "course-101_lecture-202",
        }
        self.handoff = {
            "title": "生成学习笔记", "instruction": "只为 Synthetic lecture 生成笔记。",
            "warnings": [], "verification": [],
        }

    def write_manifest(self, course_id="course-101", sub_id="lecture-202"):
        meta = self.raw / "course-101_lecture-202.json"
        meta.write_text(json.dumps({
            "complete": True, "course_id": course_id, "sub_id": sub_id,
            "artifact_sha256": {"subtitle_srt": hashlib.sha256(self.srt.read_bytes()).hexdigest()},
        }), encoding="utf-8")
        self.manifest.write_text(json.dumps({
            "course_id": course_id, "sub_id": sub_id, "base_name": "course-101_lecture-202",
            "artifacts": {"subtitle_srt": str(self.srt), "transcript_meta": str(meta)},
        }), encoding="utf-8")

    def tearDown(self):
        self.storage.close_thread_connection()
        self.temp.cleanup()

    def create(self, key="agent-key-001"):
        with patch("console.agent_service.agent_bridge.detect_artifacts", return_value=self.artifacts), \
             patch("console.agent_service.agent_bridge.build_agent_instruction", return_value=self.handoff):
            return self.service.create({"lecture_id": self.lecture.id}, key)

    def test_handoff_does_not_claim_agent_execution(self):
        workflow, created, instruction = self.create()
        self.assertTrue(created)
        self.assertEqual(workflow.operation, "agent_note")
        self.assertEqual(workflow.state, "waiting_agent")
        self.assertEqual(workflow.steps[0].state, "waiting_agent")
        self.assertEqual(workflow.steps[0].attempts, [])
        self.assertIn("只为 Synthetic lecture", instruction)

    def test_waiting_agent_is_persisted_and_idempotent(self):
        first, created, _ = self.create()
        second, duplicate, instruction = self.create()
        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.state, "waiting_agent")
        self.assertIn("只为 Synthetic lecture", instruction)

    def test_wrong_lecture_output_is_rejected(self):
        workflow, _, _ = self.create()
        self.write_manifest(course_id="other-course")
        note = self.raw / "course-101_lecture-202_notes.md"
        timeline = self.raw / "course-101_lecture-202_timeline.txt"
        note.write_text("### Notes\nA synthetic lesson note.", encoding="utf-8")
        timeline.write_text("00:00-01:00：合成主题", encoding="utf-8")
        with patch("console.agent_service.agent_bridge.find_lecture_dir", return_value=self.raw), \
             patch("console.agent_service.agent_bridge.verify_agent_output", return_value={"ready": True, "missing": []}):
            result = self.service.verify(workflow.id)
        self.assertFalse(result["ready"])
        self.assertEqual(result["workflow"].state, "waiting_agent")

    def test_empty_outputs_do_not_unlock_waiting_agent(self):
        workflow, _, _ = self.create()
        self.write_manifest()
        (self.raw / "course-101_lecture-202_notes.md").touch()
        (self.raw / "course-101_lecture-202_timeline.txt").touch()
        with patch("console.agent_service.agent_bridge.find_lecture_dir", return_value=self.raw), \
             patch("console.agent_service.agent_bridge.verify_agent_output", return_value={"ready": True, "missing": []}):
            result = self.service.verify(workflow.id)
        self.assertFalse(result["ready"])
        self.assertEqual(result["workflow"].state, "waiting_agent")

    def test_verified_outputs_advance_only_after_checks(self):
        workflow, _, _ = self.create()
        self.write_manifest()
        (self.raw / "course-101_lecture-202_notes.md").write_text("### Notes\nA detailed synthetic lesson note.", encoding="utf-8")
        (self.raw / "course-101_lecture-202_timeline.txt").write_text("00:00-01:00：合成主题", encoding="utf-8")
        with patch("console.agent_service.agent_bridge.find_lecture_dir", return_value=self.raw), \
             patch("console.agent_service.agent_bridge.verify_agent_output", return_value={"ready": True, "missing": []}):
            result = self.service.verify(workflow.id)
        self.assertTrue(result["ready"])
        self.assertEqual(result["workflow"].state, "succeeded")
        self.assertTrue(result["workflow"].steps[0].artifact["validated"])

    def test_timeline_outside_subtitle_duration_keeps_waiting(self):
        workflow, _, _ = self.create()
        self.write_manifest()
        (self.raw / "course-101_lecture-202_notes.md").write_text("### Notes\nA detailed synthetic lesson note.", encoding="utf-8")
        (self.raw / "course-101_lecture-202_timeline.txt").write_text("00:00-02:00：超出字幕范围", encoding="utf-8")
        with patch("console.agent_service.agent_bridge.find_lecture_dir", return_value=self.raw), \
             patch("console.agent_service.agent_bridge.verify_agent_output", return_value={"ready": True, "missing": []}):
            result = self.service.verify(workflow.id)
        self.assertFalse(result["ready"])
        self.assertEqual(result["workflow"].state, "waiting_agent")

    def test_v2_handoff_route_keeps_instruction_out_of_task_list(self):
        workflow_manager = type("WorkflowManager", (), {"storage": self.storage})()
        router = V2Router(self.courses, FakeSettings(), workflows=workflow_manager)
        with patch("console.agent_service.agent_bridge.detect_artifacts", return_value=self.artifacts), \
             patch("console.agent_service.agent_bridge.build_agent_instruction", return_value=self.handoff):
            status, created = router.post("/api/v2/workflows", {"lecture_id": self.lecture.id, "operation": "agent_note"}, idempotency_key="router-key-001")
        self.assertEqual(status, 201)
        self.assertEqual(created["data"]["state"], "waiting_agent")
        self.assertIn("只为 Synthetic lecture", created["instruction"])
        self.assertNotIn("instruction", json.dumps(created["data"]))
        status, instruction = router.get(f"/api/v2/workflows/{created['data']['id']}/agent-instruction", {})
        self.assertEqual(status, 200)
        self.assertIn("Synthetic lecture", instruction["data"]["instruction"])


if __name__ == "__main__":
    unittest.main()
