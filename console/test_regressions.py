"""Offline regressions. Run: python -m unittest console.test_regressions -v."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from console import agent_bridge, config_store
from console.server import make_server, Handler
from console.runner import TaskManager


class HttpRegressions(unittest.TestCase):
    def test_closed_client_does_not_trigger_another_error_response(self):
        handler = object.__new__(Handler)
        handler.path = "/api/tasks"
        handler.headers = {"Host": f"127.0.0.1:{self.port}", "X-Console-Token": "test-session-token"}
        handler.server = self.server
        handler.state = self.server.RequestHandlerClass.state
        with patch.object(handler, "_route_get", side_effect=ConnectionAbortedError("browser closed")), patch.object(handler, "_send_json") as send:
            handler.do_GET()
            send.assert_not_called()
        self.assertTrue(handler.close_connection)

    def setUp(self):
        self.server = make_server("127.0.0.1", 0, "test-session-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, method="GET", body=None, headers=None):
        supplied = {"X-Console-Token": "test-session-token", **(headers or {})}
        raw = json.dumps(body) if body is not None else None
        self.conn.request(method, path, body=raw, headers=supplied)
        res = self.conn.getresponse()
        return res.status, dict(res.getheaders()), res.read()

    def test_each_keepalive_request_uses_its_own_body(self):
        with patch.object(config_store, "write_env") as write, patch.object(config_store, "public_credentials", return_value={}):
            self.request("/api/config/credentials", "POST", {"username": "first"})
            self.request("/api/config/credentials", "POST", {"username": "second"})
            self.assertEqual(write.call_args.args[0]["TONGJI_USERNAME"], "second")

    def test_refresh_can_authenticate_with_session_cookie(self):
        code, headers, _ = self.request("/?token=test-session-token")
        self.assertEqual(code, 200)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.request("/", headers={"X-Console-Token": "", "Cookie": cookie.split(";")[0]})[0], 200)

    def test_other_local_port_origin_is_rejected(self):
        self.assertEqual(self.request("/api/task-kinds", headers={"Origin": f"http://localhost:{self.port + 1}"})[0], 403)

    def test_invalid_log_offset_has_a_json_error(self):
        task = SimpleNamespace(snapshot=lambda: {}, tail=lambda offset: {})
        with patch.object(self.server.RequestHandlerClass.state.tasks, "get", return_value=task):
            code, _, raw = self.request("/api/tasks/test/log?offset=bad")
        self.assertEqual(code, 400)
        self.assertFalse(json.loads(raw)["ok"])

    def test_unknown_agent_is_rejected(self):
        with patch.object(agent_bridge, "detect_artifacts", return_value={}):
            self.assertEqual(self.request("/api/agent/instruction?kind=wiki&agent=unknown")[0], 400)

    def test_invalid_content_length_returns_json(self):
        code, _, raw = self.request("/api/tasks", "POST", headers={"Content-Length": "bad"})
        self.assertEqual(code, 400)
        self.assertFalse(json.loads(raw)["ok"])

    def test_verify_respects_optional_timeline(self):
        with patch.object(agent_bridge, "detect_artifacts", return_value={"notes": "notes.md", "timeline": ""}):
            code, _, raw = self.request("/api/agent/verify?kind=note&course_id=101&sub_id=202&include_timeline=0")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(raw)["ready"])

    def test_verify_dialogue_does_not_accept_standard_notes(self):
        with patch.object(agent_bridge, "detect_artifacts", return_value={"notes": "notes.md", "timeline": "timeline.txt"}):
            code, _, raw = self.request("/api/agent/verify?kind=note&course_id=101&sub_id=202&note_style=dialogue")
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(raw)["ready"])


class TaskRegressions(unittest.TestCase):
    def test_failed_batch_preparation_does_not_block_queue(self):
        manager = TaskManager()
        executed = threading.Event()
        with patch("console.runner.Path.mkdir", side_effect=PermissionError("test")):
            with self.assertRaises(PermissionError):
                manager.start("batch", {"course_id": "101"})
        with patch.object(manager, "_execute", side_effect=lambda task, env: executed.set()):
            manager.start("index", {})
            self.assertTrue(executed.wait(2))
            manager.shutdown()

    def test_failed_thread_start_rolls_back_queue(self):
        manager = TaskManager()
        with patch("console.runner.threading.Thread.start", side_effect=RuntimeError("test")):
            with self.assertRaises(ValueError):
                manager.start("index", {})
        self.assertEqual(manager.list(), [])
        executed = threading.Event()
        with patch.object(manager, "_execute", side_effect=lambda task, env: executed.set()):
            manager.start("build", {})
            self.assertTrue(executed.wait(2))
            manager.shutdown()

    def test_queue_order_does_not_depend_on_thread_start_order(self):
        manager = TaskManager()
        gates = [threading.Event() for _ in range(3)]
        executed = [threading.Event() for _ in range(3)]
        order = []
        original_run = manager._run
        def delayed_run(task, env):
            gates[int(task.params["course_id"])].wait(3)
            original_run(task, env)
        def fake_execute(task, env):
            number = int(task.params["course_id"])
            order.append(number)
            executed[number].set()
            task.status = "succeeded"
        with patch.object(manager, "_run", side_effect=delayed_run), patch.object(manager, "_execute", side_effect=fake_execute):
            try:
                for number in range(3):
                    manager.start("index", {"course_id": str(number)})
                gates[2].set()
                self.assertFalse(executed[2].wait(.2))
                gates[1].set()
                self.assertFalse(executed[1].wait(.2))
                gates[0].set()
                for thread in list(manager._threads.values()):
                    thread.join(3)
                self.assertEqual(order, [0, 1, 2])
            finally:
                for gate in gates:
                    gate.set()
                manager.shutdown()

    def test_mutating_tasks_are_queued_and_queued_cancel_does_not_execute(self):
        manager = TaskManager()
        entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
        def fake_execute(task, env):
            if task.kind == "index":
                entered.set()
                release.wait(5)
            else:
                second_entered.set()
            task.status = "succeeded"
        with patch.object(manager, "_execute", side_effect=fake_execute, create=True), patch("console.runner.subprocess.Popen", side_effect=RuntimeError("unexpected execution")):
            first = manager.start("index", {})
            self.assertTrue(entered.wait(2))
            second = manager.start("build", {})
            manager.cancel(second.id)
            self.assertFalse(second_entered.wait(.2))
            release.set()
            for thread in list(manager._threads.values()):
                thread.join(3)
            self.assertEqual(second.status, "cancelled")
        self.assertEqual(first.status, "succeeded")


class VisionRegressions(unittest.TestCase):
    def test_blank_key_preserves_saved_key_for_same_endpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(config_store, "VISION_DIR", root), patch.object(config_store, "VISION_CONFIG_PATH", root / "config.json"):
                config_store.save_vision(provider_id="custom", model="vision-one", api_key="test-key", base_url="https://example.test/v1")
                config_store.save_vision(provider_id="custom", model="vision-two", api_key="", base_url="https://example.test/v1")
                self.assertEqual(config_store.read_vision()["models"][0]["apiKey"], "test-key")
                self.assertEqual(config_store.public_vision()["provider"], "custom")

    def test_different_endpoint_does_not_inherit_a_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(config_store, "VISION_DIR", root), patch.object(config_store, "VISION_CONFIG_PATH", root / "config.json"):
                config_store.save_vision(provider_id="custom", model="vision", api_key="test-key", base_url="https://one.test/v1")
                with self.assertRaises(ValueError):
                    config_store.save_vision(provider_id="custom", model="vision", api_key="", base_url="https://two.test/v1")


class ArtifactRegressions(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.raw = self.root / "raw" / "course" / "session" / "原始数据"
        self.raw.mkdir(parents=True)
        self.config = patch.object(agent_bridge, "load_workspace_config", return_value=SimpleNamespace(workspace_root=self.root))
        self.config.start()
        (self.raw / "manifest.json").write_text(json.dumps({"course_id": "101", "sub_id": "202", "base_name": "101_202"}), encoding="utf-8")

    def tearDown(self):
        self.config.stop()
        self.folder.cleanup()

    def test_empty_files_are_not_completed_artifacts(self):
        (self.raw / "101_202_notes.md").touch()
        (self.raw / "101_202_timeline.txt").touch()
        self.assertFalse(agent_bridge.detect_artifacts("101", "202")["notes_ready"])

    def test_teacher_handout_is_not_a_transcript(self):
        (self.raw / "教师_讲义.txt").write_text("teacher handout", encoding="utf-8")
        self.assertFalse(agent_bridge.detect_artifacts("101", "202")["has_transcript"])

    def test_legacy_slide_index_can_identify_a_lecture_without_manifest(self):
        (self.raw / "manifest.json").unlink()
        slides = self.raw / "slides"
        slides.mkdir()
        (slides / "index.json").write_text(json.dumps({"course_id": "101", "sub_id": "202"}), encoding="utf-8")
        (slides / "01.jpg").write_bytes(b"fixture")
        artifacts = agent_bridge.detect_artifacts("101", "202")
        self.assertEqual(artifacts["lecture_dir"], str(self.raw))
        self.assertEqual(artifacts["slide_count"], 1)

    def test_sub_id_does_not_override_an_explicit_other_course(self):
        self.assertIsNone(agent_bridge.find_lecture_dir("999", "202"))

    def test_timeline_instruction_is_executable(self):
        payload = agent_bridge.build_agent_instruction(kind="note", course_id="101", sub_id="202", artifacts={"lecture_dir": str(self.raw)})
        self.assertIn('timeline-normalize --input "', payload["instruction"])

    def test_instruction_identifies_the_course_and_lecture(self):
        manifest = {"course_id": "101", "sub_id": "202", "base_name": "101_202", "course_title": "高等数学", "session_title": "第 3 讲：导数", "teacher": "示例教师", "date": "2026-10-08"}
        (self.raw / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        artifacts = agent_bridge.detect_artifacts("101", "202")
        payload = agent_bridge.build_agent_instruction(kind="note", course_id="101", sub_id="202", artifacts=artifacts)
        for expected in ("高等数学", "第 3 讲：导数", "示例教师", "课程 ID：101", "节次 ID：202", "仅处理当前节次"):
            self.assertIn(expected, payload["instruction"])

    def test_wiki_instruction_describes_its_whole_workspace_scope(self):
        payload = agent_bridge.build_agent_instruction(kind="wiki", course_id="101", sub_id="202")
        self.assertIn("当前知识库的全部课程", payload["instruction"])


if __name__ == "__main__":
    unittest.main()
