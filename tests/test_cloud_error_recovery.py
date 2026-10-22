"""Recovery protocol with temporary state, mocked vendors and loopback WS/Opus.

These tests verify retry hints and terminal ownership. They do not establish
physical firmware recovery or the quality/availability of live speech services.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import tests.test_doubao_cloud as cloud
from cloud_service import CloudPolicy
from core.handle.englishMvp import fail
from protocol_benchmark import OpusEncoderUtils


def temporary_policy(folder):
    registry = folder / 'devices.json'
    registry.write_text(json.dumps({'fixture': hashlib.sha256(b'fixture-token').hexdigest()}))
    registry.chmod(0o600)
    return CloudPolicy(folder, registry)


class RecoveryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.policy = temporary_policy(Path(self.temporary.name))

    def test_fixed_error_codes_delays_and_no_raw_provider_payload(self):
        cases = (
            ('empty_audio', 'empty_audio', 3000),
            ('empty_or_silent_audio', 'empty_audio', 3000),
            ('cloud_live_disabled', 'standby', 60000),
            ('device_credential_revoked', 'credential_revoked', 60000),
            ('cloud_turn_rate_limited', 'rate_limited', 3000),
            ('seed_asr_failed', 'asr_failed', 3000),
            ('doubao_english_failed:ValueError:invalid_english_language', 'english_failed', 3000),
            ('seed_tts_failed', 'tts_failed', 3000),
            ('TimeoutError', 'timeout', 3000),
            ('unknown_upstream_failure', 'other_error', 3000),
        )
        for reason, code, delay in cases:
            with self.subTest(reason=reason):
                raw = reason + ': Authorization=Bearer fixture-secret https://fixture.invalid/?api_key=fixture-key'
                response = self.policy.error_response(raw, now=0)
                self.assertEqual(response, {'error_code': code, 'retry_after_ms': delay})
                serialized = json.dumps(response)
                for sensitive in (raw, 'fixture-secret', 'fixture-key', 'Authorization', 'fixture.invalid'):
                    self.assertNotIn(sensitive, serialized)

    def test_budget_retry_rounds_up_to_next_utc_day(self):
        midnight = datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()
        cases = (
            (midnight, 86400000),
            (midnight + 3600, 82800000),
            (midnight + 86399, 1000),
            (midnight + 86398.9995, 1001),
            (midnight + 86399.9995, 1),
            (midnight + 86400, 86400000),
        )
        for now, expected in cases:
            with self.subTest(now=now):
                response = self.policy.error_response('daily_cloud_budget_exhausted', now=now)
                self.assertEqual(response, {'error_code': 'budget_exhausted',
                                            'retry_after_ms': expected})
                self.assertIs(type(response['retry_after_ms']), int)
                self.assertGreaterEqual(response['retry_after_ms'], 1)
                self.assertLessEqual(response['retry_after_ms'], 86400000)
        with patch('cloud_service.time.time', return_value=midnight + 86399.9995):
            self.assertEqual(self.policy.error_response('daily_cloud_budget_exhausted')['retry_after_ms'], 1)

    def test_non_string_errors_cannot_export_arbitrary_objects(self):
        for value in (None, {'api_key': 'fixture-secret'}, ['fixture-secret'], 42):
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual(self.policy.error_response(value, now=0),
                                 {'error_code': 'other_error', 'retry_after_ms': 3000})


class RecoveryProtocolTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = cloud.ContractTests.asyncTearDown
    llm_response = cloud.ContractTests.llm_response
    tts_response = cloud.ContractTests.tts_response

    async def asyncSetUp(self):
        self.enterContext(patch('httpx.HTTPTransport.handle_request',
            side_effect=AssertionError('External HTTP disabled in recovery tests')))
        self.enterContext(patch('httpx.AsyncHTTPTransport.handle_async_request',
            new=AsyncMock(side_effect=AssertionError('External HTTP disabled in recovery tests'))))
        await cloud.ContractTests.asyncSetUp(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.policy = temporary_policy(self.folder)

    async def test_local_failure_keeps_compatibility_and_sends_one_terminal_stop(self):
        conn = SimpleNamespace(websocket=SimpleNamespace(send=AsyncMock()),
            mvp_request_id='fixture-request', mvp_recording=False,
            mvp_auto_listening=True, mvp_audio_returning=True, mvp_streaming_tts=True,
            session_id='fixture-session', sentence_id='fixture-request', config={},
            clearSpeakStatus=Mock(), logger=Mock())
        with patch('core.handle.englishMvp.event'):
            await fail(conn, 'fixture_local_failure')
        messages = [json.loads(call.args[0]) for call in conn.websocket.send.await_args_list]
        self.assertEqual(len(messages), 2)
        error, stop = messages
        self.assertEqual(error['type'], 'error')
        self.assertIs(error['terminal'], True)
        self.assertEqual(error['error'], 'fixture_local_failure')
        self.assertEqual(error['error_code'], 'other_error')
        self.assertEqual(error['retry_after_ms'], 3000)
        self.assertEqual((stop['type'], stop['state']), ('tts', 'stop'))
        self.assertEqual(error['request_id'], stop['request_id'])
        self.assertFalse(conn.mvp_audio_returning)
        self.assertFalse(conn.mvp_streaming_tts)
        conn.clearSpeakStatus.assert_called_once()

    async def test_cloud_failure_sanitizes_error_and_includes_policy_retry_hint(self):
        conn = SimpleNamespace(websocket=SimpleNamespace(send=AsyncMock()),
            server=SimpleNamespace(cloud_policy=self.policy),
            mvp_request_id='fixture-request', mvp_recording=False,
            mvp_auto_listening=True, mvp_audio_returning=True, mvp_streaming_tts=True,
            session_id='fixture-session', sentence_id='fixture-request', config={},
            clearSpeakStatus=Mock(), logger=Mock())
        raw = 'device_credential_revoked: Authorization=Bearer fixture-secret'
        with patch('core.handle.englishMvp.event'):
            await fail(conn, raw)
        messages = [json.loads(call.args[0]) for call in conn.websocket.send.await_args_list]
        self.assertEqual(len(messages), 2)
        error, stop = messages
        self.assertIs(error['terminal'], True)
        self.assertEqual(error['error'], 'credential_revoked')
        self.assertEqual(error['error_code'], 'credential_revoked')
        self.assertEqual(error['retry_after_ms'], 60000)
        self.assertNotIn('fixture-secret', json.dumps(messages))
        self.assertNotIn(raw, json.dumps(messages))
        self.assertEqual((stop['type'], stop['state']), ('tts', 'stop'))

    def server(self, asr):
        config = cloud.load_cloud_config(require_credentials=False)
        config['selected_module'] = {}
        config['english_mvp']['log_path'] = str(self.folder / 'events.jsonl')
        server = cloud.WebSocketServer(config)
        server._asr = asr
        server._llm = self.llm
        return server

    async def upload(self, ws):
        packets = []
        OpusEncoderUtils(16000, 1, 60).encode_pcm_to_opus_stream(cloud.PCM, True, packets.append)
        await ws.send(json.dumps({'type': 'listen', 'state': 'start', 'mode': 'manual'}))
        for packet in packets:
            await ws.send(packet)
        await ws.send(json.dumps({'type': 'listen', 'state': 'stop'}))

    async def collect_until_stop(self, ws):
        messages, packets = [], []
        while True:
            incoming = await asyncio.wait_for(ws.recv(), 5)
            if isinstance(incoming, bytes):
                packets.append(incoming)
                continue
            row = json.loads(incoming)
            messages.append(row)
            if row.get('type') == 'tts' and row.get('state') == 'stop':
                return messages, packets

    async def test_cloud_terminal_failure_then_new_turn_works_on_same_socket(self):
        asr = SimpleNamespace(close=AsyncMock(),
            speech_to_text=AsyncMock(return_value=('妈妈，我想喝水。', None)))
        server = self.server(asr)
        server.cloud_policy = self.policy
        self.teaching_reply = {'intent': 'express', 'english': 'Mom, I want some water.'}
        reason = 'seed_asr_failed: fixture-secret'
        with patch.object(self.policy, 'before_turn', side_effect=[ValueError(reason), {}]), \
             patch('core.connection.initialize_tts', lambda _: self.tts):
            async with cloud.websockets.serve(server._handle_connection, '127.0.0.1', 0) as listener:
                ws = await cloud.connect(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/')
                try:
                    await self.upload(ws)
                    failed, audio = await self.collect_until_stop(ws)
                    self.assertEqual(audio, [])
                    errors = [row for row in failed if row['type'] == 'error']
                    self.assertEqual(len(errors), 1)
                    self.assertIs(errors[0]['terminal'], True)
                    self.assertEqual(errors[0]['error_code'], 'asr_failed')
                    self.assertEqual(errors[0]['retry_after_ms'], 3000)
                    self.assertNotIn('fixture-secret', json.dumps(failed))
                    asr.speech_to_text.assert_not_awaited()
                    self.assertEqual(self.requests, [])
                    # No implicit replay of failed speech: the next turn is an
                    # explicit new recording, with no cloud reservation in tests.
                    await self.upload(ws)
                    complete, audio = await self.collect_until_stop(ws)
                    self.assertGreater(len(audio), 0)
                    self.assertFalse(any(row['type'] == 'error' for row in complete), complete)
                    spoken = next(row for row in complete if row.get('state') == 'sentence_start')
                    self.assertEqual(spoken['text'], 'Mom, I want some water.')
                    self.assertNotEqual(failed[-1]['request_id'], complete[-1]['request_id'])
                    asr.speech_to_text.assert_awaited_once()
                finally:
                    await ws.close()

    async def test_busy_and_malformed_controls_are_nonterminal_during_active_request(self):
        started, resume = asyncio.Event(), asyncio.Event()

        async def delayed_asr(*_):
            started.set()
            await resume.wait()
            return '妈妈，我想喝水。', None

        server = self.server(SimpleNamespace(close=AsyncMock(), speech_to_text=delayed_asr))
        with patch('core.connection.initialize_tts', lambda _: self.tts):
            async with cloud.websockets.serve(server._handle_connection, '127.0.0.1', 0) as listener:
                ws = await cloud.connect(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/')
                errors = []
                try:
                    await self.upload(ws)
                    await asyncio.wait_for(started.wait(), 5)
                    for control in (json.dumps({'type': 'listen', 'state': 'start', 'mode': 'manual'}), '[]'):
                        await ws.send(control)
                        error = json.loads(await asyncio.wait_for(ws.recv(), 5))
                        self.assertEqual(error['type'], 'error')
                        self.assertIs(error['terminal'], False)
                        errors.append(error)
                        # A following pong confirms the control didn't enqueue
                        # an early tts:stop while the original ASR still waits.
                        await ws.send('{"type":"ping"}')
                        self.assertEqual(json.loads(await asyncio.wait_for(ws.recv(), 5)), {'type': 'pong'})
                    self.assertEqual(errors[0]['error'], 'busy')
                    self.assertEqual(errors[0]['request_id'], errors[1]['request_id'])
                    resume.set()
                    complete, audio = await self.collect_until_stop(ws)
                    self.assertGreater(len(audio), 0)
                    self.assertFalse(any(row['type'] == 'error' for row in complete), complete)
                    self.assertEqual(complete[-1]['request_id'], errors[0]['request_id'])
                finally:
                    resume.set()
                    await ws.close()
        events = [json.loads(line) for line in (self.folder / 'events.jsonl').read_text().splitlines()]
        terminal = [event for event in events if event['event'] == 'request_complete']
        self.assertEqual(len(terminal), 1)
        self.assertTrue(terminal[0]['success'])


if __name__ == '__main__':
    unittest.main()
