from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from console.note_service import NoteGenerationError, NoteService, _call_text_model


class NoteServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.raw = Path(self.temp.name)
        self.course_id = "course-synthetic"
        self.sub_id = "lecture-synthetic"
        self.base = f"{self.course_id}_{self.sub_id}"
        self.transcript = self.raw / f"{self.base}.srt"
        self.transcript.write_text(
            "1\n00:00:00,000 --> 00:00:20,000\n第一段字幕，说明线性代数的向量空间与基。\n\n"
            "2\n00:00:20,000 --> 00:00:40,000\n第二段字幕，说明线性变换的定义和矩阵表示。\n",
            encoding="utf-8",
        )
        self.manifest = self.raw / "manifest.json"
        self.manifest.write_text(json.dumps({
            "course_id": self.course_id,
            "sub_id": self.sub_id,
            "base_name": self.base,
            "artifacts": {"subtitle_srt": str(self.transcript), "transcript_meta": str(self.raw / f"{self.base}.json")},
        }), encoding="utf-8")
        (self.raw / f"{self.base}.json").write_text(json.dumps({
            "complete": True,
            "course_id": self.course_id,
            "sub_id": self.sub_id,
            "artifact_sha256": {"subtitle_srt": hashlib.sha256(self.transcript.read_bytes()).hexdigest()},
        }), encoding="utf-8")
        self.calls: list[dict] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def model(self, payload, _config):
        self.calls.append(payload)
        user = payload["messages"][1]["content"]
        start = 0 if "00:00:00,000" in user else 20_000
        end = start + 20_000
        return {
            "content": json.dumps({
                "notes_markdown": f"## 片段 {start // 1000}\n基于字幕整理：定义与推导。",
                "timeline": [{"start_ms": start, "end_ms": end, "topic": "定义与推导"}],
            }, ensure_ascii=False),
            "finish_reason": "stop",
        }

    def service(self, model=None):
        return NoteService(
            model_config={"provider": "openai-compatible", "model": "synthetic", "base_url": "https://model.example.invalid/v1", "api_key": "synthetic-secret"},
            model_call=model or self.model,
            max_segment_chars=70,
        )

    def test_truncated_model_output_is_failure(self):
        def truncated(_payload, _config):
            return {"content": '{"notes_markdown":"半截', "finish_reason": "length"}

        with self.assertRaisesRegex(NoteGenerationError, "截断"):
            self.service(truncated).generate(
                self.raw, self.manifest, course_id=self.course_id, sub_id=self.sub_id,
                note_style="standard", include_timeline=True,
            )
        self.assertFalse((self.raw / f"{self.base}_notes.md").exists())

    def test_segment_restart_reuses_valid_parts(self):
        calls_before_failure = []

        def fails_second(payload, config):
            calls_before_failure.append(payload)
            if len(calls_before_failure) == 2:
                raise RuntimeError("synthetic transport failure")
            return self.model(payload, config)

        with self.assertRaises(NoteGenerationError):
            self.service(fails_second).generate(
                self.raw, self.manifest, course_id=self.course_id, sub_id=self.sub_id,
                note_style="standard", include_timeline=True,
            )
        first_checkpoint = json.loads(next(self.raw.glob(".*checkpoint.json")).read_text(encoding="utf-8"))
        self.assertEqual(len(first_checkpoint["segments"]), 1)

        calls_before_restart = len(self.calls)
        self.service().generate(
            self.raw, self.manifest, course_id=self.course_id, sub_id=self.sub_id,
            note_style="standard", include_timeline=True,
        )
        self.assertEqual(len(self.calls) - calls_before_restart, 1)
        self.assertTrue((self.raw / f"{self.base}_notes.md").is_file())
        self.assertTrue((self.raw / f"{self.base}_timeline.txt").is_file())

    def test_timeline_out_of_bounds_rejected(self):
        def out_of_bounds(_payload, _config):
            return {"content": json.dumps({
                "notes_markdown": "## 笔记\n依据字幕整理。",
                "timeline": [{"start_ms": 0, "end_ms": 90_000, "topic": "越界"}],
            }), "finish_reason": "stop"}

        with self.assertRaisesRegex(NoteGenerationError, "时间轴"):
            self.service(out_of_bounds).generate(
                self.raw, self.manifest, course_id=self.course_id, sub_id=self.sub_id,
                note_style="standard", include_timeline=True,
            )

    def test_existing_notes_not_overwritten_without_regenerate(self):
        note = self.raw / f"{self.base}_notes.md"
        note.write_text("已有人工复核笔记", encoding="utf-8")
        with self.assertRaisesRegex(NoteGenerationError, "已存在"):
            self.service().generate(
                self.raw, self.manifest, course_id=self.course_id, sub_id=self.sub_id,
                note_style="standard", include_timeline=True,
            )
        self.assertEqual(note.read_text(encoding="utf-8"), "已有人工复核笔记")
        self.assertEqual(self.calls, [])

    def test_model_key_never_appears_in_errors_or_checkpoint(self):
        def failing(_payload, _config):
            raise RuntimeError("request rejected")

        with self.assertRaises(NoteGenerationError) as caught:
            self.service(failing).generate(
                self.raw, self.manifest, course_id=self.course_id, sub_id=self.sub_id,
                note_style="standard", include_timeline=True,
            )
        self.assertNotIn("synthetic-secret", str(caught.exception))
        checkpoint = next(self.raw.glob(".*checkpoint.json"))
        self.assertNotIn("synthetic-secret", checkpoint.read_text(encoding="utf-8"))

    def test_incomplete_or_wrong_identity_transcript_never_reaches_model(self):
        meta = self.raw / f"{self.base}.json"
        meta.write_text(json.dumps({"complete": True, "course_id": self.course_id, "sub_id": "other"}), encoding="utf-8")
        with self.assertRaisesRegex(NoteGenerationError, "完整性校验"):
            self.service().generate(
                self.raw, self.manifest, course_id=self.course_id, sub_id=self.sub_id,
                note_style="standard", include_timeline=True,
            )
        self.assertEqual(self.calls, [])

    def test_missing_model_configuration_is_reported_before_generation(self):
        with self.assertRaisesRegex(NoteGenerationError, "先完成字幕和课件采集"):
            NoteService(model_config={}, model_call=self.model).generate(
                self.raw, self.manifest, course_id=self.course_id, sub_id=self.sub_id,
                note_style="standard", include_timeline=True,
            )
        self.assertEqual(self.calls, [])

    def test_provider_requests_use_documented_non_streaming_chat_shapes(self):
        class Response(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        openai_response = Response(json.dumps({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}).encode())
        with patch("console.note_service.build_opener") as build_opener:
            build_opener.return_value.open.return_value = openai_response
            result = _call_text_model({"messages": [{"role": "user", "content": "hello"}]}, {
                "provider": "openai-compatible", "model": "synthetic", "baseUrl": "https://model.example.invalid/v1", "apiKey": "synthetic-secret",
            })
            request = build_opener.return_value.open.call_args.args[0]
        self.assertEqual(result["finish_reason"], "length")
        self.assertTrue(request.full_url.endswith("/v1/chat/completions"))
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-secret")
        self.assertEqual(json.loads(request.data)["response_format"], {"type": "json_object"})

        ollama_response = Response(json.dumps({"message": {"content": "{}"}, "done_reason": "stop"}).encode())
        with patch("console.note_service.build_opener") as build_opener:
            build_opener.return_value.open.return_value = ollama_response
            result = _call_text_model({"messages": [{"role": "user", "content": "hello"}]}, {
                "provider": "ollama", "model": "synthetic", "baseUrl": "http://localhost:11434/v1",
            })
            request = build_opener.return_value.open.call_args.args[0]
        self.assertEqual(result["finish_reason"], "stop")
        self.assertTrue(request.full_url.endswith("/api/chat"))
        self.assertFalse(json.loads(request.data)["stream"])


if __name__ == "__main__":
    unittest.main()
