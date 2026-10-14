from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock

from console.asr_providers import (
    ASRCancelled,
    BcutASRProvider,
    ProviderUnavailable,
    provider_catalog,
    resolve_provider,
)


class FakeProvider:
    provider_id = "fake"

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = []

    def transcribe(self, audio, progress, cancel):
        self.calls.append((audio, progress, cancel))
        progress({"stage": "recognizing"})
        if cancel():
            raise ASRCancelled("识别已取消")
        if self.error:
            raise self.error
        return "recognized", "", []


class FakeBcutClient:
    def __init__(self) -> None:
        self.calls = []

    def transcribe_file(self, audio_path, *, progress, cancel):
        self.calls.append((audio_path, progress, cancel))
        return "recognized", "", []


class ASRProviderTests(unittest.TestCase):
    def test_bcut_adapter_passes_progress_and_cancel_to_existing_client(self):
        client = FakeBcutClient()
        adapter = BcutASRProvider(client)
        cancel = lambda: False
        progress = lambda _: None
        adapter.transcribe("cached.mp3", progress, cancel)
        self.assertIs(client.calls[0][1], progress)
        self.assertIs(client.calls[0][2], cancel)

    def test_provider_switch_keeps_audio(self):
        provider = FakeProvider()
        reports = []
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "cached.mp3"
            audio.write_bytes(b"cached audio")
            self.assertEqual(provider.transcribe(audio, reports.append, lambda: False)[0], "recognized")
            self.assertTrue(audio.is_file())
            self.assertEqual(audio.read_bytes(), b"cached audio")
        self.assertEqual(reports, [{"stage": "recognizing"}])

    def test_unconfigured_provider_is_blocked(self):
        with self.assertRaises(ProviderUnavailable):
            resolve_provider("local_whisper")
        catalog = {item["id"]: item for item in provider_catalog()}
        self.assertEqual(catalog["local_whisper"]["availability"], "unavailable")

    def test_cancel_reaches_provider(self):
        provider = FakeProvider()
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(ASRCancelled):
            provider.transcribe("cached.mp3", lambda _: None, cancelled.is_set)
        self.assertEqual(provider.calls[0][0], "cached.mp3")

    def test_bcut_client_checks_cancel_before_upload(self):
        from scripts.tongji_backend.transcriber import BcutASRClient

        client = BcutASRClient()
        client.session = Mock()
        with self.assertRaises(ASRCancelled):
            client.transcribe_file("unused.mp3", cancel=lambda: True)
        client.session.post.assert_not_called()

    def test_provider_error_does_not_delete_cache(self):
        provider = FakeProvider(error=RuntimeError("provider failed"))
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "cached.mp3"
            audio.write_bytes(b"keep me")
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                provider.transcribe(audio, lambda _: None, lambda: False)
            self.assertEqual(audio.read_bytes(), b"keep me")


if __name__ == "__main__":
    unittest.main()
