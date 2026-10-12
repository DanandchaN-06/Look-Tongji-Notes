from __future__ import annotations

import threading
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from console.runner import TaskManager
from scripts.tongji_backend.media_cache import account_auth_refresh_lock


class SchedulerTests(unittest.TestCase):
    def test_auth_refresh_is_serialized_for_same_account(self):
        with tempfile.TemporaryDirectory() as folder:
            state_dir = Path(folder)
            first_inside, second_attempt, release_first, second_inside = (threading.Event() for _ in range(4))
            overlap = []

            def first():
                with account_auth_refresh_lock(state_dir, "same-account"):
                    first_inside.set()
                    release_first.wait(3)
                    overlap.append(second_inside.is_set())

            def second():
                second_attempt.set()
                with account_auth_refresh_lock(state_dir, "SAME-ACCOUNT"):
                    second_inside.set()

            a = threading.Thread(target=first)
            a.start()
            self.assertTrue(first_inside.wait(2))
            b = threading.Thread(target=second)
            b.start()
            try:
                self.assertTrue(second_attempt.wait(2))
                self.assertFalse(second_inside.is_set())
            finally:
                release_first.set()
            a.join(2)
            b.join(2)
            self.assertEqual(overlap, [False])
            self.assertTrue(second_inside.is_set())

    def test_two_lectures_progress_concurrently(self):
        manager = TaskManager()
        entered = threading.Condition()
        started = set()
        release = threading.Event()

        def execute(task, _env):
            with entered:
                started.add(task.id)
                entered.notify_all()
            release.wait(3)
            task.status = "succeeded"

        manager._execute = execute
        try:
            a = manager.start("transcribe", {"course_id": "c", "sub_id": "one"},
                              account_id="acct", parallelism=2)
            b = manager.start("slide", {"course_id": "c", "sub_id": "two"},
                              account_id="acct", parallelism=2)
            with entered:
                self.assertTrue(entered.wait_for(lambda: len(started) == 2, timeout=3))
            self.assertEqual(started, {a.id, b.id})
        finally:
            release.set()
            manager.shutdown()

    def test_asr_slot_is_limited_to_one(self):
        manager = TaskManager()
        first_entered, release, second_entered = (threading.Event() for _ in range(3))
        count = 0
        lock = threading.Lock()

        def execute(task, _env):
            nonlocal count
            with lock:
                count += 1
                first = count == 1
            if first:
                first_entered.set()
                release.wait(3)
            else:
                second_entered.set()
            task.status = "succeeded"

        manager._execute = execute
        try:
            manager.start("transcribe", {"course_id": "c", "sub_id": "one"}, account_id="acct", parallelism=2)
            self.assertTrue(first_entered.wait(2))
            manager.start("transcribe", {"course_id": "c", "sub_id": "two"}, account_id="acct", parallelism=2)
            self.assertFalse(second_entered.wait(0.15))
            release.set()
            self.assertTrue(second_entered.wait(2))
        finally:
            release.set()
            manager.shutdown()

    def test_download_slot_allows_two_lectures(self):
        manager = TaskManager()
        entered = threading.Condition()
        started = set()
        release = threading.Event()

        def execute(task, _env):
            with entered:
                started.add(task.id)
                entered.notify_all()
            release.wait(3)
            task.status = "succeeded"

        manager._execute = execute
        try:
            a = manager.start("slide", {"course_id": "c", "sub_id": "one"}, account_id="acct", parallelism=2)
            b = manager.start("slide", {"course_id": "c", "sub_id": "two"}, account_id="acct", parallelism=2)
            with entered:
                self.assertTrue(entered.wait_for(lambda: len(started) == 2, timeout=3))
            self.assertEqual(started, {a.id, b.id})
        finally:
            release.set()
            manager.shutdown()

    def test_same_lecture_is_exclusive(self):
        manager = TaskManager()
        first_entered, release = threading.Event(), threading.Event()
        second_entered = threading.Event()
        call_count = 0
        lock = threading.Lock()

        def execute(task, _env):
            nonlocal call_count
            with lock:
                call_count += 1
                first = call_count == 1
            if first:
                first_entered.set()
                release.wait(3)
            else:
                second_entered.set()
            task.status = "succeeded"

        manager._execute = execute
        try:
            manager.start("transcribe", {"course_id": "c", "sub_id": "same"}, account_id="acct", parallelism=2)
            self.assertTrue(first_entered.wait(2))
            manager.start("slide", {"course_id": "c", "sub_id": "same"}, account_id="acct", parallelism=2)
            self.assertFalse(second_entered.wait(0.15))
            release.set()
            self.assertTrue(second_entered.wait(2))
        finally:
            release.set()
            manager.shutdown()

    def test_failed_attempt_releases_resources(self):
        manager = TaskManager()
        first_entered, second_entered = threading.Event(), threading.Event()
        calls = Counter()

        def execute(task, _env):
            calls[task.params["sub_id"]] += 1
            if task.params["sub_id"] == "first":
                first_entered.set()
                task.status = "failed"
                raise RuntimeError("synthetic runner error")
            second_entered.set()
            task.status = "succeeded"

        manager._execute = execute
        try:
            manager.start("transcribe", {"course_id": "c", "sub_id": "first"}, account_id="acct", parallelism=2)
            manager.start("transcribe", {"course_id": "c", "sub_id": "next"}, account_id="acct", parallelism=2)
            self.assertTrue(first_entered.wait(2))
            self.assertTrue(second_entered.wait(2))
            self.assertEqual(calls, Counter({"first": 1, "next": 1}))
        finally:
            manager.shutdown()

    def test_cancel_queued_job_never_executes(self):
        manager = TaskManager()
        first_entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()

        def execute(task, _env):
            if task.params["sub_id"] == "first":
                first_entered.set()
                release.wait(3)
            else:
                second_entered.set()
            task.status = "succeeded"

        manager._execute = execute
        try:
            manager.start("slide", {"course_id": "c", "sub_id": "first"}, account_id="acct", parallelism=1)
            self.assertTrue(first_entered.wait(2))
            second = manager.start("slide", {"course_id": "c", "sub_id": "second"}, account_id="acct", parallelism=1)
            manager.cancel(second.id)
            release.set()
            self.assertFalse(second_entered.wait(0.2))
            self.assertEqual(second.status, "cancelled")
        finally:
            release.set()
            manager.shutdown()

    def test_many_queued_jobs_use_only_fixed_workers(self):
        manager = TaskManager()
        first_entered, release = threading.Event(), threading.Event()

        def execute(task, _env):
            if task.params.get("course_id") == "0":
                first_entered.set()
                release.wait(3)
            task.status = "succeeded"

        manager._execute = execute
        try:
            manager.start("index", {"course_id": "0"})
            self.assertTrue(first_entered.wait(2))
            for number in range(1, 13):
                manager.start("index", {"course_id": str(number)})
            self.assertEqual(len(manager._scheduler._threads), 4)
        finally:
            release.set()
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
