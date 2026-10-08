"""Offline checks for CLI -> console material discovery."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from console import agent_bridge
from scripts import look_tongji as cli


class SlideDiscovery(unittest.TestCase):
    def run_slide(self, root, existing=None):
        raw = root / "raw" / "Demo" / "2026-10-08-session" / "原始数据"
        slides = raw / "slides"
        slides.mkdir(parents=True)
        manifest = raw / "manifest.json"
        if existing:
            manifest.write_text(json.dumps(existing), encoding="utf-8")
        workspace = SimpleNamespace(raw_root=raw, raw_slides_dir=slides, manifest_path=manifest,
                                    course_title="高等数学", session_title="第 3 讲：导数")
        args = SimpleNamespace(force_login=False, lecture_url="", course_id="101", sub_id="202", lecture_limit=20,
                               output_dir="", per_page=5, max_pages=1, max_items=3, concurrency=1, retries=1, timeout=8)
        def download(**kwargs):
            (slides / "01.jpg").write_bytes(b"synthetic fixture")
            (slides / "index.json").write_text("{}", encoding="utf-8")
            return 0
        with patch.object(cli, "_ensure_authenticated_client", return_value=(None, "QA")), patch.object(cli, "_resolve_course_sub", return_value=("101", "202")), patch.object(cli, "_prepare_workspace_for_lecture", return_value=(None, workspace)), patch.object(cli, "_run_slide_job", side_effect=download), patch("tongji_backend.workspace.load_workspace_config", return_value=None):
            self.assertEqual(cli.cmd_slide(args), 0)
        return raw

    def test_slide_only_download_can_be_found_by_agent_workbench(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = self.run_slide(root)
            self.assertTrue((raw / "manifest.json").is_file())
            with patch.object(agent_bridge, "load_workspace_config", return_value=SimpleNamespace(workspace_root=root)):
                artifacts = agent_bridge.detect_artifacts("101", "202")
                self.assertEqual(artifacts["slide_count"], 1)
                self.assertEqual(artifacts["course_title"], "高等数学")
                self.assertEqual(artifacts["session_title"], "第 3 讲：导数")

    def test_slide_update_preserves_existing_transcript_and_note_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            raw = self.run_slide(Path(folder), {"note_style": "dialogue", "artifacts": {"transcript_txt": "custom/transcript.txt"}, "duration_seconds": 3600})
            manifest = json.loads((raw / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["artifacts"]["transcript_txt"], "custom/transcript.txt")
            self.assertEqual(manifest["note_style"], "dialogue")
            self.assertEqual(manifest["duration_seconds"], 3600)


if __name__ == "__main__":
    unittest.main()
