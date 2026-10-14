"""Isolated browser checks with synthetic courses; no account or model calls.

Run: python -m unittest console.browser_smoke -v
Screenshots: set LOOK_CONSOLE_SCREENSHOTS to a local directory.
"""
from __future__ import annotations

import json
import os
import re
import threading
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright
from console.server import make_server
from console.features import FEATURES


class BrowserSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0, "browser-test-token")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/?token=browser-test-token"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 960})
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.task_requests = []
        self.test_tasks = []
        self.notes_ready = False
        self.manifest_course_title = ''
        self.settings_posts = []
        self.text_model_test_calls = 0
        self.page.route("**/api/**", self.route)
        self.page.goto(self.url)
        self.page.wait_for_selector("#statDeps.ok")

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def route(self, route):
        request = route.request
        path = request.url.split("/api/")[1]
        if path == "v2/courses/sync":
            return route.fulfill(json={"ok": True, "data": {"sync_job": {"course_count": 2, "lecture_count": 2}, "cache": None}})
        if path.startswith("v2/courses?"):
            courses = getattr(self, "v2_courses", [
                {"id": "course-A", "platform_course_id": "platform-course-A", "title": "Synthetic A", "teacher": "Teacher A", "term": None, "source": "local", "identity_kind": "platform", "lecture_count": 1},
                {"id": "course-B", "platform_course_id": "platform-course-B", "title": "Synthetic B", "teacher": None, "term": None, "source": "local", "identity_kind": "platform", "lecture_count": 1},
            ])
            return route.fulfill(json={"ok": True, "data": {"source": "all", "query": "", "courses": courses, "sync_status": None}})
        lecture_match = re.search(r"v2/courses/([^/?]+)/lectures(?:/([^/?]+)/artifacts)?", path)
        if lecture_match:
            course_id, lecture_id = lecture_match.groups()
            lectures = getattr(self, "v2_lectures", {
                "course-A": [{"id": "lecture-A", "course_id": "course-A", "platform_sub_id": "platform-A", "title": "First lecture", "class_date": "2026-09-14", "playback_available": True}],
                "course-B": [{"id": "lecture-B", "course_id": "course-B", "platform_sub_id": "platform-B", "title": "Second lecture", "class_date": "2026-09-15", "playback_available": True}],
            })
            if lecture_id:
                artifacts = getattr(self, "v2_artifacts", {"lecture_id": "lecture-A", "course_id": course_id, "items": [
                    {"kind": "transcript", "label": "字幕", "state": "available", "message": "可用"},
                    {"kind": "slides", "label": "课件截图", "state": "not_collected", "message": "尚未采集"},
                    {"kind": "notes", "label": "学习笔记", "state": "available", "message": "可用"},
                    {"kind": "timeline", "label": "时间轴", "state": "expired", "message": "索引记录的文件已不存在或不可用"}], "notes_preview": "Synthetic notes preview", "source": "本地资料索引"})
                return route.fulfill(json={"ok": True, "data": {**artifacts, "lecture_id": lecture_id, "course_id": course_id}})
            course = next((item for item in getattr(self, "v2_courses", []) if item["id"] == course_id), None)
            return route.fulfill(json={"ok": True, "data": {"course": course or {"id": course_id, "title": "Synthetic course", "teacher": None, "term": None}, "lectures": lectures.get(course_id, [])}})
        if path == "v2/workflows" and request.method == "POST":
            self.workflow_posts = getattr(self, "workflow_posts", [])
            self.workflow_posts.append({"body": request.post_data_json, "key": request.headers.get("idempotency-key")})
            if request.post_data_json.get("operation") == "agent_note":
                self.v2_workflow = {
                    "id": "workflow-agent-synthetic", "lecture_id": "lecture-A", "operation": "agent_note", "state": "waiting_agent",
                    "settings_revision_id": "settings-synthetic", "created_at": "2026-10-12T08:30:00Z", "updated_at": "2026-10-12T08:30:00Z",
                    "cancel_requested": False,
                    "target": {"course_title": "Synthetic A", "lecture_title": "First lecture", "class_date": "2026-09-14", "platform_sub_id": "platform-A", "operation_label": "外部 Agent 写作"},
                    "steps": [{"id": "step-agent-synthetic", "workflow_id": "workflow-agent-synthetic", "name": "agent_note", "ordinal": 0, "state": "waiting_agent",
                        "artifact": {"title": "生成学习笔记", "warnings": []}, "error_code": None, "error": {}, "attempts": []}],
                }
                return route.fulfill(status=201, json={"ok": True, "data": self.v2_workflow, "created": True, "instruction": "请只为 First lecture 生成笔记。"})
            self.v2_workflow = {
                "id": "workflow-synthetic", "lecture_id": "lecture-A", "operation": "transcribe", "state": "succeeded",
                "settings_revision_id": "settings-synthetic", "created_at": "2026-10-12T08:30:00Z", "updated_at": "2026-10-12T08:30:02Z",
                "cancel_requested": False,
                "target": {"course_title": "Synthetic A", "lecture_title": "First lecture", "class_date": "2026-09-14", "platform_sub_id": "platform-A", "operation_label": "只取字幕"},
                "steps": [{"id": "step-synthetic", "workflow_id": "workflow-synthetic", "name": "transcribe", "ordinal": 0, "state": "succeeded",
                    "artifact": {"validated": True}, "error_code": None, "error": {},
                    "attempts": [{"id": "attempt-synthetic", "step_id": "step-synthetic", "number": 1, "state": "succeeded",
                        "started_at": "2026-10-12T08:30:00Z", "ended_at": "2026-10-12T08:30:02Z", "exit_code": 0,
                        "progress": {"label": "字幕成果已校验"}, "log_path": None}]}],
            }
            return route.fulfill(status=201, json={"ok": True, "data": self.v2_workflow, "created": True})
        if re.fullmatch(r"v2/workflows/[^/?]+/agent-instruction", path):
            return route.fulfill(json={"ok": True, "data": {"instruction": "请只为 First lecture 生成笔记。"}})
        if re.fullmatch(r"v2/workflows/[^/?]+/verify-agent", path):
            return route.fulfill(json={"ok": True, "data": {"ready": False, "message": "Agent 任务仍在等待；指令复制或保存不会被视为完成。", "workflow": self.v2_workflow}})
        if path.startswith("v2/workflows/") and "/attempts/" in path:
            offset = int(path.split("offset=")[-1]) if "offset=" in path else 0
            text = "Synthetic persistent workflow log\n" if offset == 0 else ""
            size = len(text.encode("utf-8"))
            return route.fulfill(json={"ok": True, "data": {"offset": offset, "next_offset": offset + size, "size": offset + size, "text": text, "truncated": False}})
        if path == "v2/workflows":
            workflows = [self.v2_workflow] if getattr(self, "v2_workflow", None) else []
            return route.fulfill(json={"ok": True, "data": workflows})
        if re.fullmatch(r"v2/workflows/[^/?]+", path):
            workflow = getattr(self, "v2_workflow", None)
            if workflow:
                return route.fulfill(json={"ok": True, "data": workflow})
            return route.fulfill(status=404, json={"ok": False, "error": {"message": "not found"}})
        if path == "v2/settings/test/text_model":
            self.text_model_test_calls += 1
            return route.fulfill(json={"ok": True, "data": {"success": True, "message": "synthetic model endpoint ready"}})
        if path == "v2/settings":
            settings = {
                "revision_id": "synthetic-revision",
                "account": {"username": "", "username_set": False, "password_set": False,
                            "workspace_root": "C:/Synthetic/Courses", "owner_name": "", "site_name": ""},
                "asr": {
                    "provider": "bcutasr", "availability": "unknown",
                    "upload_target": "BcutASR 云端识别服务",
                    "providers": [
                        {"id": "bcutasr", "name": "BcutASR", "availability": "unknown", "upload_target": "BcutASR 云端识别服务"},
                        {"id": "local_whisper", "name": "本地 Whisper", "availability": "unavailable", "upload_target": "本机"},
                    ],
                },
                "note": {"route": "external_agent", "style": "standard", "include_timeline": True},
                "text_model": {"provider": "openai-compatible", "model": "", "base_url": "",
                               "configured": False, "key_configured": False, "providers": ["openai-compatible", "ollama"]},
                "vision_model": {"provider": "", "model": "", "base_url": "", "configured": False,
                                 "key_configured": False, "providers": [{"id": "custom", "name": "自定义", "baseUrl": "", "models": []}]},
                "slides": {"concurrency": 4, "retries": 3, "timeout_seconds": 30, "allow_without_screenshots": True},
                "tasks": {"course_parallelism": 2, "asr_parallelism": 1, "retry_limit": 3},
                "reader": {"auto_update": True, "publish_repository": ""},
            }
            if request.method == "POST":
                posted = request.post_data_json
                self.settings_posts.append(posted)
                if "text_model" in posted:
                    settings["text_model"].update({
                        key: value for key, value in posted["text_model"].items()
                        if key in {"provider", "model", "base_url"}
                    })
                    settings["text_model"]["configured"] = bool(posted["text_model"].get("api_key"))
                    settings["text_model"]["key_configured"] = bool(posted["text_model"].get("api_key"))
                return route.fulfill(json={"ok": True, "data": {"revision_id": "synthetic-revision-2", "settings": settings}})
            return route.fulfill(json={"ok": True, "data": settings})
        workspace = {"workspace_root": "C:/Courses", "site_built": False, "lecture_count": 0}
        artifacts = {"workspace_configured": True, "lecture_dir": "C:/Courses/raw/示例课程/第1讲/原始数据", "has_notes": self.notes_ready, "notes": "notes.md" if self.notes_ready else "", "notes_ready": False, "slide_count": 0}
        artifacts.update({"course_id": "101", "sub_id": "202", "course_title": self.manifest_course_title})
        if path == "status":
            payload = {"ok": True, "deps_ok": True, "missing_deps": [], "repo_root": "C:/Projects/look-tongji-notes", "credentials": {"configured": True}, "workspace_config": workspace, "vision_model": {"configured": True}, "console": {"picker_available": False, "version": "0.3.0"}, "tools": {"python": "python", "node": "node", "ffmpeg": "ffmpeg"}}
        elif path == "config":
            payload = {"ok": True, "credentials": {"password_set": True}, "workspace": workspace, "vision": {"provider": "custom", "model": "test-vision", "base_url": "https://example.test/v1", "providers": [{"id": "custom", "name": "自定义", "models": [], "apiFormat": "openai"}]}}
        elif path.startswith("courses?"):
            payload = {"ok": True, "courses": [{"course_id": "101", "title": "高等数学 · 示例课程", "teacher": "示例教师"}]}
        elif path.startswith("lectures?"):
            payload = {"ok": True, "lectures": [{"sub_id": "202", "sub_title": "第 1 讲 · 课程导论", "date": "2026-10-08", "has_playback": True}]}
        elif path.startswith("artifacts?"):
            payload = {"ok": True, "artifacts": artifacts}
        elif path == "tasks" and request.method == "POST":
            self.task_requests.append(request.post_data_json)
            payload = {"ok": True, "task": {"id": "test-task"}}
        elif path == "tasks":
            payload = {"ok": True, "tasks": self.test_tasks}
        elif path.startswith("tasks/"):
            task_id = path.split('/')[1]
            task = next((t for t in self.test_tasks if t['id'] == task_id and t.get('progress')), None)
            payload = {"ok": True, "lines": ["log-" + task_id], "offset": 1, "task": task or {"status": "succeeded", "label": "测试任务"}}
        elif path.startswith("agent/verify?"):
            payload = {"ok": True, "ready": self.notes_ready, "missing": [], "message": "产物文件已生成"}
        elif path.startswith("agent/instruction?"):
            payload = {"ok": True, "instruction": "请在 Agent 中生成学习笔记。", "warnings": [], "artifacts": artifacts}
        else:
            payload = {"ok": True}
        route.fulfill(json=payload)

    def navigate(self, view):
        self.page.locator(f'.nav-item[data-view="{view}"]').click()

    def test_configuration_only_on_settings(self):
        console_warnings = []
        self.page.on("console", lambda message: console_warnings.append(message.text) if message.type in ("error", "warning") else None)
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/v2/?token=browser-test-token")
        self.page.get_by_role("heading", name="设置", exact=True).wait_for()
        self.assertEqual(self.page.locator("#text-api-key").input_value(), "")
        self.assertEqual(self.page.get_by_role("link", name="打开原阅读网页").get_attribute("href"), "/reader/")
        self.page.get_by_role("button", name="我的课程").click()
        self.assertEqual(self.page.locator("#text-api-key, #text-model, #text-base-url").count(), 0)
        self.page.get_by_role("button", name="任务中心").click()
        self.assertEqual(self.page.locator("#text-api-key, #text-model, #text-base-url, #workspace-root").count(), 0)
        self.page.get_by_role("button", name="设置").click()
        self.page.get_by_role("heading", name="笔记与 Agent").wait_for()
        self.page.get_by_role("button", name="设置").click()
        self.page.locator("#text-model").fill("synthetic-model")
        self.page.locator("#text-base-url").fill("https://model.example.invalid/v1")
        self.page.locator("#text-api-key").fill("synthetic-key-never-echoed")
        self.page.locator(".settings-toolbar .button-primary").click()
        self.page.get_by_role("status").get_by_text("设置已保存").wait_for()
        self.assertEqual(self.settings_posts[0]["text_model"]["api_key"], "synthetic-key-never-echoed")
        self.assertEqual(self.page.locator("#text-api-key").input_value(), "")
        self.page.on("dialog", lambda dialog: dialog.accept())
        self.page.locator("#settings-text-model .card-actions .button").click()
        self.page.locator("#settings-text-model .test-result").get_by_text("synthetic model endpoint ready").wait_for()
        self.assertEqual(self.text_model_test_calls, 1)
        screenshots = os.environ.get("LOOK_CONSOLE_SCREENSHOTS")
        if screenshots:
            folder = Path(screenshots)
            folder.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(folder / "v2-settings-desktop.png"))
            self.page.locator("#settings-text-model").scroll_into_view_if_needed()
            self.page.screenshot(path=str(folder / "v2-settings-models-desktop.png"))
            self.page.set_viewport_size({"width": 390, "height": 844})
            self.page.screenshot(path=str(folder / "v2-settings-models-mobile.png"))
            self.page.keyboard.press("Home")
            self.page.screenshot(path=str(folder / "v2-settings-mobile.png"))
        self.assertEqual(console_warnings, [])

    def test_v2_course_selection_survives_refresh_and_shows_real_lecture_artifacts(self):
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/v2/?token=browser-test-token")
        self.page.get_by_role("button", name="我的课程").click()
        self.page.get_by_role("button", name=re.compile("Synthetic A")).click()
        self.page.get_by_role("button", name=re.compile("First lecture")).click()
        self.page.get_by_text("课次 ID：platform-A").wait_for()
        self.page.get_by_text("预览学习笔记").click()
        self.page.get_by_text("Synthetic notes preview").wait_for()
        self.assertIn("可用", self.page.locator(".artifact-list").inner_text())
        self.page.reload()
        self.page.get_by_role("button", name=re.compile("我的课程")).click()
        self.page.get_by_text("课次 ID：platform-A").wait_for()
        self.assertIn("First lecture", self.page.locator(".lecture-detail").inner_text())
        self.page.locator("#course-search").fill("Synthetic")
        self.page.get_by_role("button", name="任务中心").click()
        self.page.get_by_role("button", name="我的课程").click()
        self.assertEqual(self.page.locator("#course-search").input_value(), "Synthetic")

    def test_late_response_ignored(self):
        self.page.add_init_script("""const originalFetch = window.fetch;
          window.fetch = (...args) => originalFetch(...args).then(res =>
            String(args[0]).includes('/course-A/lectures') && !String(args[0]).includes('/artifacts')
              ? new Promise(resolve => setTimeout(() => resolve(res), 350)) : res);""")
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/v2/?token=browser-test-token")
        self.page.get_by_role("button", name="我的课程").click()
        self.page.get_by_role("button", name=re.compile("Synthetic A")).click()
        self.page.get_by_role("button", name=re.compile("Synthetic B")).click()
        self.page.get_by_text("Second lecture").wait_for()
        self.page.wait_for_timeout(450)
        self.assertIn("Synthetic B", self.page.locator(".detail-title").inner_text())
        self.assertIn("Second lecture", self.page.locator(".lecture-list-panel").inner_text())
        self.assertNotIn("First lecture", self.page.locator(".lecture-list-panel").inner_text())

    def test_end_to_end_user_flow(self):
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/v2/?token=browser-test-token")
        self.page.get_by_role("button", name="我的课程").click()
        self.page.get_by_role("button", name=re.compile("Synthetic A")).click()
        self.page.get_by_role("button", name=re.compile("First lecture")).click()
        self.page.get_by_role("button", name="只取字幕").click()
        self.page.get_by_role("heading", name=re.compile("只取字幕 · First lecture")).wait_for()
        self.page.get_by_text("Synthetic persistent workflow log").wait_for()
        self.assertEqual(len(self.workflow_posts), 1)
        self.assertEqual(self.workflow_posts[0]["body"], {"lecture_id": "lecture-A", "operation": "transcribe"})
        self.assertTrue(self.workflow_posts[0]["key"])
        self.assertIn("Synthetic A", self.page.locator(".task-detail-panel").inner_text())

    def test_double_click_creates_one_job(self):
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/v2/?token=browser-test-token")
        self.page.get_by_role("button", name="我的课程").click()
        self.page.get_by_role("button", name=re.compile("Synthetic A")).click()
        self.page.get_by_role("button", name=re.compile("First lecture")).click()
        self.page.get_by_role("button", name="只取字幕").dblclick()
        self.page.get_by_role("heading", name=re.compile("只取字幕 · First lecture")).wait_for()
        self.assertEqual(len(self.workflow_posts), 1)

    def test_keyboard_navigation_works(self):
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/v2/?token=browser-test-token")
        self.page.keyboard.press("Tab")  # brand link
        self.page.keyboard.press("Tab")  # first primary navigation item
        focused = self.page.evaluate("({text: document.activeElement.innerText, outline: getComputedStyle(document.activeElement).outlineStyle})")
        self.assertIn("我的课程", focused["text"])
        self.assertEqual(focused["outline"], "solid")
        self.page.keyboard.press("Enter")
        self.page.get_by_role("heading", name="我的课程").wait_for()
        self.assertIn("我的课程", self.page.locator('.primary-nav-item[aria-current="page"]').inner_text())

    def test_all_original_commands_accounted_for(self):
        coverage = (Path(__file__).resolve().parents[1] / "FEATURE_COVERAGE.md").read_text(encoding="utf-8")
        expected = {"/setup", "/list", "/trans", "/note", "/add", "/wiki", "/cheatsheet", "/page", "/ralphtrans"}
        self.assertEqual({item["command"] for item in FEATURES}, expected)
        for command in expected:
            with self.subTest(command=command):
                self.assertIn(f"`{command}`", coverage)

    def test_agent_handoff_stays_waiting_until_outputs_are_verified(self):
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/v2/?token=browser-test-token")
        self.page.get_by_role("button", name="我的课程").click()
        self.page.get_by_role("button", name=re.compile("Synthetic A")).click()
        self.page.get_by_role("button", name=re.compile("First lecture")).click()
        self.page.get_by_role("button", name="交给 Agent 写笔记").click()
        self.page.get_by_role("heading", name=re.compile("外部 Agent 写作 · First lecture")).wait_for()
        self.page.get_by_role("heading", name="等待外部 Agent").wait_for()
        self.assertIn("请只为 First lecture", self.page.locator("#agent-instruction").input_value())
        self.page.get_by_role("button", name="检查 Agent 成果").click()
        self.page.get_by_text("Agent 任务仍在等待；指令复制或保存不会被视为完成。").wait_for()
        self.assertIn("等待 Agent", self.page.locator(".task-detail-panel").inner_text())
        self.assertEqual(self.workflow_posts[0]["body"], {"lecture_id": "lecture-A", "operation": "agent_note"})

    def test_old_tasks_show_course_lecture_operation_and_queue(self):
        self.page.route('**/api/lectures?course_id=101', lambda route: route.fulfill(json={
            'ok':True,'title':'示例数学','teacher':'示例教师','lectures':[
                {'sub_id':'202','sub_title':'第一讲','date':'2026-10-01'},
                {'sub_id':'203','sub_title':'第二讲','date':'2026-10-02'}]}))
        self.test_tasks = [
            {'id':'first','label':'字幕转写','kind':'transcribe','status':'running','created_at':1,
             'params':{'course_id':'101','sub_id':'202'},'progress':{'label':'识别','tracks':[]}},
            {'id':'second','label':'课件截图下载','kind':'slide','status':'queued','queue_position':2,'created_at':1,
             'params':{'course_id':'101','sub_id':'203'},'progress':{'label':'等待','tracks':[]}}]
        self.navigate('tasks')
        self.page.locator('#taskList .list-item', has_text='第二讲').wait_for()
        rows = self.page.locator('#taskList .list-item')
        self.assertIn('示例数学', rows.nth(0).inner_text())
        self.assertIn('第一讲', rows.nth(0).inner_text())
        self.assertIn('课件截图下载', rows.nth(1).inner_text())
        self.assertIn('队列第 2 个', rows.nth(1).inner_text())
        rows.nth(1).click()
        self.assertIn('第二讲', self.page.locator('#taskDetailTitle').inner_text())

    def test_task_with_unavailable_names_keeps_clear_id_target(self):
        self.page.route('**/api/lectures?**', lambda route: route.fulfill(status=502,json={'ok':False,'error':'临时不可用'}))
        self.test_tasks = [{'id':'unknown','label':'单节转写','kind':'transcribe','status':'failed','created_at':1,
                            'params':{'course_id':'888','sub_id':'999'},'progress':{'label':'失败','tracks':[]}}]
        self.navigate('tasks')
        self.assertIn('课程 ID 888', self.page.locator('#taskList').inner_text())
        self.assertIn('课次 ID 999', self.page.locator('#taskList').inner_text())

    def test_failed_task_can_retry_original_target_using_cache(self):
        self.test_tasks = [{'id':'failed-task','label':'素材采集','kind':'note','status':'failed','created_at':1,
                            'params':{'course_id':'101','sub_id':'202','force_transcribe':True},
                            'error':'进程退出码 1','progress':{'label':'失败','tracks':[]}}]
        self.navigate('tasks')
        self.page.locator('#taskList .list-item').click()
        self.page.wait_for_selector('#btnRetryTask:visible')
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.page.locator('#btnRetryTask').click()
        self.page.wait_for_timeout(300)
        self.assertEqual(self.task_requests[-1]['kind'], 'note')
        self.assertEqual(self.task_requests[-1]['params']['sub_id'], '202')
        self.assertFalse(self.task_requests[-1]['params']['force_transcribe'])

    def test_running_task_has_no_retry_button(self):
        self.test_tasks = [{'id':'active-task','label':'采集','kind':'note','status':'running','created_at':1,
                            'params':{'course_id':'101','sub_id':'202'},'progress':{'label':'识别','tracks':[]}}]
        self.navigate('tasks')
        self.page.locator('#taskList .list-item').click()
        self.assertTrue(self.page.locator('#btnRetryTask').is_hidden())

    def test_cancelled_batch_can_resume_and_skips_completed_items(self):
        self.test_tasks = [{'id':'batch-task','label':'整门课采集','kind':'batch','status':'cancelled','created_at':1,
                            'params':{'course_id':'101','max_retries':2},'progress':{'label':'已取消','tracks':[]}}]
        self.navigate('tasks')
        self.page.locator('#taskList .list-item').click()
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.page.locator('#btnRetryTask').click()
        self.page.wait_for_timeout(300)
        self.assertTrue(self.task_requests[-1]['params']['retry_failed'])
        self.assertEqual(self.task_requests[-1]['params']['course_id'], '101')
        self.assertEqual(self.task_requests[-1]['params']['max_retries'], 2)

    def test_failed_task_shows_service_rejection_and_materials_entry(self):
        self.test_tasks = [{'id':'rejected-task','label':'采集','kind':'note','status':'failed','created_at':1,
                            'params':{'course_id':'101','sub_id':'202'},'error':'HTTP 412',
                            'progress':{'label':'失败','tracks':[]}}]
        self.navigate('tasks')
        self.page.locator('#taskList .list-item').click()
        self.assertIn('识别服务拒绝请求', self.page.locator('#taskRecoveryHint').inner_text())
        self.page.locator('#btnTaskNext').click()
        self.page.wait_for_selector('.view[data-view="agent"].is-active')
        self.assertEqual(self.page.locator('#agentCourse').input_value(), '101')
        self.assertEqual(self.page.locator('#agentSub').input_value(), '202')

    def test_progress_shows_measured_counts_then_unknown_asr_stage(self):
        self.test_tasks = [{ 'id':'progress-task','label':'课件采集','kind':'slide','status':'running','created_at':1,
                             'progress':{'label':'下载素材','percent':30,'elapsed_seconds':8,'tracks':[{'name':'课件','label':'下载素材','completed':3,'total':10,'failed':1,'unit':'张','percent':30}]} }]
        self.navigate('tasks')
        self.page.locator('#taskList .list-item').click()
        self.assertIn('已处理 3 张 / 10 张', self.page.locator('#taskTracks').inner_text())
        self.assertEqual(self.page.locator('#taskProgressBar').get_attribute('value'), '30')
        if os.environ.get('LOOK_CONSOLE_SCREENSHOTS'):
            self.page.screenshot(path=str(Path(os.environ['LOOK_CONSOLE_SCREENSHOTS'])/'tasks.png'),full_page=True)
        self.test_tasks[0]['progress'].update(label='云端识别字幕', percent=None, tracks=[])
        self.page.wait_for_selector('#taskProgressBar:not([value])')
        self.assertIn('未提供可靠', self.page.locator('#taskProgressHint').inner_text())

    def test_course_sheet_does_not_require_a_lecture(self):
        self.navigate('agent')
        self.page.locator('#btnAgentLoadCourses').click()
        self.page.select_option('#agentCourseChoice','101')
        self.page.locator('input[name=agentKind][value=cheatsheet]').check()
        self.page.select_option('#sheetScope','course')
        self.page.locator('#btnMakeInstruction').click()
        self.page.wait_for_selector('#btnCopy:not([disabled])')
        self.assertIn('整门课程', self.page.locator('#agentTargetContext').inner_text())

    def test_video_download_is_not_presented_as_subtitle_file_size(self):
        self.test_tasks = [{'id':'media-task','kind':'transcribe','label':'字幕转写','status':'running','created_at':1,
                            'progress':{'label':'下载素材','percent':50,'tracks':[{'name':'字幕','track':'transcript','label':'下载素材','completed':1048576,'total':2097152,'unit':'字节','percent':50}]}}]
        self.navigate('tasks')
        self.page.locator('#taskList .list-item').click()
        self.assertIn('下载录课视频', self.page.locator('#taskTracks').inner_text())
        self.assertIn('已下载 1.0 MB / 2.0 MB', self.page.locator('#taskTracks').inner_text())
        self.assertIn('不是字幕文件大小', self.page.locator('#taskProgressHint').inner_text())

    def test_pure_transcript_entry_sends_transcribe_task(self):
        self.navigate('courses')
        self.page.locator('#btnLoadCourses').click()
        self.page.locator('#courseList .list-item').first.click()
        self.page.locator('#lectureList .list-item').first.click()
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.page.locator('summary').filter(has_text='采集高级选项').click()
        self.page.locator('#forceTranscribe').check()
        self.page.locator('#btnTranscriptOnly').click()
        self.page.wait_for_timeout(100)
        self.assertEqual(self.task_requests[-1]['kind'], 'transcribe')
        self.assertTrue(self.task_requests[-1]['params']['force_transcribe'])

    def test_knowledge_explanation_and_recheck_are_actionable(self):
        self.assertNotIn('无法读取', self.page.locator('#banner').inner_text())
        self.page.locator('.knowledge-explainer summary').click()
        self.assertIn('电脑上的课程资料文件夹', self.page.locator('.knowledge-explainer').inner_text())
        self.page.locator('#btnRecheck').click()
        self.page.wait_for_selector('#btnRecheck:not([disabled])')

    def test_refresh_keeps_authenticated_page_and_view(self):
        self.navigate("settings")
        self.page.reload()
        self.page.wait_for_selector('.view[data-view="settings"].is-active')
        self.assertTrue(self.page.locator("#btnSaveCred").is_visible())

    def test_settings_feedback_is_visible(self):
        self.navigate("settings")
        self.page.locator("#btnSaveCred").click()
        self.assertTrue(self.page.locator("#banner").is_visible())

    def test_material_import_sends_selected_lecture(self):
        self.navigate("courses")
        self.page.locator("#btnLoadCourses").click()
        self.page.locator("#courseList .list-item").first.click()
        self.page.locator("#lectureList .list-item").first.click()
        self.page.locator("#materialList").fill("讲义=C:/Materials/example.pdf")
        self.page.locator("#btnImportMaterials").click()
        self.page.wait_for_timeout(100)
        self.assertEqual(self.task_requests[-1]["params"]["sub_id"], "202")

    def test_agent_choice_is_generic_and_persists(self):
        self.navigate("agent")
        self.page.locator("#agentTool").select_option("cursor")
        self.page.reload()
        self.page.wait_for_selector("#agentTool")
        self.assertEqual(self.page.locator("#agentTool").input_value(), "cursor")
        self.assertTrue(self.page.locator("#agentCopyTitle").inner_text().endswith("Cursor"))

    def test_agent_workbench_shows_selected_course_and_lecture(self):
        self.navigate("courses")
        self.page.locator("#btnLoadCourses").click()
        self.page.locator("#courseList .list-item").first.click()
        self.page.locator("#lectureList .list-item").first.click()
        self.page.locator("#btnToAgent").click()
        summary = self.page.locator("#agentTargetContext").inner_text()
        for expected in ("高等数学", "示例教师", "第 1 讲"):
            self.assertIn(expected, summary)
        screenshots = os.environ.get("LOOK_CONSOLE_SCREENSHOTS")
        if screenshots:
            folder = Path(screenshots)
            folder.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(folder / "agent-target.png"), full_page=True)

    def test_agent_can_choose_course_and_lecture_without_entering_ids(self):
        self.navigate("agent")
        self.page.locator("#btnAgentLoadCourses").click()
        self.page.locator("#agentCourseChoice").select_option("101")
        self.page.locator('#agentLectureChoice option[value="202"]').wait_for(state="attached")
        self.page.locator("#agentLectureChoice").select_option("202")
        self.assertEqual(self.page.locator("#agentCourse").input_value(), "101")
        self.assertEqual(self.page.locator("#agentSub").input_value(), "202")
        self.assertIn("第 1 讲", self.page.locator("#agentTargetContext").inner_text())

    def test_manual_course_change_does_not_keep_another_courses_lectures(self):
        self.navigate("courses")
        self.page.locator("#btnLoadCourses").click()
        self.page.locator("#courseList .list-item").first.click()
        self.page.locator("#lectureList .list-item").first.click()
        self.page.locator("#btnToAgent").click()
        self.page.locator("#agentManual summary").click()
        self.page.locator("#agentCourse").fill("303")
        self.page.locator("#agentSub").fill("404")
        self.page.locator("#btnCheckArtifacts").click()
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.locator('#agentLectureChoice option[value="202"]').count(), 0)

    def test_metadata_enrichment_does_not_discard_same_selection_instruction(self):
        self.page.add_init_script("""const originalFetch = window.fetch;
          window.fetch = (...args) => originalFetch(...args).then(res =>
            String(args[0]).includes('/agent/instruction') ? new Promise(resolve => setTimeout(() => resolve(res), 400)) : res);""")
        self.page.reload()
        self.navigate("courses")
        self.page.locator("#btnLoadCourses").click()
        self.page.locator("#courseList .list-item").first.click()
        self.page.locator("#lectureList .list-item").first.click()
        self.page.locator("#btnToAgent").click()
        self.page.locator("#btnMakeInstruction").click()
        self.manifest_course_title = '高等数学（本地素材名称）'
        self.page.locator("#btnCheckArtifacts").click()
        self.page.wait_for_timeout(650)
        self.assertTrue(self.page.locator("#btnCopy").is_enabled())
        self.assertNotEqual(self.page.locator("#agentInstruction").input_value(), '')

    def test_course_search_uses_all_courses_without_checkbox(self):
        self.navigate('courses')
        self.page.locator('#courseAll').uncheck()
        self.page.locator('#courseQuery').fill('示例教师')
        with self.page.expect_request(lambda request: '/api/courses?' in request.url) as request:
            self.page.locator('#btnLoadCourses').click()
        self.assertIn('all=1', request.value.url)

    def test_course_retry_replaces_previous_error_with_success(self):
        self.navigate('courses')
        attempts = [0]
        def courses(route):
            attempts[0] += 1
            if attempts[0] == 1:
                route.fulfill(status=502, content_type='application/json', body=json.dumps({'ok': False, 'error': '获取课程失败', 'detail': '连接超时'}))
            else:
                route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'source': 'all', 'courses': [{'course_id': '101', 'title': '示例课程'}]}))
        self.page.route('**/api/courses?**', courses)
        self.page.locator('#btnLoadCourses').click()
        self.page.wait_for_function("document.getElementById('banner').textContent.includes('获取课程失败')")
        self.assertIn('连接超时', self.page.locator('#banner').inner_text())
        self.page.locator('#btnLoadCourses').click()
        self.page.wait_for_selector('#courseList .list-item')
        self.assertNotIn('获取课程失败', self.page.locator('#banner').inner_text())

    def test_empty_search_is_not_reported_as_missing_configuration(self):
        self.navigate('courses')
        self.page.locator('#courseQuery').fill('没有匹配的教师')
        self.page.route('**/api/courses?**', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'courses': []})))
        self.page.locator('#btnLoadCourses').click()
        self.page.locator("#courseMsg").filter(has_text="未找到匹配").wait_for()
        self.assertNotIn('请先配置账号', self.page.locator('#courseList').inner_text())
        self.assertNotIn('err', self.page.locator('#banner').get_attribute('class'))

    def assert_latest_course_search_wins(self, old_failure):
        self.page.add_init_script("""const originalFetch = window.fetch;
          window.fetch = (...args) => originalFetch(...args).then(res =>
            String(args[0]).includes('query=older') ? new Promise(resolve => setTimeout(() => resolve(res), 450)) : res);""")
        self.page.reload()
        self.navigate('courses')
        def courses(route):
            old = 'query=older' in route.request.url
            if old and old_failure:
                route.fulfill(status=502, content_type='application/json', body=json.dumps({'ok': False, 'error': '旧请求失败'}))
            else:
                route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'courses': [{'course_id': '101' if old else '202', 'title': '旧结果' if old else '新结果'}]}))
        self.page.route('**/api/courses?**', courses)
        self.page.locator('#courseQuery').fill('older')
        self.page.locator('#courseQuery').press('Enter')
        self.page.locator('#courseQuery').fill('newer')
        self.page.locator('#courseQuery').press('Enter')
        self.page.wait_for_function("document.getElementById('courseList').textContent.includes('新结果')")
        self.page.wait_for_timeout(650)
        self.assertIn('新结果', self.page.locator('#courseList').inner_text())
        self.assertNotIn('旧', self.page.locator('#banner').inner_text())
        self.assertNotIn('旧结果', self.page.locator('#courseList').inner_text())

    def test_old_failed_search_does_not_replace_new_success(self):
        self.assert_latest_course_search_wins(True)

    def test_old_successful_search_does_not_replace_new_results(self):
        self.assert_latest_course_search_wins(False)

    def test_note_without_optional_timeline_can_verify(self):
        self.notes_ready = True
        self.navigate("agent")
        self.page.locator("#agentManual summary").click()
        self.page.locator("#agentCourse").fill("101")
        self.page.locator("#agentSub").fill("202")
        self.page.locator("#optTimeline").uncheck()
        self.page.locator("#btnVerify").click()
        self.page.wait_for_timeout(100)
        self.assertNotIn("尚未检测到", self.page.locator("#verifyResult").inner_text())

    def test_layout_at_supported_sizes(self):
        for width in (320, 768, 1024, 1440):
            self.page.set_viewport_size({"width": width, "height": 960})
            for view in ("home", "settings", "courses", "tasks", "agent", "wiki"):
                with self.subTest(width=width, view=view):
                    self.navigate(view)
                    dimensions = self.page.evaluate("({page: document.documentElement.scrollWidth, viewport: innerWidth})")
                    self.assertLessEqual(dimensions["page"], dimensions["viewport"])
                    main = self.page.locator("main").bounding_box()
                    self.assertGreater(main["width"], min(280, width - 24))
        screenshots = os.environ.get("LOOK_CONSOLE_SCREENSHOTS")
        if screenshots:
            folder = Path(screenshots)
            folder.mkdir(parents=True, exist_ok=True)
            self.page.set_viewport_size({"width": 1440, "height": 960})
            for view in ("home", "agent", "settings", "wiki"):
                self.navigate(view)
                self.page.screenshot(path=str(folder / f"{view}.png"), full_page=True)

    def test_fast_switch_to_finished_task_still_loads_its_log(self):
        self.test_tasks = [{"id": name, "label": name, "kind": "index", "created_at": 0, "status": "succeeded"} for name in ("taskA", "taskB")]
        self.page.add_init_script("""const originalFetch = window.fetch;
          window.fetch = (...args) => originalFetch(...args).then(res =>
            String(args[0]).includes('/taskA/log') ? new Promise(resolve => setTimeout(() => resolve(res), 400)) : res);""")
        self.page.reload()
        self.navigate("tasks")
        self.page.locator('[data-task="taskA"]').click()
        self.page.locator('[data-task="taskB"]').click()
        self.page.wait_for_timeout(650)
        self.assertIn("log-taskB", self.page.locator("#taskLog").inner_text())
        self.assertNotIn("log-taskA", self.page.locator("#taskLog").inner_text())

    def test_changed_agent_inputs_do_not_receive_old_instruction(self):
        self.page.add_init_script("""const originalFetch = window.fetch;
          window.fetch = (...args) => originalFetch(...args).then(res =>
            String(args[0]).includes('/agent/instruction') ? new Promise(resolve => setTimeout(() => resolve(res), 400)) : res);""")
        self.page.reload()
        self.navigate("agent")
        self.page.locator("#agentManual summary").click()
        self.page.locator("#agentCourse").fill("101")
        self.page.locator("#agentSub").fill("202")
        self.page.locator("#btnMakeInstruction").click()
        self.page.locator("#agentSub").fill("303")
        self.page.wait_for_timeout(650)
        self.assertEqual(self.page.locator("#agentInstruction").input_value(), "")


if __name__ == "__main__":
    unittest.main()
