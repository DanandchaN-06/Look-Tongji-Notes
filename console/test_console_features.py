"""Offline checks for course recovery and concrete Agent handoffs."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from console import agent_bridge, library
from console.runner import build_argv
from console.handoffs import sheet_target
from scripts import look_tongji as cli
from tongji_backend.checkpoints import path_for


class FeatureTests(unittest.TestCase):
    def test_advanced_flags_are_accepted_by_real_cli_parser(self):
        for kind in ('slide', 'note'):
            args = build_argv(kind, dict(course_id='101', sub_id='202', limit=3, concurrency=2, retries=4, timeout=20, force_login=True))
            parsed = cli.build_parser().parse_args(args)
            self.assertEqual((parsed.max_items, parsed.concurrency, parsed.retries, parsed.timeout), (3, 2, 4, 20))
        parsed = cli.build_parser().parse_args(build_argv('batch', dict(course_id='101', retry_failed=True)))
        self.assertTrue(parsed.retry_failed)

    def test_transcript_manifest_preserves_unmodified_metadata_and_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            raw = Path(folder)
            manifest = raw / 'manifest.json'
            manifest.write_text(json.dumps({'teacher': '示例教师', 'date': '2026-10-08', 'lecture_url': 'example', 'note_style': 'dialogue', 'artifacts': {'slides_dir': 'custom/slides', 'timeline_txt': 'custom/timeline.txt'}}), encoding='utf-8')
            ws = SimpleNamespace(manifest_path=manifest, course_title='测试课程', session_title='第1讲')
            with patch.object(cli, 'write_manifest') as write:
                cli._write_lecture_manifest_from_outputs(workspace=ws, course_id='101', sub_id='202', lecture_url='', transcript_output_dir=raw, slide_output_dir=raw/'slides', note_style='')
            result = write.call_args.args[1]
            self.assertEqual(result['teacher'], '示例教师')
            self.assertEqual(result['lecture_url'], 'example')
            self.assertEqual(result['note_style'], 'dialogue')
            self.assertEqual(result['artifacts']['slides_dir'], 'custom/slides')
            self.assertEqual(result['artifacts']['timeline_txt'], 'custom/timeline.txt')
            (raw/'101_202.json').write_text(json.dumps({'duration_seconds':7200}),encoding='utf-8')
            with patch.object(cli, 'write_manifest') as write:
                cli._write_lecture_manifest_from_outputs(workspace=ws,course_id='101',sub_id='202',lecture_url='',transcript_output_dir=raw,slide_output_dir=raw/'new-slides',note_style='',slides_regenerated=True)
            result = write.call_args.args[1]
            self.assertEqual(result['artifacts']['slides_dir'], str(raw/'new-slides'))
            self.assertNotIn('duration_warning',result)

    def test_course_a_b_a_resume_and_explicit_failed_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = SimpleNamespace(workspace_root=root)
            client = SimpleNamespace(get_course_detail=lambda _: {'lectures': [{'sub_id':'202','sub_title':'第1讲','has_playback':True}]})
            args = cli.build_parser().parse_args(['batch-transcribe','--course-id','101','--output-dir',str(root/'output'),'--no-workspace-prompt','--max-retries','1'])
            with patch.object(cli, '_ensure_authenticated_client', return_value=(client,'QA')), patch.object(cli, 'ensure_workspace_config', return_value=config), patch.object(cli, '_run_transcript_job', return_value=0) as job:
                self.assertEqual(cli.cmd_batch_transcribe(args), 0)
                args.course_id = '303'
                self.assertEqual(cli.cmd_batch_transcribe(args), 0)
                args.course_id = '101'
                self.assertEqual(cli.cmd_batch_transcribe(args), 0)
                self.assertEqual(job.call_count, 2)
                failed = json.loads(path_for(root, '101').read_text(encoding='utf-8'))
                failed['lectures'][0].update(status='failed', attempts=1)
                path_for(root, '101').write_text(json.dumps(failed), encoding='utf-8')
                self.assertEqual(cli.cmd_batch_transcribe(args), 3)
                self.assertEqual(job.call_count, 2)
                args.retry_failed = True
                self.assertEqual(cli.cmd_batch_transcribe(args), 0)
                self.assertEqual(job.call_count, 3)

    def test_custom_course_sheet_instruction_and_verification_use_same_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root/'review.html'
            target.write_text('<html>示例速查表</html>', encoding='utf-8')
            with patch.object(agent_bridge, '_workspace_root', return_value=root), patch.object(agent_bridge,'course_notes',return_value=[]), patch.object(agent_bridge, 'detect_artifacts', return_value={}):
                result = agent_bridge.build_agent_instruction(kind='cheatsheet',course_id='101',cheatsheet_scope='course',cheatsheet_output='review.html')
                self.assertIn(target.as_posix(), result['instruction'])
                checked = agent_bridge.verify_agent_output(kind='cheatsheet',course_id='101',sub_id='',cheatsheet_scope='course',cheatsheet_output='review.html')
                self.assertTrue(checked['ready'])
            with self.assertRaises(ValueError):
                sheet_target(root,'','101','','html','course','../escape.html')

    def test_publish_preflight_checks_current_site_without_deploying(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'site').mkdir()
            (root/'site'/'index.html').write_text('<a href="?token=private">secret link</a>', encoding='utf-8')
            with patch.object(library.config_store,'public_workspace',return_value={'workspace_root':str(root)}), patch.object(library.config_store,'public_credentials',return_value={'gh_pages_repo':'demo/notes'}), patch.object(library,'secret_values',return_value=[]), patch.object(library.shutil,'which',return_value='gh'), patch.object(library.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
                result = library.publish_preflight()
                self.assertFalse(result['ready'])
                self.assertEqual(result['findings'][0]['path'],'index.html')
                self.assertEqual(run.call_args.args[0], ['gh','auth','status'])


if __name__ == '__main__':
    unittest.main()
