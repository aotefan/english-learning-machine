"""Only completion-aware MVP clients skip the estimated playback tail delay."""
import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import tests.test_doubao_cloud  # Establish the existing local backend path/config.
from core.handle import englishMvp
from core.handle.sendAudioHandle import sendAudio, send_tts_message, _wait_for_audio_completion
from core.utils.audioRateController import AudioRateController
from tests.test_cloud_tts_streaming import SilentLogger


class AudioCompletionTests(unittest.IsolatedAsyncioTestCase):
    def connection(self, *, completion=True, enabled=True, delay=0, mqtt=False):
        conn = SimpleNamespace(
            config={'english_mvp': {'enabled': enabled}, 'tts_audio_send_delay': delay},
            mvp_requires_completion=completion, conn_from_mqtt_gateway=mqtt,
            logger=SilentLogger(), websocket=SimpleNamespace(send=AsyncMock()),
            sentence_id='fixture', session_id='session', mvp_request_id='fixture',
            mvp_streaming_tts=True, mvp_audio_returning=True, client_abort=False,
            mvp_playback_reported=False, mvp_playback_completed=False,
            mvp_pending_english='A new sentence.', mvp_pending_intent='express',
            mvp_pending_wav=b'new-wav', mvp_pending_transcript='synthetic',
            mvp_last_english='Old sentence.', mvp_last_wav=b'old-wav',
            mvp_pending_used_clarification=None, clearSpeakStatus=lambda: None,
            mvp_recording=False, mvp_auto_listening=False, mvp_hello=True)
        conn.mvp_started = time.perf_counter()
        conn.mvp_submitted_at = conn.mvp_started
        self.connections.append(conn)
        return conn

    async def asyncSetUp(self):
        self.connections = []
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for conn in self.connections:
            controller = getattr(conn, 'audio_rate_controller', None)
            if controller: controller.reset()

    def task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.append(task)
        return task

    async def test_completed_mvp_sends_stop_without_estimated_playback_sleep(self):
        for delay in (0, 30):
            with self.subTest(delay=delay):
                conn = self.connection(delay=delay)
                conn.audio_rate_controller = AudioRateController(60, send_delay=delay)
                # No packets are in flight, so any sleep here is additional
                # playback estimation rather than essential transport pacing.
                with patch('core.handle.sendAudioHandle.asyncio.sleep', new=AsyncMock()) as sleep:
                    await send_tts_message(conn, 'stop')
                sleep.assert_not_awaited()
                messages = [json.loads(call.args[0]) for call in conn.websocket.send.await_args_list]
                self.assertEqual(messages, [{'type': 'tts', 'state': 'stop',
                    'session_id': 'session', 'request_id': 'fixture'}])
                self.assertFalse(conn.mvp_streaming_tts)

    async def test_legacy_non_mvp_and_gateway_keep_existing_playback_wait(self):
        for completion, enabled, mqtt, delay, expected in (
            (False, True, False, 0, .42), (False, True, False, 30, .06),
            (True, False, False, 0, .42), (True, True, True, 0, .42),
        ):
            with self.subTest(completion=completion, enabled=enabled, mqtt=mqtt, delay=delay):
                conn = self.connection(completion=completion, enabled=enabled, delay=delay, mqtt=mqtt)
                conn.audio_rate_controller = AudioRateController(60, send_delay=delay)
                with patch('core.handle.sendAudioHandle.asyncio.sleep', new=AsyncMock()) as sleep:
                    await _wait_for_audio_completion(conn)
                sleep.assert_awaited_once_with(expected)

    async def test_last_packet_must_finish_sending_even_after_popped_from_queue(self):
        conn = self.connection()
        last_send_entered, release = asyncio.Event(), asyncio.Event()
        sent = []

        async def send(message):
            if message == b'last':
                last_send_entered.set()
                await release.wait()
            sent.append(message)
        conn.websocket.send = send
        # The first five packets use the existing immediate prebuffer route;
        # the final packet then belongs to the background rate controller.
        await sendAudio(conn, [b'first'] * 5 + [b'last'])
        await asyncio.wait_for(last_send_entered.wait(), 1)
        self.assertEqual(len(conn.audio_rate_controller.queue), 0)
        self.assertFalse(conn.audio_rate_controller.queue_empty_event.is_set())
        stopping = self.task(send_tts_message(conn, 'stop'))
        await asyncio.sleep(0)
        self.assertFalse(stopping.done())
        self.assertTrue(conn.mvp_streaming_tts)
        self.assertEqual(sent, [b'first'] * 5)
        with patch('core.handle.englishMvp.event') as event:
            for name, source in (('playback_started', 'codec_output_returned'),
                                 ('playback_completed', 'codec_queue_drained')):
                await englishMvp.handle_message(conn, json.dumps({'type': 'mvp_playback',
                    'request_id': 'fixture', 'event': name, 'source': source}))
            self.assertTrue(conn.mvp_playback_reported)
            self.assertFalse(conn.mvp_playback_completed)
            self.assertEqual(conn.mvp_last_english, 'Old sentence.')
            # Once the *actual* last send returns, stop can follow immediately.
            release.set()
            with patch('core.handle.sendAudioHandle.asyncio.sleep', new=AsyncMock()) as sleep:
                await asyncio.wait_for(stopping, 1)
            sleep.assert_not_awaited()
            self.assertEqual(sent[:6], [b'first'] * 5 + [b'last'])
            self.assertEqual(json.loads(sent[-1])['state'], 'stop')
            self.assertFalse(conn.mvp_streaming_tts)
            await englishMvp.handle_message(conn, json.dumps({'type': 'mvp_playback',
                'request_id': 'fixture', 'event': 'playback_completed', 'source': 'codec_queue_drained'}))
            self.assertTrue(conn.mvp_playback_completed)
            self.assertEqual(conn.mvp_last_english, 'A new sentence.')
            self.assertEqual(sum(call.args[1] == 'playback_confirmed' for call in event.call_args_list), 1)

    async def test_cancel_wait_then_fail_clears_inflight_send_without_confirming(self):
        conn = self.connection()
        blocked, cancelled = asyncio.Event(), asyncio.Event()
        sent = []

        async def send(message):
            if message == b'last':
                blocked.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            sent.append(message)
        conn.websocket.send = send
        await sendAudio(conn, [b'first'] * 5 + [b'last'])
        await asyncio.wait_for(blocked.wait(), 1)
        stopping = self.task(send_tts_message(conn, 'stop'))
        await asyncio.sleep(0)
        # Match process cancellation: cancel its normal stop wait before fail
        # resets the sender. Reset wakes waiters but is not a success signal.
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
        conn.client_abort = True
        with patch('core.handle.englishMvp.event') as event:
            await englishMvp.fail(conn, 'request_aborted')
            await asyncio.wait_for(cancelled.wait(), 1)
            await englishMvp.handle_message(conn, json.dumps({'type': 'mvp_playback',
                'request_id': 'fixture', 'event': 'playback_completed', 'source': 'codec_queue_drained'}))
            self.assertFalse(conn.mvp_audio_returning)
            self.assertFalse(conn.mvp_streaming_tts)
            self.assertFalse(conn.mvp_playback_completed)
            self.assertEqual(conn.mvp_last_english, 'Old sentence.')
            self.assertFalse(any(call.args[1] == 'playback_confirmed' for call in event.call_args_list))
        self.assertNotIn(b'last', sent)
        controls = [json.loads(message) for message in sent if isinstance(message, str)]
        self.assertEqual([message['type'] for message in controls], ['error', 'tts'])
        self.assertTrue(controls[0]['terminal'])
        self.assertEqual(controls[1]['state'], 'stop')


if __name__ == '__main__': unittest.main()
