"""Mode metadata contracts with local sockets and mocked speech, no paid APIs."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_cloud_tts_streaming as streaming
cloud = streaming.cloud
ChunkStream = streaming.ChunkStream
from core.handle import englishMvp


class TeachingModeHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_opt_in_initial_mode_and_rehandshake_reset(self):
        conn = SimpleNamespace(websocket=SimpleNamespace(send=AsyncMock()),
                               welcome_msg={'type': 'hello'}, mvp_closing=False)
        hello = {'type': 'hello', 'version': 1, 'features': {'mvp_teaching_mode': True},
                 'audio_params': {'format': 'opus', 'sample_rate': 16000,
                                  'channels': 1, 'frame_duration': 60}}
        for previous in ('unknown', 'chat'):
            conn.mvp_teaching_mode = previous
            conn.websocket.send.reset_mock()
            await englishMvp.handle_message(conn, json.dumps(hello))
            messages = [json.loads(c.args[0]) for c in conn.websocket.send.call_args_list]
            self.assertEqual(messages, [{'type': 'hello'},
                                       {'type': 'teaching_mode', 'mode': 'express'}])
            self.assertEqual(conn.mvp_teaching_mode, 'express')
        self.assertEqual(conn.welcome_msg, {'type': 'hello'})

    async def test_legacy_and_invalid_capabilities_get_original_handshake_only(self):
        for features in (None, {}, {'mvp_teaching_mode': False},
                         {'mvp_teaching_mode': 1}, {'mvp_teaching_mode': 'true'}):
            with self.subTest(features=features):
                conn = SimpleNamespace(websocket=SimpleNamespace(send=AsyncMock()),
                                       welcome_msg={'type': 'hello'})
                await englishMvp.handle_message(conn, json.dumps({
                    'type': 'hello', 'features': features,
                    'audio_params': {'format': 'opus', 'sample_rate': 16000,
                                     'channels': 1, 'frame_duration': 60}}))
                self.assertEqual(conn.websocket.send.call_count, 1)


class TeachingModeTurnTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = streaming.StreamingTests.asyncSetUp
    asyncTearDown = streaming.StreamingTests.asyncTearDown
    connection = streaming.StreamingTests.connection
    use_stream = streaming.StreamingTests.use_stream

    async def test_mode_switch_metadata_matches_server_even_if_speech_fails(self):
        for command, mode in (('陪练模式', 'chat'), ('表达模式', 'express'),
                              ('关闭陪练模式', 'express'), ('关掉陪练模式', 'express')):
            for fail in (False, True):
                with self.subTest(command=command, speech_failure=fail):
                    self.policy.last_turn.clear()
                    self.use_stream(ChunkStream([cloud.PCM]))
                    conn = self.connection()
                    conn.mvp_teaching_mode = 'chat' if mode == 'express' else 'express'
                    conn.mvp_reports_teaching_mode = True
                    conn.asr.speech_to_text = AsyncMock(return_value=(command, None))
                    conn.llm.teaching_response = lambda *_args, **_kw: self.fail('No model for mode command')
                    with patch.object(self.tts, 'text_to_speak',
                                      side_effect=RuntimeError('fixture failure')) if fail else patch.object(
                                          self.tts, 'text_to_speak', wraps=self.tts.text_to_speak):
                        await englishMvp.process(conn, [cloud.PCM])
                    messages = [json.loads(x) for x in conn.websocket.sent if isinstance(x, str)]
                    modes = [m for m in messages if m['type'] == 'teaching_mode']
                    self.assertEqual(modes, [{'type': 'teaching_mode', 'mode': mode}])
                    self.assertEqual(conn.mvp_teaching_mode, mode)
                    self.assertEqual(conn.mvp_dialogue_context, [])
                    self.assertIsNone(conn.mvp_help_context)
                    if fail:
                        self.assertTrue(any(m['type'] == 'error' for m in messages))
                    if getattr(conn, 'audio_rate_controller', None):
                        conn.audio_rate_controller.reset()

    async def test_quoted_mode_name_does_not_switch_or_emit_mode(self):
        self.use_stream(ChunkStream([cloud.PCM]))
        conn = self.connection()
        conn.mvp_reports_teaching_mode = True
        conn.asr.speech_to_text = AsyncMock(return_value=('陪练模式是什么意思？', None))
        await englishMvp.process(conn, [cloud.PCM])
        messages = [json.loads(x) for x in conn.websocket.sent if isinstance(x, str)]
        self.assertFalse(any(m['type'] == 'teaching_mode' for m in messages))
        self.assertEqual(conn.mvp_teaching_mode, 'express')
