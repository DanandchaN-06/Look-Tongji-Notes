from __future__ import annotations

import threading
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import look_tongji as cli
from console.runner import build_argv
from scripts.tongji_backend.workspace import WorkspaceConfig, build_workspace_wiki, write_manifest


class CliSideEffectTests(unittest.TestCase):
    def test_deferred_build_preserves_cli_default(self):
        parser = cli.build_parser()
        default_args = parser.parse_args(["note", "--course-id", "101", "--sub-id", "202"])
        deferred_args = parser.parse_args([
            "note", "--course-id", "101", "--sub-id", "202", "--defer-site-build",
        ])
        self.assertFalse(default_args.defer_site_build)
        self.assertTrue(deferred_args.defer_site_build)
        self.assertNotIn("--defer-site-build", build_argv("transcribe", {"course_id": "101", "sub_id": "202"}))
        self.assertIn("--defer-site-build", build_argv("transcribe", {
            "course_id": "101", "sub_id": "202", "defer_site_build": True,
        }))

        config = SimpleNamespace(workspace_root=Path("synthetic-workspace"))
        with patch.object(cli, "build_workspace_wiki", return_value=Path("synthetic-site")) as build:
            self.assertEqual(cli._build_workspace_if_requested(config, defer=False), 0)
            build.assert_called_once_with(config)
        with patch.object(cli, "build_workspace_wiki") as build:
            self.assertEqual(cli._build_workspace_if_requested(config, defer=True), 0)
            build.assert_not_called()

    def test_workspace_builds_are_serialized_across_threads(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            config = WorkspaceConfig(root, "owner", "site", root / "workspace.json")
            lock = threading.Lock()
            active = 0
            maximum = 0

            def build(_root, *_args):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                out_index = _args.index("--out") + 1
                out_dir = Path(_args[out_index])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "index.html").write_text("synthetic site", encoding="utf-8")
                with lock:
                    active -= 1

            with patch("scripts.tongji_backend.workspace.ensure_workspace_scaffold"), \
                 patch("scripts.tongji_backend.workspace.sync_all_manifests_to_llmwiki", return_value=[]), \
                 patch("scripts.tongji_backend.workspace._run_llmwiki", side_effect=build):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(lambda _: build_workspace_wiki(config), range(2)))

            self.assertEqual(maximum, 1)

    def test_failed_workspace_build_keeps_published_site(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            config = WorkspaceConfig(root, "owner", "site", root / "workspace.json")
            site = root / "site"
            site.mkdir()
            (site / "index.html").write_text("previous published page", encoding="utf-8")
            with patch("scripts.tongji_backend.workspace.ensure_workspace_scaffold"), \
                 patch("scripts.tongji_backend.workspace.index_workspace_wiki", return_value=[]), \
                 patch("scripts.tongji_backend.workspace._run_llmwiki", side_effect=RuntimeError("synthetic failure")):
                with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                    build_workspace_wiki(config)
            self.assertEqual((site / "index.html").read_text(encoding="utf-8"), "previous published page")
            self.assertFalse(list(root.glob(".site-build-*")))

    def test_deferred_manifest_write_does_not_touch_shared_index(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            manifest_path = root / "manifest.json"
            workspace = SimpleNamespace(manifest_path=manifest_path)
            config = WorkspaceConfig(root, "owner", "site", root / "workspace.json")
            with patch("scripts.tongji_backend.workspace.load_workspace_config", return_value=config), \
                 patch("scripts.tongji_backend.workspace.sync_manifest_to_llmwiki_source") as sync:
                write_manifest(workspace, {"sub_id": "202"}, defer_site_build=True)
                sync.assert_not_called()
                write_manifest(workspace, {"sub_id": "202"})
                sync.assert_called_once_with(config, manifest_path)
            self.assertEqual(manifest_path.read_text(encoding="utf-8"), '{\n  "sub_id": "202"\n}\n')

    def test_slide_failure_keeps_successful_transcript_stage(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = SimpleNamespace(
                raw_root=root / "raw", raw_slides_dir=root / "raw" / "slides",
            )
            workspace.raw_root.mkdir(parents=True)
            workspace.raw_slides_dir.mkdir()
            config = WorkspaceConfig(root, "owner", "site", root / "workspace.json")
            client = SimpleNamespace(auth=SimpleNamespace(get_jwt_token=lambda: ""))
            args = cli.build_parser().parse_args([
                "note", "--course-id", "101", "--sub-id", "202", "--no-material-prompt", "--defer-site-build",
            ])
            with patch.object(cli, "_ensure_authenticated_client", return_value=(client, "QA")), \
                 patch.object(cli, "_resolve_course_sub", return_value=("101", "202")), \
                 patch.object(cli, "_prepare_workspace_for_lecture", return_value=(config, workspace)), \
                 patch.object(cli, "_collect_materials", return_value=[]), \
                 patch.object(cli, "_run_transcript_job", return_value=0), \
                 patch.object(cli, "_run_slide_job", return_value=3), \
                 patch.object(cli, "_write_lecture_manifest_from_outputs") as write_manifest, \
                 patch.object(cli, "_build_workspace_if_requested", return_value=0):
                self.assertEqual(cli.cmd_note(args), 0)
            write_manifest.assert_called_once()


if __name__ == "__main__":
    unittest.main()
