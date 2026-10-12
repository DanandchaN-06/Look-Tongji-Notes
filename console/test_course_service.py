"""Course cache behavior with synthetic platform responses and manifests."""

from __future__ import annotations

import json
import http.client
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from console.course_service import CourseService, CourseServiceError
from console.migration import MigrationService
from console.routes_v2 import V2Router
from console.server import make_server
from console.storage import ConsoleStorage, stable_account_id


class FakeCourseAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = False
        self.courses = [
            {"course_id": "platform-course-1", "title": "Synthetic algorithms", "teacher": "Demo teacher"}
        ]
        self.lectures = {
            "platform-course-1": [
                {
                    "sub_id": "lecture-1",
                    "sub_title": "First synthetic lecture",
                    "date": "2026-09-14",
                    "has_playback": True,
                }
            ]
        }

    def list_courses(self):
        self.calls.append(("courses", ""))
        if self.fail:
            raise CourseServiceError("COURSE_SYNC_FAILED", "Synthetic platform failure", retryable=True)
        return self.courses

    def list_lectures(self, course_id: str):
        self.calls.append(("lectures", course_id))
        if self.fail:
            raise CourseServiceError("COURSE_SYNC_FAILED", "Synthetic platform failure", retryable=True)
        return self.lectures.get(course_id, [])


class CourseServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.storage = ConsoleStorage(self.root / "console.sqlite3")
        self.adapter = FakeCourseAdapter()
        self.identity: tuple[str, str] | None = ("tongji", "synthetic-user-a")
        self.service = CourseService(
            self.storage,
            adapter=self.adapter,
            account_identity=lambda: self.identity,
            workspace_root=lambda: self.root / "courses",
        )

    def tearDown(self) -> None:
        self.storage.close_thread_connection()
        self.temp_dir.cleanup()

    def test_offline_reads_cache(self):
        self.service.sync_courses()
        call_count = len(self.adapter.calls)
        self.adapter.fail = True

        result = self.service.list_courses(source="platform")

        self.assertEqual(len(result["courses"]), 1)
        self.assertEqual(len(self.adapter.calls), call_count)

    def test_failed_sync_preserves_cache_and_last_success(self):
        self.service.sync_courses()
        before = self.service.list_courses(source="platform")
        last_success = before["sync_status"]["last_success_at"]
        self.adapter.fail = True

        with self.assertRaises(CourseServiceError):
            self.service.sync_courses()

        after = self.service.list_courses(source="platform")
        self.assertEqual(after["courses"], before["courses"])
        self.assertEqual(after["sync_status"]["state"], "failed")
        self.assertEqual(after["sync_status"]["last_success_at"], last_success)
        self.assertNotIn("Synthetic platform failure", json.dumps(after["sync_status"]))

    def test_empty_query_is_success(self):
        result = self.service.list_courses(source="all", query="not present")
        self.assertEqual(result["courses"], [])
        self.assertEqual(result["query"], "not present")

    def test_courses_sort_by_most_recent_lecture_date(self):
        account_id = self.service.local_account_id
        self.storage.upsert_account(platform_account_id="workspace", provider="local")
        dated_courses = [
            ("older-course", "Zebra older", "2026-9-12"),
            ("recent-course", "Alpha recent", "2026-12-5"),
            ("same-date-z", "Zulu tie", "2026-12-5"),
            ("undated-course", "Middle undated", None),
        ]
        for key, title, class_date in dated_courses:
            course = self.storage.upsert_import_course(
                account_id=account_id, import_key=key, title=title
            )
            if class_date:
                self.storage.upsert_import_lecture(
                    course_id=course.id,
                    import_key=f"{key}-lecture",
                    title=f"Lecture for {title}",
                    class_date=class_date,
                )

        result = self.service.list_courses(source="local")

        self.assertEqual(
            [course["title"] for course in result["courses"]],
            ["Alpha recent", "Zulu tie", "Zebra older", "Middle undated"],
        )

    def test_duplicate_sync_is_stable(self):
        first = self.service.sync_courses()
        second = self.service.sync_courses()
        account_id = stable_account_id("tongji", "synthetic-user-a")
        course = self.storage.list_courses(account_id)[0]
        self.assertEqual(first["sync_job"]["course_count"], 1)
        self.assertEqual(second["sync_job"]["course_count"], 1)
        self.assertEqual(self.storage.connection.execute("SELECT COUNT(*) FROM courses").fetchone()[0], 1)
        self.assertEqual(self.storage.connection.execute("SELECT COUNT(*) FROM lectures").fetchone()[0], 1)
        self.assertEqual(len(self.storage.list_lectures(course.id)), 1)

    def test_foreign_account_cannot_read_course(self):
        self.service.sync_courses()
        self.identity = ("tongji", "synthetic-user-b")
        result = self.service.list_courses(source="platform")
        self.assertEqual(result["courses"], [])

    def test_local_imports_are_stable_and_not_merged_by_title(self):
        raw_root = self.root / "courses" / "raw"
        for course_folder in ("import-a", "import-b"):
            raw = raw_root / course_folder / "Week 1" / "原始数据"
            raw.mkdir(parents=True)
            (raw / "manifest.json").write_text(
                json.dumps({"course_title": "Same synthetic title", "session_title": "First lecture"}),
                encoding="utf-8",
            )

        migration = MigrationService(self.storage, lambda: self.root / "courses", backup_root=self.root / "backups")
        preview = migration.preview()
        self.assertEqual(preview["course_count"], 2)
        migration.apply(preview["preview_id"])

        first = self.service.list_courses(source="local")
        second = self.service.list_courses(source="local")
        self.assertEqual(len(first["courses"]), 2)
        self.assertEqual(len({item["id"] for item in first["courses"]}), 2)
        self.assertEqual(first["courses"], second["courses"])
        self.assertTrue(all(item["identity_kind"] == "import" for item in first["courses"]))
        self.assertTrue(all(item["lecture_count"] == 1 for item in first["courses"]))

    def test_local_platform_manifests_keep_one_course_and_lecture_identity(self):
        raw_root = self.root / "courses" / "raw"
        for index in range(3):
            raw = raw_root / "stable-course" / f"Week {index + 1}" / "原始数据"
            raw.mkdir(parents=True)
            (raw / "manifest.json").write_text(
                json.dumps(
                    {
                        "course_id": "platform-course-1",
                        "sub_id": f"lecture-{index + 1}",
                        "course_title": "Synthetic algorithms",
                        "session_title": f"Lecture {index + 1}",
                        "date": f"2026-09-{14 + index:02d}",
                    }
                ),
                encoding="utf-8",
            )

        migration = MigrationService(self.storage, lambda: self.root / "courses", backup_root=self.root / "backups")
        preview = migration.preview()
        migration.apply(preview["preview_id"])

        first = self.service.list_courses(source="local")
        second = self.service.list_courses(source="local")
        self.assertEqual(len(first["courses"]), 1)
        self.assertEqual(first["courses"][0]["lecture_count"], 3)
        self.assertEqual(first["courses"], second["courses"])

    def test_reading_local_courses_does_not_implicitly_migrate_files(self):
        raw = self.root / "courses" / "raw" / "course" / "Week 1" / "原始数据"
        raw.mkdir(parents=True)
        (raw / "manifest.json").write_text(json.dumps({"course_id": "course-1", "sub_id": "lecture-1"}), encoding="utf-8")

        self.assertEqual(self.service.list_courses(source="local")["courses"], [])
        self.assertEqual(self.storage.list_courses(self.service.local_account_id), [])

    def test_v2_router_uses_standard_envelope_and_fixed_actions(self):
        router = V2Router(self.service)
        status, payload = router.get("/api/v2/courses", {"source": ["all"], "q": ["absent"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "data": {
            "source": "all", "query": "absent", "courses": [], "sync_status": None
        }})

        unauthenticated = CourseService(
            self.storage,
            adapter=self.adapter,
            account_identity=lambda: None,
            workspace_root=lambda: None,
        )
        missing_auth = V2Router(unauthenticated)
        status, payload = missing_auth.post("/api/v2/courses/sync", {})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["actions"], ["open_settings"])

    def test_v2_course_lectures_route_serializes_cached_records(self):
        raw = self.root / "courses" / "raw" / "cached-course" / "Week 1" / "原始数据"
        raw.mkdir(parents=True)
        (raw / "manifest.json").write_text(
            json.dumps({
                "course_id": "cached-course-id",
                "sub_id": "cached-lecture-id",
                "course_title": "Synthetic cached course",
                "session_title": "Synthetic cached lecture",
            }),
            encoding="utf-8",
        )
        migration = MigrationService(
            self.storage,
            lambda: self.root / "courses",
            backup_root=self.root / "backups",
        )
        preview = migration.preview()
        migration.apply(preview["preview_id"])
        course = self.service.list_courses(source="local")["courses"][0]

        status, payload = V2Router(self.service).get(
            f"/api/v2/courses/{course['id']}/lectures", {}
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertNotIn("account_id", payload["data"]["course"])
        self.assertNotIn("manifest_path", payload["data"]["lectures"][0])
        self.assertEqual(payload["data"]["lectures"][0]["title"], "Synthetic cached lecture")

    def test_v2_http_route_requires_existing_console_token(self):
        db_path = self.root / "http.sqlite3"
        server = None
        thread = None
        with patch.dict(os.environ, {
            "LOOK_TONGJI_CONSOLE_DB": str(db_path),
            "LOOK_TONGJI_CONFIG_PATH": str(self.root / "missing-config.json"),
        }), patch("console.server.config_store.read_env", return_value={}):
            server = make_server("127.0.0.1", 0, "synthetic-http-token")
            server.daemon_threads = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request("GET", "/api/v2/courses?source=local")
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                response.read()

                connection.request(
                    "GET",
                    "/api/v2/courses?source=local",
                    headers={"X-Console-Token": "synthetic-http-token"},
                )
                response = connection.getresponse()
                body = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(body["data"]["courses"], [])
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
