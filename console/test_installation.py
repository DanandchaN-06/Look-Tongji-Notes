from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from console.core import redact, redact_env_values
from console import portable_launcher
from console.server import make_server


class InstallationBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.token = "installation-test-token"
        cls.server = make_server("127.0.0.1", 0, cls.token)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def fetch(self, path: str, headers: dict[str, str] | None = None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            return urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            return error

    def test_static_path_traversal_rejected(self):
        response = self.fetch("/v2/%252e%252e/state/console-v2.sqlite3")
        self.assertEqual(response.code, 403)

    def test_all_v2_routes_require_auth(self):
        for path in ("/api/v2/settings", "/api/v2/courses", "/api/v2/workflows"):
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path).code, 401)

    def test_non_local_origin_rejected(self):
        response = self.fetch(
            "/api/v2/settings",
            {"X-Console-Token": self.token, "Origin": "http://attacker.example"},
        )
        self.assertEqual(response.code, 403)

    def test_secret_redaction(self):
        message = redact("Authorization: Bearer sensitive-token password=private api_key=secret-key")
        message = redact_env_values(message, ["known-local-secret"])
        for secret in ("sensitive-token", "private", "secret-key"):
            self.assertNotIn(secret, message)
        self.assertNotIn("known-local-secret", redact_env_values("oops known-local-secret", ["known-local-secret"]))

    def test_fresh_machine_start_serves_prebuilt_ui_without_node(self):
        manifest_path = "console/frontend/package.json"
        root = Path(__file__).resolve().parent.parent
        package = json.loads((root / manifest_path).read_text(encoding="utf-8"))
        self.assertIn("build", package["scripts"])
        root = Path(__file__).resolve().parent.parent
        clean_path = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
        self.assertIsNone(__import__("shutil").which("node", path=clean_path))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = os.environ.copy()
        env["PATH"] = clean_path
        env["APPDATA"] = str(Path(__import__("tempfile").gettempdir()) / "look-console-clean-start")
        env["PYTHONUTF8"] = "1"
        output_queue: queue.Queue[str] = queue.Queue()
        process = subprocess.Popen(
            [__import__("sys").executable, str(root / "console" / "start.py"), "--no-window", "--port", str(port), "--print-url"],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        reader = threading.Thread(target=lambda: [output_queue.put(line) for line in process.stdout], daemon=True)
        reader.start()
        app_url = ""
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    line = output_queue.get(timeout=.2).strip()
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line.startswith("CONSOLE_URL="):
                    app_url = line.split("=", 1)[1]
                    break
            self.assertTrue(app_url, "本地控制台未能启动")
            parsed = urllib.parse.urlsplit(app_url)
            request = urllib.request.Request(f"http://127.0.0.1:{parsed.port}/v2/?{parsed.query}")
            with urllib.request.urlopen(request, timeout=5) as response:
                html = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("Content-Security-Policy", response.headers)
                self.assertIn("/v2/assets/", html)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            reader.join(timeout=1)
            if process.stdout:
                process.stdout.close()

    @unittest.skipUnless(os.name == "nt", "desktop launcher lock uses Windows msvcrt")
    def test_double_launch_reuses_instance(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            record = {"port": 12345, "token": "synthetic-token", "pid": 123}
            (state / "manager-service.json").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(portable_launcher, "APP", root), \
                 patch.object(portable_launcher, "STATE", state), \
                 patch.object(portable_launcher, "healthy", return_value=True), \
                 patch.object(portable_launcher.subprocess, "Popen") as popen:
                reused_record, reused = portable_launcher.launch(root / "project", manager=True)
            self.assertTrue(reused)
            self.assertEqual(reused_record, record)
            popen.assert_not_called()

    def test_manager_entry_opens_new_console_and_reader_entry_stays_original(self):
        record = {"port": 8765, "token": "synthetic-token"}
        self.assertEqual(
            portable_launcher.service_url(record, manager=True),
            "http://127.0.0.1:8765/v2/?token=synthetic-token",
        )
        self.assertEqual(portable_launcher.service_url(record), "http://127.0.0.1:8765/")


if __name__ == "__main__":
    unittest.main()
