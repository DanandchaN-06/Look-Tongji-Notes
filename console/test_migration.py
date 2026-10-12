from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from console.course_service import CourseService
from console.migration import MigrationService, StaleMigrationPreview
from console.routes_v2 import V2Router
from console.storage import ConsoleStorage, stable_account_id


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.storage = ConsoleStorage(self.root / "console.sqlite3")
        self.service = MigrationService(
            self.storage, lambda: self.workspace, backup_root=self.root / "backups"
        )
        self.local_account = stable_account_id("local", "workspace")
        self.storage.upsert_account(provider="local", platform_account_id="workspace")

    def tearDown(self):
        self.storage.close_thread_connection()
        self.temp.cleanup()

    def add_manifest(self, course_dir: str, lecture_dir: str, *, course_id: str | None, sub_id: str | None, title: str):
        raw = self.workspace / "raw" / course_dir / lecture_dir / "原始数据"
        raw.mkdir(parents=True)
        manifest = {"course_title": title, "session_title": lecture_dir}
        if course_id:
            manifest["course_id"] = course_id
        if sub_id:
            manifest["sub_id"] = sub_id
        (raw / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return raw / "manifest.json"

    def test_migration_is_repeatable_and_keeps_three_lectures_under_one_course(self):
        for number in range(1, 4):
            self.add_manifest("platform-course", f"Week {number}", course_id="course-1", sub_id=f"lecture-{number}", title="Algorithms")

        preview = self.service.preview()
        self.assertEqual(preview["course_count"], 1)
        self.assertEqual(len(preview["mappings"][0]["lectures"]), 3)
        applied = self.service.apply(preview["preview_id"])
        repeated = self.service.apply(preview["preview_id"])
        restarted_storage = ConsoleStorage(self.storage.db_path)
        try:
            restarted = MigrationService(
                restarted_storage, lambda: self.workspace, backup_root=self.root / "backups"
            )
            second_preview = restarted.preview()
            restarted.apply(second_preview["preview_id"])
        finally:
            restarted_storage.close_thread_connection()

        courses = self.storage.list_courses(self.local_account)
        self.assertEqual(len(courses), 1)
        self.assertEqual(len(self.storage.list_lectures(courses[0].id)), 3)
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(repeated["state"], "applied")
        self.assertTrue(MigrationService.backup_database_path(applied["backup_path"]).is_file())

    def test_existing_identity_conflict_is_left_untouched(self):
        current = self.storage.upsert_course(
            account_id=self.local_account, platform_course_id="course-1", title="Human-edited title", source="local"
        )
        self.add_manifest("course", "Week 1", course_id="course-1", sub_id="lecture-1", title="Platform title")

        preview = self.service.preview()
        self.service.apply(preview["preview_id"])

        course = self.storage.get_course(current.id, self.local_account)
        self.assertEqual(course.title, "Human-edited title")
        self.assertEqual(self.storage.list_lectures(current.id), [])
        self.assertTrue(any(item["kind"] == "course_identity" for item in preview["conflicts"]))

    def test_same_title_custom_courses_remain_separate(self):
        self.add_manifest("custom-a", "Week 1", course_id=None, sub_id=None, title="Same title")
        self.add_manifest("custom-b", "Week 1", course_id=None, sub_id=None, title="Same title")

        preview = self.service.preview()
        self.service.apply(preview["preview_id"])

        courses = self.storage.list_courses(self.local_account)
        self.assertEqual(len(courses), 2)
        self.assertEqual({course.title for course in courses}, {"Same title"})
        self.assertEqual(len({course.import_key for course in courses}), 2)

    def test_stale_preview_is_rejected_before_database_changes(self):
        manifest = self.add_manifest("course", "Week 1", course_id="course-1", sub_id="lecture-1", title="Algorithms")
        preview = self.service.preview()
        manifest.write_text(manifest.read_text(encoding="utf-8").replace("Algorithms", "Changed"), encoding="utf-8")

        with self.assertRaises(StaleMigrationPreview):
            self.service.apply(preview["preview_id"])
        self.assertEqual(self.storage.list_courses(self.local_account), [])

    def test_backup_restores_previous_database_and_workspace_state(self):
        manifest = self.add_manifest("course", "Week 1", course_id="course-1", sub_id="lecture-1", title="Algorithms")
        settings_file = self.root / "private-settings.json"
        settings_file.write_text('{"preference":"before"}', encoding="utf-8")
        service = MigrationService(
            self.storage, lambda: self.workspace, backup_root=self.root / "backups",
            backup_sources=lambda: [settings_file],
        )
        original = manifest.read_bytes()
        preview = service.preview()
        applied = service.apply(preview["preview_id"])
        restore_root = self.root / "restored"
        restore_root.mkdir()
        restore_db = restore_root / "console.sqlite3"
        MigrationService.restore_backup(
            applied["backup_path"], database_path=restore_db, workspace_root=restore_root / "workspace",
            configuration_paths=[restore_root / "private-settings.json"],
        )
        backup = ConsoleStorage(restore_db)
        try:
            self.assertEqual(backup.list_courses(self.local_account), [])
        finally:
            backup.close_thread_connection()
        self.assertEqual((restore_root / "workspace" / "raw" / "course" / "Week 1" / "原始数据" / "manifest.json").read_bytes(), original)
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual((restore_root / "private-settings.json").read_text(encoding="utf-8"), '{"preference":"before"}')

    def test_legacy_batch_unknown_status_is_preserved_as_unknown(self):
        state_dir = self.workspace / "state" / "batches"
        state_dir.mkdir(parents=True)
        (state_dir / "old.json").write_text(json.dumps({
            "course_id": "course-1", "lectures": [{"sub_id": "lecture-1", "status": "mystery"}]
        }), encoding="utf-8")

        preview = self.service.preview()

        self.assertEqual(preview["legacy_batches"][0]["lectures"][0]["status"], "unknown")
        self.assertIn("needs_review", preview["legacy_batches"][0]["lectures"][0])

    def test_unreadable_manifest_is_reported_and_never_imported(self):
        raw = self.workspace / "raw" / "course" / "Week 1" / "原始数据"
        raw.mkdir(parents=True)
        (raw / "manifest.json").write_text('{broken', encoding="utf-8")

        preview = self.service.preview()
        self.service.apply(preview["preview_id"])

        self.assertEqual(preview["course_count"], 0)
        self.assertTrue(any(item["kind"] == "unreadable_manifest" for item in preview["conflicts"]))
        self.assertEqual(self.storage.list_courses(self.local_account), [])

    def test_preview_route_is_read_only_and_apply_hides_backup_path(self):
        self.add_manifest("course", "Week 1", course_id="course-1", sub_id="lecture-1", title="Algorithms")
        courses = CourseService(self.storage, adapter=object(), account_identity=lambda: None, workspace_root=lambda: self.workspace)
        router = V2Router(courses, migration_service=self.service)

        status, response = router.post("/api/v2/migrations/preview", {})

        self.assertEqual(status, 200)
        self.assertEqual(len(self.storage.list_courses(self.local_account)), 0)
        self.assertNotIn(str(self.root), json.dumps(response))
        self.assertEqual(response["data"]["mappings"][0]["title"], "Algorithms")
        apply_status, applied = router.post(
            "/api/v2/migrations/apply", {"preview_id": response["data"]["preview_id"]}
        )
        self.assertEqual(apply_status, 200)
        self.assertTrue(applied["data"]["backup_created"])
        self.assertNotIn(str(self.root), json.dumps(applied))

    def test_stale_preview_route_returns_conflict(self):
        manifest = self.add_manifest("course", "Week 1", course_id="course-1", sub_id="lecture-1", title="Algorithms")
        courses = CourseService(self.storage, adapter=object(), account_identity=lambda: None, workspace_root=lambda: self.workspace)
        router = V2Router(courses, migration_service=self.service)
        preview = self.service.preview()
        manifest.write_text('{}', encoding="utf-8")

        status, response = router.post("/api/v2/migrations/apply", {"preview_id": preview["preview_id"]})

        self.assertEqual(status, 409)
        self.assertEqual(response["error"]["code"], "STALE_PREVIEW")
        self.assertEqual(self.storage.list_courses(self.local_account), [])


if __name__ == "__main__":
    unittest.main()
