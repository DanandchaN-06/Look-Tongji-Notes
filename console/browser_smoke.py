"""Isolated browser checks with synthetic courses; no account or model calls.

Run: python -m unittest console.browser_smoke -v
Screenshots: set LOOK_CONSOLE_SCREENSHOTS to a local directory.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright
from console.server import make_server


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
        self.page.route("**/api/**", self.route)
        self.page.goto(self.url)
        self.page.wait_for_selector("#statDeps.ok")

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def route(self, route):
        request = route.request
        path = request.url.split("/api/")[1]
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
