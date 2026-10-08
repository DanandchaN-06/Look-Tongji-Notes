import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from console import agent_bridge
from tongji_backend.transcriber import Transcriber
from tongji_backend import media_tools


class MediaToolTests(unittest.TestCase):
    def test_empty_alias_is_skipped_for_a_working_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            bad=Path(folder)/'empty.exe'
            good=Path(folder)/'ffmpeg.exe'
            bad.write_bytes(b'')
            good.write_bytes(b'MZsynthetic executable')
            with patch.object(media_tools,'candidates',return_value=[bad,good]),patch.object(media_tools.subprocess,'run',return_value=Mock(returncode=0,stdout='ffmpeg version synthetic',stderr='')) as run:
                self.assertEqual(media_tools.resolve_ffmpeg(),str(good))
                self.assertEqual(run.call_count,1)

    def test_unusable_ffmpeg_stops_before_downloading(self):
        worker=Transcriber()
        worker._asr=Mock()
        with patch('tongji_backend.transcriber.resolve_ffmpeg',side_effect=media_tools.MediaToolError('invalid executable')),patch.object(worker,'_parallel_download') as download,patch('tongji_backend.transcriber.time.sleep') as delay:
            with self.assertRaises(media_tools.MediaToolError):
                worker.transcribe_url('https://example.test/video')
        download.assert_not_called()
        delay.assert_not_called()
        worker._asr.transcribe_file.assert_not_called()
