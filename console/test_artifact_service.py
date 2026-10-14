from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from console.artifact_service import ArtifactService
from console.course_service import CourseService
from console.routes_v2 import V2Router
from console.storage import ConsoleStorage


class ArtifactServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.raw = self.workspace / "raw" / "synthetic-course" / "Week 1" / "原始数据"
        self.raw.mkdir(parents=True)
        self.storage = ConsoleStorage(self.root / "db.sqlite3")
        account = self.storage.upsert_account(provider="local", platform_account_id="workspace")
        self.account_id = account.id
        self.course = self.storage.upsert_course(
            account_id=self.account_id,
            platform_course_id="course-1",
            title="Synthetic course",
            source="local",
        )
        self.manifest_path = self.raw / "manifest.json"
        self.lecture = self.storage.upsert_lecture(
            course_id=self.course.id,
            platform_sub_id="lecture-1",
            title="Synthetic lecture",
            class_date="2026-09-14",
            playback_available=True,
            manifest_path=str(self.manifest_path),
        )

    def tearDown(self):
        self.storage.close_thread_connection()
        self.temp.cleanup()

    def write_manifest(self, artifacts):
        self.manifest_path.write_text(json.dumps({
            "course_id": "course-1", "sub_id": "lecture-1", "base_name": "course-1_lecture-1",
            "artifacts": artifacts,
        }), encoding="utf-8")

    def test_existing_artifacts_are_read_by_lecture_identity_and_paths_are_not_exposed(self):
        transcript = self.raw / "course-1_lecture-1.srt"
        transcript.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        notes = self.raw / "course-1_lecture-1_notes.md"
        notes.write_text("# Synthetic notes\nA useful summary.", encoding="utf-8")
        timeline = self.raw / "timeline.txt"
        timeline.write_text("00:00-00:01 Opening", encoding="utf-8")
        slides = self.raw / "slides" / "index.json"
        slides.parent.mkdir()
        slides.write_text(json.dumps({"items": [{"filename": "slide-1.png"}], "failures": []}), encoding="utf-8")
        self.write_manifest({
            "subtitle_srt": str(transcript), "transcript_meta": str(self.raw / "course-1_lecture-1.json"),
            "slide_index": str(slides), "timeline_txt": str(timeline),
        })
        (self.raw / "course-1_lecture-1.json").write_text(json.dumps({
            "complete": True, "course_id": "course-1", "sub_id": "lecture-1",
            "artifact_sha256": {"subtitle_srt": hashlib.sha256(transcript.read_bytes()).hexdigest()},
        }), encoding="utf-8")

        result = ArtifactService(lambda: self.workspace).for_lecture(self.course, self.lecture)

        self.assertEqual({item["kind"]: item["state"] for item in result["items"]}, {
            "transcript": "available", "slides": "available", "notes": "available", "timeline": "available",
        })
        self.assertIn("Synthetic notes", result["notes_preview"])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_partial_slides_are_not_marked_unavailable(self):
        slides = self.raw / "slides" / "index.json"
        slides.parent.mkdir(parents=True, exist_ok=True)
        (slides.parent / "slide-1.png").write_bytes(b"synthetic slide")
        slides.write_text(json.dumps({
            "items": [{"filename": "slide-1.png"}],
            "failures": [{"snapshot_id": "snapshot-2", "error": "temporary failure"}],
        }), encoding="utf-8")
        self.write_manifest({"slide_index": str(slides)})

        result = ArtifactService(lambda: self.workspace).for_lecture(self.course, self.lecture)

        row = next(item for item in result["items"] if item["kind"] == "slides")
        self.assertEqual(row["state"], "available")
        self.assertIn("部分下载失败", row["message"])

    def test_placeholder_notes_are_not_marked_available(self):
        notes = self.raw / "course-1_lecture-1_notes.md"
        notes.write_text("TODO：待生成笔记", encoding="utf-8")
        self.write_manifest({})

        result = ArtifactService(lambda: self.workspace).for_lecture(self.course, self.lecture)

        row = next(item for item in result["items"] if item["kind"] == "notes")
        self.assertEqual(row["state"], "not_collected")

    def test_manifest_identity_mismatch_never_reports_artifact_available(self):
        transcript = self.raw / "notes.txt"
        transcript.write_text("wrong lecture", encoding="utf-8")
        self.write_manifest({"transcript_txt": str(transcript)})
        self.manifest_path.write_text(json.dumps({"course_id": "course-1", "sub_id": "other", "artifacts": {"transcript_txt": str(transcript)}}), encoding="utf-8")

        result = ArtifactService(lambda: self.workspace).for_lecture(self.course, self.lecture)

        self.assertTrue(all(item["state"] == "not_collected" for item in result["items"]))

    def test_missing_recorded_artifact_is_expired_and_path_escape_is_rejected(self):
        self.write_manifest({"transcript_txt": str(self.workspace / "missing.txt"), "timeline_txt": str(self.root / "outside.txt")})
        result = ArtifactService(lambda: self.workspace).for_lecture(self.course, self.lecture)
        states = {item["kind"]: item["state"] for item in result["items"]}
        self.assertEqual(states["transcript"], "expired")
        self.assertEqual(states["timeline"], "expired")

    def test_transcript_requires_matching_completion_hash_and_valid_srt(self):
        transcript = self.raw / "course-1_lecture-1.srt"
        transcript.write_text("broken subtitles", encoding="utf-8")
        meta_path = self.raw / "course-1_lecture-1.json"
        meta_path.write_text(json.dumps({
            "complete": True, "course_id": "course-1", "sub_id": "lecture-1",
            "artifact_sha256": {"subtitle_srt": hashlib.sha256(transcript.read_bytes()).hexdigest()},
        }), encoding="utf-8")
        self.write_manifest({"subtitle_srt": str(transcript), "transcript_meta": str(meta_path)})

        result = ArtifactService(lambda: self.workspace).for_lecture(self.course, self.lecture)

        self.assertEqual(result["items"][0]["state"], "expired")

    def test_platform_without_playback_is_marked_not_provided(self):
        lecture = self.storage.upsert_lecture(
            course_id=self.course.id, platform_sub_id="lecture-2", title="No playback",
            playback_available=False,
        )
        self.assertEqual(ArtifactService(lambda: self.workspace).for_lecture(self.course, lecture)["items"][0]["state"], "not_provided")

    def test_v2_artifact_route_requires_the_course_lecture_pair(self):
        self.write_manifest({})
        courses = CourseService(
            self.storage,
            account_identity=lambda: None,
            workspace_root=lambda: self.workspace,
        )
        router = V2Router(courses)
        path = f"/api/v2/courses/{self.course.id}/lectures/{self.lecture.id}/artifacts"
        status, payload = router.get(path, {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["lecture_id"], self.lecture.id)
        self.assertNotIn("manifest_path", json.dumps(payload))
        status, _ = router.get(f"/api/v2/courses/{self.course.id}/lectures/other-lecture/artifacts", {})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
