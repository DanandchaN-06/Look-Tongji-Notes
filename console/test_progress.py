import json
import unittest
from console import progress
from console.runner import Task


class ProgressTests(unittest.TestCase):
    def event(self, **fields):
        return progress.PREFIX + json.dumps(fields)

    def test_measured_counts_report_real_percentage(self):
        task = Task("t", "slide", {}, [])
        task.status = "running"
        task.consume_progress(self.event(track="slides", stage="download", completed=3, total=10, failed=1, unit="张"))
        result = task.snapshot()["progress"]
        self.assertEqual(result["percent"], 30)
        self.assertEqual(result["tracks"][0]["failed"], 1)

    def test_cloud_recognition_does_not_invent_percentage(self):
        task = Task("t", "transcribe", {}, [])
        task.status = "running"
        task.consume_progress(self.event(track="transcript", stage="recognize"))
        result = task.snapshot()["progress"]
        self.assertIsNone(result["percent"])
        self.assertEqual(result["label"], "云端识别字幕")

    def test_parallel_note_keeps_both_tracks_without_fake_overall_percentage(self):
        task = Task("t", "note", {}, [])
        task.status = "running"
        task.consume_progress(self.event(track="slides", stage="download", completed=2, total=4, unit="张"))
        task.consume_progress(self.event(track="transcript", stage="extract"))
        result = task.snapshot()["progress"]
        self.assertIsNone(result["percent"])
        self.assertEqual(len(result["tracks"]), 2)

    def test_invalid_or_sensitive_event_fields_do_not_reach_api(self):
        event = progress.parse_event(self.event(track="slides", stage="download", completed=-1, total=4, secret="test-secret"))
        self.assertIsNone(event["percent"])
        self.assertNotIn("secret", event)
        self.assertIsNone(progress.parse_event("LOOK_PROGRESS broken"))

    def test_completed_task_has_a_terminal_progress_state(self):
        task = Task("t", "build", {}, [])
        task.status = "succeeded"
        self.assertEqual(task.snapshot()["progress"]["percent"], 100)

    def test_video_download_bytes_are_labeled_as_source_media(self):
        event = progress.parse_event(self.event(track="transcript", stage="download_media", completed=1024, total=2048, unit="字节"))
        self.assertIsNotNone(event)
        self.assertIn('录课视频', event['label'])
        self.assertEqual(event['percent'], 50)
        old = progress.parse_event(self.event(track="transcript", stage="download", completed=1024,total=2048,unit="字节"))
        self.assertIn('录课视频', old['label'])


if __name__ == "__main__":
    unittest.main()
