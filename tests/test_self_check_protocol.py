"""Diagnostic against actual upstream WebSocket/Opus with a controlled ASR fault."""
import contextlib
import copy
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_protocol import WebSocketServer, load_config, BrokenASR, English, TTS, websockets
from self_check import diagnose


class DiagnosticProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_asr_failure_report_then_next_request_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            config = copy.deepcopy(await load_config())
            config['selected_module'] = {}
            config['english_mvp']['log_path'] = str(folder/'events.jsonl')
            server = WebSocketServer(config)
            server._asr = BrokenASR();server._llm = English()
            with patch('core.connection.initialize_tts', lambda config: TTS({}, True)):
                listener = await websockets.serve(server._handle_connection, '127.0.0.1', 0)
                port = listener.sockets[0].getsockname()[1]
                info = dict(loaded=True, voice_ready=True, pids=[], host='127.0.0.1',port=port,
                            debug_port=1,event_log=str(folder/'events.jsonl'))
                service = SimpleNamespace(info=lambda:info)
                try:
                    with patch('self_check.command', new=AsyncMock()), \
                         patch('self_check.page_ready', side_effect=OSError('page unavailable')), \
                         contextlib.redirect_stdout(io.StringIO()):
                        first = folder/'first';first.mkdir()
                        bad = await diagnose(first, service)
                        self.assertFalse(bad['success'])
                        self.assertIn('injected_asr_timeout', bad['error'])
                        self.assertTrue(bad['request']['request_id'])
                        checks = {c['stage']:c['status'] for c in bad['checks']}
                        self.assertEqual(checks['ASR中文识别'], 'fail')
                        self.assertEqual(checks['英文转换'], 'not_run')
                        second = folder/'second';second.mkdir()
                        good = await diagnose(second, service)
                        self.assertTrue(good['success'])
                        self.assertFalse(good['hardware'])
                        self.assertEqual(next(c['status'] for c in good['checks'] if c['stage']=='调试页'), 'warning')
                        self.assertNotEqual(good['request']['request_id'], bad['request']['request_id'])
                        saved = json.loads((second/'report.json').read_text())
                        self.assertTrue(saved['success'])
                finally:
                    listener.close();await listener.wait_closed()


if __name__ == '__main__':
    unittest.main()
