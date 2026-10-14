"""Persistence contracts for the console's first SQLite schema."""

from __future__ import annotations

import tempfile
import sqlite3
import threading
import unittest
from pathlib import Path

from console.storage import ConsoleStorage, _MIGRATIONS


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "console.sqlite3"
        self.storage = ConsoleStorage(self.db_path)

    def tearDown(self) -> None:
        self.storage.close_thread_connection()
        self.temp_dir.cleanup()

    def _workflow(self, storage: ConsoleStorage | None = None):
        store = storage or self.storage
        account = store.upsert_account(platform_account_id="synthetic-user-a", provider="tongji")
        course = store.upsert_course(
            account_id=account.id,
            platform_course_id="course-101",
            title="Synthetic course",
            teacher="Synthetic teacher",
            term="2026-fall",
        )
        lecture = store.upsert_lecture(
            course_id=course.id,
            platform_sub_id="lecture-202",
            title="Synthetic lecture",
            class_date="2026-09-14",
            playback_available=True,
        )
        revision = store.create_settings_revision({"note_route": "external_agent"})
        workflow = store.create_workflow(
            lecture_id=lecture.id,
            operation="organize",
            settings_revision_id=revision.id,
            target={"course_title": course.title, "session_title": lecture.title},
        )
        step = store.upsert_step(
            workflow_id=workflow.id,
            name="inspect",
            ordinal=1,
            state="succeeded",
        )
        attempt = store.create_attempt(step_id=step.id, state="succeeded", exit_code=0)
        return account, course, lecture, revision, workflow, step, attempt

    def test_reopen_preserves_workflow(self):
        *_, workflow, step, attempt = self._workflow()
        self.storage.close_thread_connection()

        reopened = ConsoleStorage(self.db_path)
        restored = reopened.get_workflow(workflow.id)
        self.assertEqual(restored.id, workflow.id)
        self.assertEqual(restored.state, "queued")
        self.assertEqual(restored.target["session_title"], "Synthetic lecture")
        self.assertEqual(restored.steps[0].id, step.id)
        self.assertEqual(restored.steps[0].attempts[0].id, attempt.id)
        reopened.close_thread_connection()

    def test_upsert_is_idempotent(self):
        account, course, lecture, *_ = self._workflow()
        same_course = self.storage.upsert_course(
            account_id=account.id,
            platform_course_id="course-101",
            title="Updated synthetic course",
            teacher="Synthetic teacher",
            term="2026-fall",
        )
        same_lecture = self.storage.upsert_lecture(
            course_id=course.id,
            platform_sub_id="lecture-202",
            title="Updated synthetic lecture",
            class_date="2026-09-14",
            playback_available=True,
        )
        self.assertEqual(same_course.id, course.id)
        self.assertEqual(same_lecture.id, lecture.id)
        self.assertEqual(self.storage.connection.execute("SELECT COUNT(*) FROM courses").fetchone()[0], 1)
        self.assertEqual(self.storage.connection.execute("SELECT COUNT(*) FROM lectures").fetchone()[0], 1)
        self.assertEqual(self.storage.get_course(course.id, account.id).title, "Updated synthetic course")

    def test_lectures_sort_oldest_first_with_unpadded_dates(self):
        account = self.storage.upsert_account(platform_account_id="synthetic-order-user", provider="tongji")
        course = self.storage.upsert_course(
            account_id=account.id, platform_course_id="ordered-course", title="Ordered course"
        )
        fixtures = [
            ("lecture-2027", "2027-1-1"),
            ("lecture-dec", "2026-12-5"),
            ("lecture-sept-later", "2026-9-30"),
            ("lecture-sept-first", "2026-9-5"),
            ("lecture-undated", None),
        ]
        for key, class_date in fixtures:
            self.storage.upsert_lecture(
                course_id=course.id,
                platform_sub_id=key,
                title=key,
                class_date=class_date,
            )

        lectures = self.storage.list_lectures(course.id)

        self.assertEqual(
            [lecture.platform_sub_id for lecture in lectures],
            ["lecture-sept-first", "lecture-sept-later", "lecture-dec", "lecture-2027", "lecture-undated"],
        )

    def test_account_isolation(self):
        first = self.storage.upsert_account(platform_account_id="synthetic-user-a", provider="tongji")
        second = self.storage.upsert_account(platform_account_id="synthetic-user-b", provider="tongji")
        self.assertNotEqual(first.id, "synthetic-user-a")
        self.assertNotEqual(second.id, "synthetic-user-b")
        course_a = self.storage.upsert_course(
            account_id=first.id, platform_course_id="same-platform-id", title="Same title"
        )
        course_b = self.storage.upsert_course(
            account_id=second.id, platform_course_id="same-platform-id", title="Same title"
        )
        self.assertNotEqual(course_a.id, course_b.id)
        self.assertEqual(self.storage.list_courses(first.id)[0].id, course_a.id)
        self.assertEqual(self.storage.list_courses(second.id)[0].id, course_b.id)
        self.assertIsNone(self.storage.get_course(course_a.id, second.id))

    def test_transaction_rolls_back(self):
        with self.assertRaisesRegex(RuntimeError, "abort test transaction"):
            with self.storage.transaction():
                self.storage.upsert_account(platform_account_id="synthetic-rollback", provider="tongji")
                raise RuntimeError("abort test transaction")
        self.assertEqual(self.storage.connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)

    def test_schema_upgrade_preserves_data(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        legacy = sqlite3.connect(legacy_path)
        legacy.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for statement in _MIGRATIONS[1]:
            legacy.execute(statement)
        legacy.execute("INSERT INTO schema_migrations VALUES (1, 'legacy')")
        legacy.execute("INSERT INTO accounts(id, provider, created_at) VALUES ('legacy-account', 'tongji', 'legacy')")
        legacy.execute(
            "INSERT INTO courses(id, account_id, platform_course_id, title, source) "
            "VALUES ('legacy-course', 'legacy-account', 'platform-course', 'Legacy course', 'platform')"
        )
        legacy.execute(
            "INSERT INTO lectures(id, course_id, platform_sub_id, title) "
            "VALUES ('legacy-lecture', 'legacy-course', 'platform-lecture', 'Legacy lecture')"
        )
        legacy.execute(
            "INSERT INTO workflows(id, lecture_id, operation, state, target_json, created_at, updated_at) "
            "VALUES ('legacy-workflow', 'legacy-lecture', 'organize', 'queued', '{\"session_title\":\"Legacy lecture\"}', 'legacy', 'legacy')"
        )
        legacy.commit()
        legacy.close()

        upgraded = ConsoleStorage(legacy_path)
        restored = upgraded.get_workflow("legacy-workflow")
        self.assertEqual(restored.target["session_title"], "Legacy lecture")
        self.assertEqual(upgraded.get_course("legacy-course", "legacy-account").title, "Legacy course")
        versions = [row[0] for row in upgraded.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
        self.assertEqual(versions, [1, 2, 3, 4, 5])
        upgraded.close_thread_connection()

    def test_settings_revision_rejects_secrets(self):
        with self.assertRaisesRegex(ValueError, "secret fields"):
            self.storage.create_settings_revision({"text_model": {"api_key": "synthetic-secret"}})

    def test_workflow_target_rejects_secret_fields(self):
        account = self.storage.upsert_account(
            platform_account_id="synthetic-target-user", provider="tongji"
        )
        course = self.storage.upsert_course(
            account_id=account.id, platform_course_id="course-101", title="Synthetic course"
        )
        lecture = self.storage.upsert_lecture(
            course_id=course.id,
            platform_sub_id="lecture-202",
            title="Synthetic lecture",
        )
        with self.assertRaisesRegex(ValueError, "secret fields"):
            self.storage.create_workflow(
                lecture_id=lecture.id,
                operation="organize",
                settings_revision_id=None,
                target={"session_title": "Synthetic lecture", "signed_url": "private"},
            )

    def test_import_courses_with_same_title_keep_distinct_identity(self):
        account = self.storage.upsert_account(
            platform_account_id="synthetic-import-user", provider="local"
        )
        first = self.storage.upsert_import_course(
            account_id=account.id, import_key="folder-a", title="Same title"
        )
        repeated = self.storage.upsert_import_course(
            account_id=account.id, import_key="folder-a", title="Same title"
        )
        second = self.storage.upsert_import_course(
            account_id=account.id, import_key="folder-b", title="Same title"
        )
        self.assertEqual(first.id, repeated.id)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.identity_kind, "import")
        self.assertIsNone(first.platform_course_id)
        self.assertEqual(len(self.storage.list_courses(account.id, source="local")), 2)

    def test_sync_failure_retains_last_success_timestamp(self):
        account = self.storage.upsert_account(
            platform_account_id="synthetic-sync-user", provider="tongji"
        )
        self.storage.set_course_sync_status(account.id, "syncing")
        succeeded = self.storage.set_course_sync_status(account.id, "succeeded")
        failed = self.storage.set_course_sync_status(
            account.id,
            "failed",
            error={"code": "COURSE_SYNC_FAILED", "message": "Synthetic failure"},
        )
        self.assertIsNotNone(succeeded.last_success_at)
        self.assertEqual(failed.last_success_at, succeeded.last_success_at)
        self.assertEqual(failed.state, "failed")
        self.assertEqual(failed.error["code"], "COURSE_SYNC_FAILED")

    def test_log_offsets_are_absolute_and_bounded(self):
        log_path = Path(self.temp_dir.name) / "attempt.log"
        log_path.write_bytes("甲\nsecond\nthird\n".encode("utf-8"))
        first = self.storage.read_log_offset(log_path, offset=0, max_bytes=4)
        second = self.storage.read_log_offset(
            log_path, offset=first.next_offset, max_bytes=7
        )
        self.assertEqual(first.text, "甲\n")
        self.assertTrue(first.truncated)
        self.assertEqual(second.offset, 4)
        self.assertEqual(second.text, "second\n")
        self.assertEqual(second.next_offset, 11)

    def test_log_offset_past_end_clamps_to_file_size(self):
        log_path = Path(self.temp_dir.name) / "short.log"
        log_path.write_bytes(b"short")
        result = self.storage.read_log_offset(log_path, offset=99)
        self.assertEqual(result.offset, 5)
        self.assertEqual(result.next_offset, 5)
        self.assertEqual(result.text, "")
        self.assertFalse(result.truncated)

    def test_database_connections_are_thread_local(self):
        main_connection = self.storage.connection
        worker_connections = []

        def read_count():
            worker_connections.append(self.storage.connection)
            self.storage.connection.execute("SELECT COUNT(*) FROM accounts").fetchone()
            self.storage.close_thread_connection()

        worker = threading.Thread(target=read_count)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(worker_connections), 1)
        self.assertIsNot(worker_connections[0], main_connection)

    def test_concurrent_initialization_applies_schema_once(self):
        db_path = Path(self.temp_dir.name) / "parallel.sqlite3"
        barrier = threading.Barrier(2)
        errors = []

        def initialize():
            store = None
            try:
                barrier.wait(timeout=5)
                store = ConsoleStorage(db_path)
                versions = [
                    row[0]
                    for row in store.connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                self.assertEqual(versions, [1, 2, 3, 4, 5])
            except Exception as error:  # surfaced on the test thread below
                errors.append(error)
            finally:
                if store is not None:
                    store.close_thread_connection()

        workers = [threading.Thread(target=initialize) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
