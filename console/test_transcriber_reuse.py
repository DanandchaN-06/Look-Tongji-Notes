"""No network: prove ASR retries and repeat tasks don't repeat media transfer."""
import json
import hashlib
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from console import agent_bridge
from scripts import look_tongji as cli
from tongji_backend.transcriber import Transcriber, TranscriptionError


class ReuseTests(unittest.TestCase):
    def test_asr_retry_downloads_once_and_reuses_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            audio=Path(folder)/'audio.mp3'
            audio.write_bytes(b'synthetic audio')
            worker=Transcriber()
            worker._asr=Mock()
            worker._asr.transcribe_file.side_effect=[RuntimeError('temporary ASR failure'),('text','srt',[])]
            with patch.object(worker,'_download_audio',return_value=str(audio)) as download, patch('tongji_backend.transcriber.time.sleep'), patch('tongji_backend.transcriber.config.MAX_ASR_RETRIES',5):
                self.assertEqual(worker.transcribe_url('https://example.test/video')[0],'text')
            self.assertEqual(download.call_count,1)
            self.assertEqual(worker._asr.transcribe_file.call_count,2)
            self.assertFalse(audio.exists())

    def test_audio_survives_exhausted_asr_and_is_used_by_next_task(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=Path(folder)/'cache'/'audio.mp3'
            worker=Transcriber()
            worker._asr=Mock()
            worker._asr.transcribe_file.side_effect=RuntimeError('ASR unavailable')
            def download(*args,**kwargs):
                cache.parent.mkdir(parents=True,exist_ok=True)
                cache.write_bytes(b'synthetic audio')
                return str(cache)
            with patch.object(worker,'_download_audio',side_effect=download) as fetch, patch('tongji_backend.transcriber.time.sleep'), patch('tongji_backend.transcriber.config.MAX_ASR_RETRIES',2):
                with self.assertRaises(TranscriptionError):
                    worker.transcribe_url('https://example.test/video',audio_cache_path=cache)
                self.assertEqual(fetch.call_count,1)
            self.assertTrue(cache.exists())
            other=Transcriber()
            other._asr=Mock()
            other._asr.transcribe_file.return_value=('text','srt',[])
            with patch.object(other,'_download_audio',side_effect=AssertionError('must use saved audio')):
                self.assertEqual(other.transcribe_url('https://example.test/video',audio_cache_path=cache)[0],'text')

    def test_ignored_range_is_rejected_before_reading_whole_video(self):
        with tempfile.TemporaryDirectory() as folder:
            head=Mock(headers={'Content-Length':'4194304','Accept-Ranges':'bytes'})
            response=Mock(status_code=200,headers={})
            response.iter_content.return_value=[b'synthetic media']
            with patch('tongji_backend.transcriber.requests.head',return_value=head),patch('tongji_backend.transcriber.requests.get',return_value=response):
                with self.assertRaises(TranscriptionError):
                    Transcriber()._parallel_download('https://example.test/video',tmp_dir=folder)
            response.iter_content.assert_not_called()

    def test_truncated_range_is_not_merged_as_complete_video(self):
        with tempfile.TemporaryDirectory() as folder:
            head=Mock(headers={'Content-Length':'12','Accept-Ranges':'bytes'})
            response=Mock(status_code=206,headers={'Content-Range':'bytes 0-11/12'})
            response.iter_content.return_value=[b'part']
            with patch('tongji_backend.transcriber.requests.head',return_value=head),patch('tongji_backend.transcriber.requests.get',return_value=response):
                with self.assertRaises(TranscriptionError):
                    Transcriber()._parallel_download('https://example.test/video',tmp_dir=folder)
            self.assertFalse((Path(folder)/'video_raw.tmp').exists())

    def test_saved_transcript_skips_video_lookup_and_transcription(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'101_202.txt').write_text('already transcribed',encoding='utf-8')
            (root/'101_202.srt').write_text('already generated',encoding='utf-8')
            meta={'course_id':'101','sub_id':'202','user':'QA','complete':True,'artifact_sha256':{'transcript_txt':hashlib.sha256((root/'101_202.txt').read_bytes()).hexdigest(),'subtitle_srt':hashlib.sha256((root/'101_202.srt').read_bytes()).hexdigest()},'artifacts':{'subtitle_srt':str(root/'101_202.srt')}}
            (root/'101_202.json').write_text(json.dumps(meta),encoding='utf-8')
            client=Mock()
            with patch.object(cli,'_state_dir',return_value=root/'state'),patch('tongji_backend.transcriber.Transcriber') as worker:
                result=cli._run_transcript_job(client=client,username='QA',course_id='101',sub_id='202',lecture_url='',output_dir=str(root))
            self.assertEqual(result,0)
            client.get_video_url.assert_not_called()
            worker.assert_not_called()
            (root/'101_202.txt').write_text('x',encoding='utf-8')
            self.assertFalse(cli._saved_transcript_ready(root,'101','202','QA'))
            meta.pop('user')
            (root/'101_202.json').write_text(json.dumps(meta),encoding='utf-8')
            self.assertFalse(cli._saved_transcript_ready(root,'101','202','QA'))

    def test_extraction_failure_keeps_download_and_never_downloads_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            video=root/'video_raw.tmp'
            video.write_bytes(b'synthetic media')
            worker=Transcriber()
            with patch('tongji_backend.transcriber.resolve_ffmpeg',return_value='ffmpeg'),patch.object(worker,'_parallel_download',return_value=str(video)) as fetch,patch.object(worker,'_extract_audio',side_effect=TranscriptionError('extract failed')),patch('tongji_backend.transcriber.subprocess.run') as fallback:
                with self.assertRaises(TranscriptionError):
                    worker._download_audio('https://example.test/video',cache_path=root/'audio.mp3')
                fetch.assert_not_called()
                fallback.assert_not_called()
            self.assertTrue(video.exists())

    def test_complete_ranges_merge_and_cleanup_scratch(self):
        with tempfile.TemporaryDirectory() as folder:
            head=Mock(headers={'Content-Length':'12','Accept-Ranges':'bytes'})
            response=Mock(status_code=206,headers={'Content-Range':'bytes 0-11/12'})
            response.iter_content.return_value=[b'hello media!']
            with patch('tongji_backend.transcriber.requests.head',return_value=head),patch('tongji_backend.transcriber.requests.get',return_value=response):
                video=Transcriber()._parallel_download('https://example.test/video',tmp_dir=folder)
            self.assertEqual(Path(video).read_bytes(),b'hello media!')
            self.assertEqual(list(Path(folder).glob('chunk_*.tmp')),[])
            self.assertFalse((Path(folder)/'video_raw.tmp.part').exists())

    def test_broken_range_resumes_from_bytes_already_saved(self):
        import requests
        with tempfile.TemporaryDirectory() as folder:
            head=Mock(headers={'Content-Length':'12','Accept-Ranges':'bytes'})
            first=Mock(status_code=206,headers={'Content-Range':'bytes 0-11/12'})
            def broken(*args,**kwargs):
                yield b'hell'
                raise requests.exceptions.ChunkedEncodingError('simulated dropped connection')
            first.iter_content.side_effect=broken
            second=Mock(status_code=206,headers={'Content-Range':'bytes 4-11/12'})
            second.iter_content.return_value=[b'o media!']
            with patch('tongji_backend.transcriber.requests.head',return_value=head),patch('tongji_backend.transcriber.requests.get',side_effect=[first,second]) as get,patch('tongji_backend.transcriber.time.sleep'):
                video=Transcriber()._parallel_download('https://example.test/video',tmp_dir=folder)
            self.assertEqual(Path(video).read_bytes(),b'hello media!')
            self.assertEqual(get.call_args.kwargs['headers']['Range'],'bytes=4-11')

    def test_forced_transcript_interruption_invalidates_old_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'101_202.json').write_text(json.dumps({'course_id':'101','sub_id':'202','user':'QA'}),encoding='utf-8')
            client=Mock()
            client.get_video_url.return_value='https://example.test/video'
            client.get_stream_params.return_value=('https://example.test/stream',None)
            writer=Path.write_text
            def interrupted(path,*args,**kwargs):
                if path.suffixes[-2:]==['.srt','.part']:
                    raise OSError('simulated interrupted subtitle write')
                return writer(path,*args,**kwargs)
            with patch.object(cli,'_state_dir',return_value=root/'state'),patch('tongji_backend.transcriber.Transcriber') as worker,patch.object(Path,'write_text',interrupted):
                worker.return_value.transcribe_url.return_value=('fresh text','fresh srt',[])
                with self.assertRaises(OSError):
                    cli._run_transcript_job(client=client,username='QA',course_id='101',sub_id='202',lecture_url='',output_dir=str(root),force_transcribe=True)
            self.assertFalse(cli._saved_transcript_ready(root,'101','202','QA'))
            self.assertFalse((root/'101_202.json').exists())

    def test_legacy_asr_files_must_match_recorded_segments(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'101_202.txt').write_text('完整旧字幕\n',encoding='utf-8')
            (root/'101_202.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\n完整旧字幕\n',encoding='utf-8')
            (root/'101_202.json').write_text(json.dumps({'course_id':'101','sub_id':'202','user':'QA','utterances':[{'transcript':'完整旧字幕','start_time':0,'end_time':1000}],'artifacts':{'subtitle_srt':'101_202.srt'}}),encoding='utf-8')
            self.assertTrue(cli._saved_transcript_ready(root,'101','202','QA'))
            (root/'101_202.txt').write_text('完',encoding='utf-8')
            self.assertFalse(cli._saved_transcript_ready(root,'101','202','QA'))

    def test_overlapping_same_lecture_jobs_share_one_completed_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            client=Mock()
            client.get_video_url.return_value='https://example.test/video'
            client.get_stream_params.return_value=('https://example.test/stream',None)
            entered=threading.Event()
            release=threading.Event()
            def cloud(*args,**kwargs):
                entered.set()
                self.assertTrue(release.wait(3))
                return ('fresh text','fresh srt',[])
            args=dict(client=client,username='QA',course_id='101',sub_id='202',lecture_url='',output_dir=str(root))
            with patch.object(cli,'_state_dir',return_value=root/'state'),patch('tongji_backend.transcriber.Transcriber') as worker,ThreadPoolExecutor(max_workers=2) as pool:
                worker.return_value.transcribe_url.side_effect=cloud
                first=pool.submit(cli._run_transcript_job,**args)
                self.assertTrue(entered.wait(3))
                second=pool.submit(cli._run_transcript_job,**args)
                release.set()
                self.assertEqual(first.result(3),0)
                self.assertEqual(second.result(3),0)
                self.assertEqual(worker.return_value.transcribe_url.call_count,1)

    def test_force_option_reaches_real_cli_parser(self):
        from console.runner import build_argv
        for kind in ('note','transcribe','batch'):
            args=cli.build_parser().parse_args(build_argv(kind,{'course_id':'101','sub_id':'202','force_transcribe':True}))
            self.assertTrue(args.force_transcribe)

    def test_two_accounts_writing_same_lecture_output_are_serialized(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            client=Mock()
            client.get_video_url.return_value='https://example.test/video'
            client.get_stream_params.return_value=('https://example.test/stream',None)
            entered=threading.Event()
            second_entered=threading.Event()
            release=threading.Event()
            count=[]
            def cloud(*args,**kwargs):
                count.append(1)
                if len(count)==1:
                    entered.set()
                    self.assertTrue(release.wait(3))
                else:
                    second_entered.set()
                return ('fresh text','fresh srt',[])
            args=dict(client=client,course_id='101',sub_id='202',lecture_url='',output_dir=str(root))
            with patch.object(cli,'_state_dir',return_value=root/'state'),patch('tongji_backend.transcriber.Transcriber') as worker,ThreadPoolExecutor(max_workers=2) as pool:
                worker.return_value.transcribe_url.side_effect=cloud
                first=pool.submit(cli._run_transcript_job,username='QA1',**args)
                self.assertTrue(entered.wait(3))
                second=pool.submit(cli._run_transcript_job,username='QA2',**args)
                try:
                    self.assertFalse(second_entered.wait(.2))
                finally:
                    release.set()
                self.assertEqual(first.result(3),0)
                self.assertEqual(second.result(3),0)
                self.assertTrue(cli._saved_transcript_ready(root,'101','202','QA2'))


if __name__=='__main__':
    unittest.main()
