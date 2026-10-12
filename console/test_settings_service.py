"""Settings persistence and credential secrecy for the V2 settings page."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from console import config_store
from console.course_service import CourseService
from console.routes_v2 import V2Router
from console.settings_service import SettingsService
from console.storage import ConsoleStorage


class SettingsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {
            "LOOK_TONGJI_CONSOLE_CONFIG_DIR": str(self.root / "user-config"),
            "LOOK_TONGJI_CONFIG_PATH": str(self.root / "workspace.json"),
        }))
        self.stack.enter_context(patch.object(config_store, "ENV_PATH", self.root / "project.env"))
        self.stack.enter_context(patch.object(
            config_store, "VISION_CONFIG_PATH", self.root / "vision" / "config.json"
        ))
        self.stack.enter_context(patch.object(config_store, "VISION_DIR", self.root / "vision"))
        self.db_path = self.root / "console.sqlite3"
        self.storage = ConsoleStorage(self.db_path)
        self.service = SettingsService(self.storage)

    def tearDown(self) -> None:
        self.storage.close_thread_connection()
        self.stack.close()
        self.temp_dir.cleanup()

    def test_asr_catalog_does_not_claim_unverified_local_provider(self):
        asr = self.service.get()["asr"]
        providers = {item["id"]: item for item in asr["providers"]}
        self.assertEqual(asr["provider"], "bcutasr")
        self.assertEqual(asr["availability"], "unknown")
        self.assertEqual(providers["bcutasr"]["availability"], "unknown")
        self.assertEqual(providers["local_whisper"]["availability"], "unavailable")

    def test_settings_merge_keeps_unrelated_fields(self):
        settings_path = config_store.console_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"unrelated": {"preserve": True}, "note": {"style": "dialogue"}}),
            encoding="utf-8",
        )

        self.service.save({"tasks": {"course_parallelism": 3}})

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["unrelated"], {"preserve": True})
        self.assertEqual(saved["note"]["style"], "dialogue")
        self.assertEqual(saved["tasks"]["course_parallelism"], 3)

    def test_public_settings_hide_secrets(self):
        self.service.save({
            "account": {"username": "synthetic-user", "password": "synthetic-password"},
            "text_model": {
                "provider": "openai-compatible",
                "model": "synthetic-model",
                "base_url": "https://example.invalid/v1",
                "api_key": "synthetic-text-key",
            },
        })
        public = json.dumps(self.service.get(), ensure_ascii=False)
        for secret in ("synthetic-password", "synthetic-text-key"):
            self.assertNotIn(secret, public)
        self.assertIn('"password_set": true', public)
        self.assertIn('"configured": true', public)

    def test_unknown_private_fields_are_not_returned_or_snapshotted(self):
        settings_path = config_store.console_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({
                "note": {"style": "dialogue", "api_key": "private-extra-secret"},
                "tasks": {"password": "private-extra-password"},
            }),
            encoding="utf-8",
        )

        public = json.dumps(self.service.get(), ensure_ascii=False)
        self.assertIn('"style": "dialogue"', public)
        self.assertNotIn("private-extra-secret", public)
        self.assertNotIn("private-extra-password", public)

        saved = self.service.save({"tasks": {"course_parallelism": 3}})
        row = self.storage.connection.execute(
            "SELECT public_json FROM settings_revisions WHERE id = ?",
            (saved["revision_id"],),
        ).fetchone()
        self.assertNotIn("private-extra-secret", row[0])
        self.assertNotIn("private-extra-password", row[0])

    def test_malformed_existing_preferences_are_preserved_on_save(self):
        settings_path = config_store.console_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        original = b'{"note":'
        settings_path.write_bytes(original)

        with self.assertRaisesRegex(ValueError, "本地设置文件"):
            self.service.save({
                "text_model": {
                    "provider": "openai-compatible",
                    "model": "synthetic-model",
                    "base_url": "https://example.invalid/v1",
                    "api_key": "synthetic-key",
                },
                "tasks": {"course_parallelism": 3},
            })

        self.assertEqual(settings_path.read_bytes(), original)
        self.assertFalse(config_store.text_model_config_path().exists())

    def test_settings_persist_across_service_restart(self):
        self.service.save({
            "note": {"route": "model", "style": "dialogue", "include_timeline": False},
            "tasks": {"course_parallelism": 3},
            "text_model": {
                "provider": "openai-compatible",
                "model": "synthetic-model",
                "base_url": "https://example.invalid/v1",
                "api_key": "synthetic-key",
            },
        })
        restarted = SettingsService(ConsoleStorage(self.db_path))
        public = restarted.get()
        self.assertEqual(public["note"]["route"], "model")
        self.assertEqual(public["note"]["style"], "dialogue")
        self.assertFalse(public["note"]["include_timeline"])
        self.assertEqual(public["tasks"]["course_parallelism"], 3)
        restarted.storage.close_thread_connection()

    def test_revision_snapshot_excludes_secrets(self):
        saved = self.service.save({
            "account": {"password": "revision-secret"},
            "text_model": {
                "provider": "openai-compatible",
                "model": "synthetic-model",
                "base_url": "https://example.invalid/v1",
                "api_key": "revision-api-key",
            },
        })
        row = self.storage.connection.execute(
            "SELECT public_json FROM settings_revisions WHERE id = ?",
            (saved["revision_id"],),
        ).fetchone()
        self.assertNotIn("revision-secret", row[0])
        self.assertNotIn("revision-api-key", row[0])
        self.assertNotIn("api_key", row[0])

    def test_new_settings_revision_does_not_rewrite_old_workflow(self):
        first = self.service.save({"note": {"route": "external_agent"}})
        account = self.storage.upsert_account(
            platform_account_id="synthetic-settings-user", provider="tongji"
        )
        course = self.storage.upsert_course(
            account_id=account.id, platform_course_id="course-1", title="Synthetic course"
        )
        lecture = self.storage.upsert_lecture(
            course_id=course.id, platform_sub_id="lecture-1", title="Synthetic lecture"
        )
        workflow = self.storage.create_workflow(
            lecture_id=lecture.id,
            operation="organize",
            settings_revision_id=first["revision_id"],
            target={"course_title": course.title, "session_title": lecture.title},
        )

        second = self.service.save({
            "note": {"route": "model"},
            "text_model": {
                "provider": "openai-compatible",
                "model": "synthetic-model",
                "base_url": "https://example.invalid/v1",
                "api_key": "synthetic-key",
            },
        })

        self.assertNotEqual(first["revision_id"], second["revision_id"])
        self.assertEqual(self.storage.get_workflow(workflow.id).settings_revision_id, first["revision_id"])

    def test_invalid_preferences_are_rejected_without_partial_save(self):
        before = self.service.get()
        with self.assertRaises(ValueError):
            self.service.save({"tasks": {"course_parallelism": 9}})
        self.assertEqual(self.service.get()["tasks"], before["tasks"])

    def test_invalid_first_workspace_does_not_write_account_credentials(self):
        with patch.object(config_store, "write_env") as write_env:
            with self.assertRaisesRegex(ValueError, "必须填写 workspace_root"):
                self.service.save({"account": {"username": "synthetic-user", "owner_name": "Owner"}})
        write_env.assert_not_called()

    def test_remote_model_endpoints_require_https(self):
        with self.assertRaisesRegex(ValueError, "必须使用 HTTPS"):
            self.service.save({
                "text_model": {
                    "provider": "openai-compatible",
                    "model": "synthetic-model",
                    "base_url": "http://model.example.invalid/v1",
                }
            })
        self.assertFalse(self.service.get()["text_model"]["configured"])

    def test_model_note_route_requires_a_configured_text_model(self):
        with self.assertRaisesRegex(ValueError, "请先配置"):
            self.service.save({"note": {"route": "model"}})
        self.service.save({
            "note": {"route": "model"},
            "text_model": {
                "provider": "openai-compatible",
                "model": "synthetic-model",
                "base_url": "https://example.invalid/v1",
                "api_key": "synthetic-key",
            },
        })
        self.assertTrue(self.service.get()["text_model"]["configured"])

    def test_text_model_test_uses_metadata_endpoint_and_keeps_key_server_side(self):
        self.service.save({
            "text_model": {
                "provider": "openai-compatible",
                "model": "synthetic-model",
                "base_url": "http://127.0.0.1:9081/v1",
                "api_key": "synthetic-server-only-key",
            }
        })
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"data": []}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch("console.settings_service.build_opener", return_value=opener):
            result = self.service.test_text_model()

        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:9081/v1/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-server-only-key")
        self.assertTrue(result["success"])
        self.assertNotIn("synthetic-server-only-key", json.dumps(result))

    def test_ollama_text_model_test_uses_local_tags_endpoint_without_key(self):
        self.service.save({
            "text_model": {
                "provider": "ollama",
                "model": "llama-local",
                "base_url": "http://localhost:11434/v1",
            }
        })
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"models": []}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch("console.settings_service.build_opener", return_value=opener):
            result = self.service.test_text_model()

        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/api/tags")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertTrue(result["success"])

    def test_v2_settings_routes_use_standard_envelope(self):
        courses = CourseService(
            self.storage,
            account_identity=lambda: None,
            workspace_root=lambda: None,
        )
        router = V2Router(courses, self.service)
        status, payload = router.get("/api/v2/settings", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["note"]["route"], "external_agent")

        status, saved = router.post("/api/v2/settings", {"note": {"style": "dialogue"}})
        self.assertEqual(status, 200)
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["data"]["settings"]["note"]["style"], "dialogue")

        with patch.object(self.service, "test_text_model", return_value={"success": True, "message": "synthetic success"}):
            status, result = router.post("/api/v2/settings/test/text_model", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["success"])


if __name__ == "__main__":
    unittest.main()
