"""Exercise the real CLI, generated wiki, preview process and shutdown offline."""
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from console import config_store
from console.server import make_server


class OfflineWorkflow(unittest.TestCase):
    def test_selected_course_id_survives_detail_without_id(self):
        from scripts import look_tongji as cli
        from types import SimpleNamespace
        from unittest.mock import Mock
        client = Mock()
        client.get_course_detail.return_value = {'title':'示例课程','lectures':[{'sub_id':'202'}]}
        with patch.object(cli,'ensure_workspace_config',return_value=object()), patch.object(cli,'prepare_lecture_workspace') as prepare:
            cli._prepare_workspace_for_lecture(client, SimpleNamespace(),course_id='101',sub_id='202')
        self.assertEqual(prepare.call_args.kwargs['course_detail']['course_id'], '101')

    def test_course_without_id_uses_stable_digest(self):
        from tongji_backend.workspace import _course_slug
        import hashlib
        self.assertEqual(_course_slug('', '示例课程'), 'course-' + hashlib.sha256('示例课程'.encode()).hexdigest()[:12])
        self.assertEqual(_course_slug('101', '示例课程'), '101')

    def test_lecture_index_uses_class_date_instead_of_processing_date(self):
        from tongji_backend.workspace import _render_llmwiki_source
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder)/'raw/示例课程/旧目录/原始数据/manifest.json'
            manifest.parent.mkdir(parents=True)
            course, session, text = _render_llmwiki_source(manifest, {
                'course_id':'101','sub_id':'202','course_title':'示例课程',
                'session_title':'2026-09-14第9-11节','generated_at':'2026-10-12T10:00:00','base_name':'101_202'
            },site_name='示例知识库')
        self.assertEqual(course,'101')
        self.assertEqual(session,'2026-09-14-202')

    def test_empty_workspace_renders_original_reading_site(self):
        from tongji_backend.workspace import build_workspace_wiki
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {'LOOK_TONGJI_CONFIG_PATH': str(root/'config.json')}):
                config_store.save_workspace(workspace_root=str(root/'courses'),owner_name='Demo',site_name='Demo Courses')
                site = build_workspace_wiki(config_store.load_workspace_config())
                self.assertTrue((site/'style.css').is_file())
                self.assertTrue((site/'script.js').is_file())
                self.assertTrue((site/'projects'/'index.html').is_file())
                html = (site/'index.html').read_text(encoding='utf-8')
                self.assertIn('Demo Courses',html)
                self.assertIn('hero-home',html)
                self.assertEqual(list((root/'courses'/'raw'/'sessions').rglob('*.md')),[])

    def test_index_build_verify_preview_and_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {"LOOK_TONGJI_CONFIG_PATH": str(root / "config.json")}):
                config_store.save_workspace(workspace_root=str(root / "courses"), owner_name="Demo", site_name="Demo Courses")
                raw = root / "courses" / "raw" / "Demo" / "2026-10-08-session" / "原始数据"
                raw.mkdir(parents=True)
                for name, text in {"101_202.txt": "Synthetic transcript for local tests.", "101_202_notes.md": "### Notes\nA synthetic lesson.", "101_202_timeline.txt": "00:00-01:00：课程导论"}.items():
                    (raw / name).write_text(text, encoding="utf-8")
                (raw / "manifest.json").write_text(json.dumps({"course_id": "101", "sub_id": "202", "base_name": "101_202", "course_title": "Demo", "session_title": "Demo lesson", "generated_at": "2026-10-08", "artifacts": {"transcript_txt": str(raw / "101_202.txt"), "timeline_txt": str(raw / "101_202_timeline.txt")}}), encoding="utf-8")
                server = make_server("127.0.0.1", 0, "workflow-test-token")
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
                def api(path, body=None):
                    conn.request("POST" if body is not None else "GET", path, body=json.dumps(body) if body is not None else None, headers={"X-Console-Token": "workflow-test-token"})
                    response = conn.getresponse()
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 200, payload)
                    return payload
                try:
                    for kind in ("index", "build"):
                        task_id = api("/api/tasks", {"kind": kind, "params": {}})["task"]["id"]
                        deadline = time.monotonic() + 30
                        while time.monotonic() < deadline:
                            task = api(f"/api/tasks/{task_id}")["task"]
                            if task["status"] not in ("queued", "running"):
                                break
                            time.sleep(.05)
                        self.assertEqual(task["status"], "succeeded", api(f"/api/tasks/{task_id}/log"))
                        self.assertEqual(task['progress']['percent'], 100)
                        self.assertTrue(any(t['stage'] == kind for t in task['progress']['tracks']))
                    self.assertTrue(api("/api/agent/verify?kind=wiki")["ready"])
                    self.assertTrue(api("/api/agent/verify?kind=note&course_id=101&sub_id=202")["ready"])
                    preview = api("/api/tasks", {"kind": "serve", "params": {}})["task"]
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        preview = api(f'/api/tasks/{preview["id"]}')["task"]
                        if preview.get("preview_url"):
                            break
                        if preview["status"] == "failed":
                            self.fail(str(api(f'/api/tasks/{preview["id"]}/log')))
                        time.sleep(.05)
                    self.assertIn("preview_url", preview)
                    site = http.client.HTTPConnection("127.0.0.1", preview["params"]["port"], timeout=5)
                    site.request("GET", "/")
                    response = site.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"Demo", response.read())
                    site.close()
                    process = server.RequestHandlerClass.state.tasks.get(preview["id"])._proc
                finally:
                    conn.close()
                    server.shutdown()
                    server.server_close()
                    thread.join(5)
                self.assertIsNotNone(process.poll(), "preview child must stop with the console")


if __name__ == "__main__":
    unittest.main()
