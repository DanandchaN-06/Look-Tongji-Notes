from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from console.storage import ConsoleStorage
from console.reader_service import ReaderBuildService


class ReaderBuildServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.storage = ConsoleStorage(self.root / "console.sqlite3")

    def tearDown(self):
        self.storage.close_thread_connection()
        self.temp.cleanup()

    def test_batch_completion_coalesces_builds(self):
        started = threading.Event()
        release = threading.Event()
        builds = []

        def build(target):
            builds.append(target)
            started.set()
            self.assertTrue(release.wait(3))
            target.mkdir(parents=True, exist_ok=True)
            (target / "index.html").write_text("new", encoding="utf-8")

        service = ReaderBuildService(self.storage, self.root, build=build, auto_start=False)
        service.request_build()
        service.start()
        self.assertTrue(started.wait(3))
        service.request_build()
        service.request_build()
        release.set()
        self.assertTrue(service.wait_for_generation(3, timeout=5))
        self.assertEqual(len(builds), 2)
        self.assertEqual(self.storage.get_reader_build_state()["published_generation"], 3)
        service.shutdown()

    def test_build_during_changes_runs_again(self):
        started = threading.Event()
        release = threading.Event()
        count = []

        def build(target):
            count.append(len(count) + 1)
            if len(count) == 1:
                started.set()
                self.assertTrue(release.wait(3))
            target.mkdir(parents=True, exist_ok=True)
            (target / "index.html").write_text(str(len(count)), encoding="utf-8")

        service = ReaderBuildService(self.storage, self.root, build=build)
        service.request_build()
        self.assertTrue(started.wait(3))
        service.request_build()
        release.set()
        self.assertTrue(service.wait_for_generation(2, timeout=5))
        self.assertEqual(len(count), 2)
        self.assertEqual((self.root / "site" / "index.html").read_text(encoding="utf-8"), "2")
        service.shutdown()

    def test_failed_build_preserves_previous_site(self):
        site = self.root / "site"
        site.mkdir()
        (site / "index.html").write_text("old page", encoding="utf-8")

        def fail(_target):
            raise RuntimeError("synthetic build failure")

        service = ReaderBuildService(self.storage, self.root, build=fail, auto_start=False)
        service.request_build()
        service.start()
        self.assertTrue(service.wait_for_state({"failed"}, timeout=5))
        self.assertEqual((site / "index.html").read_text(encoding="utf-8"), "old page")
        self.assertIn("synthetic build failure", service.status()["last_error"])
        service.shutdown()

    def test_timeline_buttons_seek_to_their_start_second(self):
        from llmwiki import build as wiki_build

        timeline = self.root / "lesson_timeline.txt"
        timeline.write_text("01:00-02:00：合成主题\n", encoding="utf-8")
        with patch.object(wiki_build, "REPO_ROOT", self.root):
            block = wiki_build._session_media_block({
                "look_tongji_video_url": "https://example.invalid/synthetic.mp4",
                "look_tongji_timeline": "lesson_timeline.txt",
            })
        self.assertIn('data-seek="60"', block)
        reader_script = Path(wiki_build.__file__).parent / "render" / "js.py"
        self.assertIn("art.video.currentTime = sec;", reader_script.read_text(encoding="utf-8"))

    def test_reader_open_returns_the_existing_site_url(self):
        from console.course_service import CourseService
        from console.routes_v2 import V2Router

        site = self.root / "workspace" / "site"
        site.mkdir(parents=True)
        (site / "index.html").write_text("current", encoding="utf-8")
        service = ReaderBuildService(self.storage, site.parent, auto_start=False)
        courses = CourseService(
            self.storage, adapter=object(), account_identity=lambda: None,
            workspace_root=lambda: site.parent,
        )
        router = V2Router(courses, workflows=type("WorkflowManager", (), {"storage": self.storage})(), reader_service=service)
        status, response = router.post("/api/v2/reader/open", {})
        self.assertEqual(status, 200)
        self.assertTrue(response["data"]["ready"])
        self.assertEqual(response["data"]["url_path"], "/reader/")
        service.shutdown()


if __name__ == "__main__":
    unittest.main()
