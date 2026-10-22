"""Failure attribution and honest software-only diagnostic results."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from self_check import diagnose, pipeline_checks, command


class SelfCheckTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_failure_does_not_claim_later_stages_or_audibility(self):
        events = [{'event':'asr_result','text':'我要喝水'},
                  {'event':'error','error':'english_timeout'}]
        checks = pipeline_checks(events, {'success':False, 'output_frames':0})
        self.assertEqual([r['status'] for r in checks], ['pass','fail','not_run','not_run'])
        completed = events[:1]+[{'event':'english_result','text':'I want water.'},
                               {'event':'tts_generated','packets':5},
                               {'event':'audio_sent','return_ms':0}]
        self.assertEqual(pipeline_checks(completed, {'success':False,'output_frames':0})[-1]['status'], 'fail')

    async def test_stopped_server_writes_report_without_request_or_start(self):
        service = SimpleNamespace(info=lambda: {'loaded':False, 'voice_ready':False})
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            out = Path(temp)
            report = await diagnose(out, service)
            self.assertFalse(report['success'])
            self.assertFalse(report['hardware'])
            self.assertIsNone(report['request'])
            self.assertEqual(report['checks'][0]['stage'], '后端运行状态')
            self.assertFalse(json.loads((out/'report.json').read_text())['success'])

    async def test_missing_dependency_preflight_has_actionable_failure(self):
        service = SimpleNamespace(info=lambda: {'loaded':True, 'voice_ready':True,'pids':[123],'port':8000})
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()), \
             patch('self_check.command', new=AsyncMock(side_effect=RuntimeError('missing model'))):
            report = await diagnose(Path(temp), service)
            self.assertFalse(report['success'])
            self.assertEqual(report['checks'][-1]['stage'], '配置、模型文件与音频依赖')
            self.assertIn('preflight.log', report['checks'][-1]['detail'])

    async def test_command_timeout_terminates_owned_child(self):
        with tempfile.TemporaryDirectory() as temp:
            pid_file = Path(temp)/'pid'
            code = 'import os,time,pathlib;pathlib.Path('+repr(str(pid_file))+').write_text(str(os.getpid()));time.sleep(30)'
            with self.assertRaises(TimeoutError):
                await command([sys.executable, '-c', code], .5, Path(temp)/'out.log')
            import psutil
            self.assertFalse(psutil.pid_exists(int(pid_file.read_text())))


if __name__ == '__main__':
    unittest.main()
