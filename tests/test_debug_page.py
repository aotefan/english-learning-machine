"""Verify the viewer does not misreport requests or expose unrelated files."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from debug_page import create_app, snapshot


class DebugPageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'events.jsonl'

    def write(self, rows):
        self.path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))

    def test_stages_timings_and_receipt_do_not_claim_audible_success(self):
        self.write([
            dict(time=1, request_id='a', event='recording_started', mode='auto'),
            dict(time=2, request_id='a', event='asr_result', text='我要喝水', asr_ms=600),
            dict(time=3, request_id='a', event='english_result', text='I want water.', english_ms=200),
            dict(time=4, request_id='a', event='request_complete', success=True, total_ms=3000, asr_ms=None),
            dict(time=5, request_id='a', event='playback_started', source='codec_output_returned'),
        ])
        row = snapshot(self.path)['requests'][0]
        self.assertEqual(row['stage'], '链路完成')
        self.assertEqual(row['timings']['asr_ms'], 600)
        self.assertTrue(row['playback_ack'])
        self.assertNotIn('audible_success', row)

    def test_partial_corrupt_and_connection_events_do_not_create_requests(self):
        self.write([dict(time=1, request_id=None, event='vad_listening'), [],
                    dict(time=float('nan'), event='error'),
                    dict(time=2, request_id='a', event='error', error='asr_timeout')])
        with self.path.open('a') as f:
            f.write('{"broken":\n{"time":3')
        result = snapshot(self.path)
        self.assertEqual(result['skipped_lines'], 3)
        self.assertEqual(len(result['requests']), 1)
        self.assertEqual(result['requests'][0]['stage'], '失败')

    def test_missing_replaced_and_large_logs(self):
        self.assertEqual(snapshot(self.path)['log_status'], 'missing')
        self.write([dict(time=i, request_id=str(i), event='recording_started') for i in range(30)])
        self.assertEqual(len(snapshot(self.path)['requests']), 20)
        with patch('debug_page.MAX_BYTES', 180):
            result = snapshot(self.path)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['requests'][0]['request_id'], '29')
        self.write([dict(time=40, request_id='new', event='recording_started')])
        self.assertEqual(len(snapshot(self.path)['requests']), 1)

    async def test_http_read_only_no_arbitrary_files_or_remote_host(self):
        self.write([dict(time=1, request_id='a', event='asr_result', text='<script>bad()</script>')])
        async with TestClient(TestServer(create_app(self.path))) as client:
            response = await client.get('/api/requests')
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual((await response.json())['requests'][0]['asr'], '<script>bad()</script>')
            self.assertNotIn('Access-Control-Allow-Origin', response.headers)
            self.assertEqual((await client.post('/api/requests')).status, 405)
            self.assertEqual((await client.get('/events.jsonl')).status, 404)
            self.assertEqual((await client.get('/api/requests', headers={'Host':'remote.invalid'})).status, 403)
            self.assertIn('英语终端', await (await client.get('/')).text())


if __name__ == '__main__':
    unittest.main()
