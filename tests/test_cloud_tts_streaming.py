"""No-network TTS chunking, first-packet timing and complete-playback contracts."""
import asyncio
import base64
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import opuslib_next
import tests.test_doubao_cloud as cloud
from cloud_service import CloudPolicy
from core.handle import englishMvp
from core.utils.pcm_opus_stream import PCMOpusStream


def audio_line(pcm):
    return (json.dumps({'code': 0, 'data': base64.b64encode(pcm).decode()}) + '\n').encode()


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks, release=None, failure=None):
        self.chunks = chunks
        self.release = release
        self.failure = failure
        self.closed = False
        self.entered = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        for index, chunk in enumerate(self.chunks):
            yield audio_line(chunk)
            if index == 0 and self.release is not None:
                await self.release.wait()
        if self.failure:
            raise self.failure
        yield b'{"code":20000000,"usage":{"text_words":12}}\n'

    async def aclose(self):
        self.closed = True


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.first_packet = asyncio.Event()
        self.request = SimpleNamespace(path='/xiaozhi/v1/', headers={
            'device-id': 'stream-fixture', 'authorization': 'Bearer test-token'})

    async def send(self, message):
        self.sent.append(message)
        if isinstance(message, bytes):
            self.first_packet.set()


class SilentLogger:
    def bind(self, **_): return self
    def info(self, *_): pass
    def debug(self, *_): pass
    def error(self, *_): pass


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch('httpx.HTTPTransport.handle_request',
            side_effect=AssertionError('External HTTP disabled in streaming tests')))
        self.enterContext(patch('httpx.AsyncHTTPTransport.handle_async_request',
            new=AsyncMock(side_effect=AssertionError('External HTTP disabled in streaming tests'))))
        self.tts = cloud.TTSProvider({'api_key': 'test-secret', 'speaker': 'test-speaker'})
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        registry = self.folder / 'devices.json'
        registry.write_text(json.dumps({'stream-fixture': hashlib.sha256(b'test-token').hexdigest()}))
        registry.chmod(0o600)
        with patch.dict(os.environ, {'ASR_CNY_PER_HOUR': '1', 'LLM_INPUT_CNY_PER_MILLION': '.2',
                'LLM_OUTPUT_CNY_PER_MILLION': '2', 'TTS_CNY_PER_10K_CHARS': '3',
                'DAILY_CNY_LIMIT': '20', 'DAILY_TURN_LIMIT': '200'}):
            self.policy = CloudPolicy(self.folder / 'state', registry, live=True)
        self.requests = []

    async def asyncTearDown(self):
        await self.tts.close()
        conn = getattr(self, 'conn', None)
        if conn and getattr(conn, 'audio_rate_controller', None):
            conn.audio_rate_controller.reset()

    def use_stream(self, stream):
        def respond(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, stream=stream)
        self.tts.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

    def connection(self, *, intent='express', requires_completion=True):
        conn = SimpleNamespace(websocket=FakeSocket(), device_id='stream-fixture',
            server=SimpleNamespace(cloud_policy=self.policy), logger=SilentLogger(),
            tts=self.tts, sample_rate=24000, config={'english_mvp': {'enabled': True}},
            stop_event=threading.Event(), client_listen_mode='manual',
            conn_from_mqtt_gateway=False, mvp_hello=True,
            mvp_requires_completion=requires_completion, mvp_teaching_mode='express',
            mvp_last_english='Old sentence.', mvp_last_wav=b'old-wav',
            mvp_help_context={'child': 'old', 'assistant': 'old help', 'replay_previous': True},
            mvp_help_at=time.monotonic(), mvp_dialogue_context=[],
            session_id='fixture-session', clearSpeakStatus=lambda: None,
            asr=SimpleNamespace(speech_to_text=AsyncMock(
                return_value=('我想喝水。', {'input_audio_seconds': .3}))),
            llm=SimpleNamespace(teaching_response=lambda *args, **kwargs:
                (intent, 'I want water.', {'input_tokens': 12, 'output_tokens': 5})))
        englishMvp.begin_request(conn)
        conn.mvp_recording = False
        conn.mvp_submitted_at = time.perf_counter()
        self.policy.last_audio[conn.device_id] = ('Old sentence.', b'old-wav', time.time())
        self.conn = conn
        return conn

    def rows(self, table='events'):
        with sqlite3.connect(self.policy.db_path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM ' + table)]

    async def acknowledge(self, conn, *, completed=True):
        events = [('playback_started', 'codec_output_returned')]
        if completed:
            events.append(('playback_completed', 'codec_queue_drained'))
        for event, source in events:
            await englishMvp.handle_message(conn, json.dumps({'type': 'mvp_playback',
                'request_id': conn.mvp_request_id, 'event': event, 'source': source}))

    async def test_natural_english_finishes_without_audio_synthesis_or_playback_promotion(self):
        conn = self.connection(intent='no_change')
        conn.asr.speech_to_text = AsyncMock(return_value=(
            'I found my green pencil.', {'input_audio_seconds': .3}))
        conn.llm.teaching_response = lambda *args, **kwargs: (
            'no_change', '', {'input_tokens': 12, 'output_tokens': 3})
        conn.mvp_clarification_context = {'turns': [
            {'child': '那个放哪儿？', 'assistant': '你说的是哪个物品？'}]}
        conn.mvp_clarification_at = time.monotonic()
        self.use_stream(ChunkStream([cloud.PCM]))
        await englishMvp.process(conn, [cloud.PCM])
        messages = [json.loads(msg) for msg in conn.websocket.sent if isinstance(msg, str)]
        self.assertFalse(any(isinstance(msg, bytes) for msg in conn.websocket.sent))
        self.assertFalse(any(msg.get('state') == 'start' for msg in messages))
        self.assertTrue(messages[-1]['no_audio'])
        self.assertEqual(messages[-1]['state'], 'stop')
        self.assertEqual(messages[-1]['request_id'], conn.mvp_request_id)
        self.assertFalse(self.requests)
        self.assertTrue(conn.mvp_request_finished)
        self.assertIsNone(conn.mvp_pending_english)
        self.assertIsNone(conn.mvp_clarification_context)
        self.assertIsNone(conn.mvp_help_context)
        self.assertIsNone(self.policy.last_played_english(conn.device_id))
        self.assertFalse(self.policy.pending_audio)
        events = [json.loads(row['data']) for row in self.rows()]
        self.assertTrue(next(row for row in events if row['event'] == 'request_complete')['success'])
        completion = next(row for row in events if row['event'] == 'request_complete')
        self.assertTrue(completion['usage_complete'])
        self.assertIsNone(completion['tts_ms'])
        self.assertFalse(any(row['event'] in ('tts_started', 'audio_sent', 'playback_confirmed') for row in events))
        self.assertEqual({row['stage'] for row in self.rows('billing_stages')}, {'asr', 'english'})
        learning = self.rows('learning')
        self.assertEqual(learning[-1]['intent'], 'no_change')
        self.assertFalse(learning[-1]['playback_reported'])
        self.assertEqual(self.rows('daily')[-1]['turns'], 1)
        # A malicious/late acknowledgement cannot promote a silent turn.
        await self.acknowledge(conn)
        self.assertFalse(self.rows('learning')[-1]['playback_reported'])

    async def test_unchanged_english_is_silent_even_when_model_chooses_express(self):
        conn = self.connection()
        conn.asr.speech_to_text = AsyncMock(return_value=(
            'I want to draw a star.', {'input_audio_seconds': .3}))
        conn.llm.teaching_response = lambda *args, **kwargs: (
            'express', 'I want to draw a star.', {'input_tokens': 12, 'output_tokens': 8})
        self.use_stream(ChunkStream([cloud.PCM]))
        await englishMvp.process(conn, [cloud.PCM])
        self.assertFalse(self.requests)
        messages = [json.loads(msg) for msg in conn.websocket.sent if isinstance(msg, str)]
        self.assertTrue(messages[-1]['no_audio'])
        self.assertFalse(any(isinstance(msg, bytes) for msg in conn.websocket.sent))
        self.assertEqual(self.rows('learning')[-1]['intent'], 'no_change')

    async def test_recognition_is_displayed_before_model_and_without_early_playback(self):
        stream = ChunkStream([cloud.PCM])
        self.use_stream(stream)
        conn = self.connection()
        entered = asyncio.Event(); release = threading.Event()
        loop = asyncio.get_running_loop()
        def model(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3): raise RuntimeError('fixture_model_wait_expired')
            return ('express', 'I want water.', {'input_tokens':12,'output_tokens':5})
        conn.llm.teaching_response = model
        task = asyncio.create_task(englishMvp.process(conn, [cloud.PCM]))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            frames = [json.loads(msg) for msg in conn.websocket.sent if isinstance(msg,str)]
            self.assertEqual([msg['text'] for msg in frames if msg['type']=='stt'], ['我想喝水。'])
            self.assertFalse(any(msg.get('type')=='tts' and msg.get('state')=='start' for msg in frames))
            self.assertFalse(conn.websocket.first_packet.is_set())
            self.assertIsNone(self.policy.last_played_english(conn.device_id))
        finally:
            release.set(); await asyncio.wait_for(task, 3)
        frames = [json.loads(msg) for msg in conn.websocket.sent if isinstance(msg,str)]
        self.assertEqual(sum(msg['type']=='stt' for msg in frames),1)
        self.assertEqual(sum(msg.get('type')=='tts' and msg.get('state')=='start' for msg in frames),1)
        self.assertTrue(conn.websocket.first_packet.is_set())

    async def test_failed_model_retains_recognized_source_without_audio(self):
        conn = self.connection()
        def failed_model(*args, **kwargs):raise RuntimeError('fixture_model_failed')
        conn.llm.teaching_response = failed_model
        await englishMvp.process(conn, [cloud.PCM])
        frames = [json.loads(msg) for msg in conn.websocket.sent if isinstance(msg,str)]
        self.assertEqual([msg['text'] for msg in frames if msg['type']=='stt'], ['我想喝水。'])
        self.assertFalse(conn.websocket.first_packet.is_set())
        self.assertIsNone(self.policy.last_played_english(conn.device_id))

    async def test_pcm_arbitrary_byte_boundaries_match_one_continuous_encoder(self):
        pcm = cloud.PCM + b'\x01\0' * 13
        reference = list(PCMOpusStream().feed(pcm, final=True))
        for size in (1, 2, 17, 2879, 2880, 2881, 5001):
            with self.subTest(size=size):
                encoder = PCMOpusStream()
                packets = []
                for start in range(0, len(pcm), size):
                    packets.extend(encoder.feed(pcm[start:start + size]))
                packets.extend(encoder.feed(b'', final=True))
                self.assertEqual(packets, reference)
                self.assertEqual(len(packets), (len(pcm) + 2879) // 2880)
                decoder = opuslib_next.Decoder(24000, 1)
                self.assertEqual(sum(len(decoder.decode(packet, 1440)) for packet in packets),
                                 len(packets) * 2880)
                with self.assertRaisesRegex(ValueError, 'already_finished'):
                    list(encoder.feed(b'', final=True))
        with self.assertRaisesRegex(ValueError, 'alignment'):
            list(PCMOpusStream().feed(b'\1', final=True))

    async def test_provider_streams_bytes_and_preserves_complete_wav_and_usage(self):
        chunks = [cloud.PCM[:1], cloud.PCM[1:2891], cloud.PCM[2891:]]
        stream = ChunkStream(chunks)
        self.use_stream(stream)
        received = []
        async def collect(chunk): received.append(chunk)
        wav = await self.tts.text_to_speak('I want water.', None, on_pcm=collect)
        self.assertEqual(received, chunks)
        with wave.open(io.BytesIO(wav)) as audio:
            self.assertEqual(audio.readframes(audio.getnframes()), cloud.PCM)
        self.assertEqual(self.tts.last_metrics['billed_text_words'], 12)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(stream.closed)

    async def test_first_packet_precedes_http_end_and_premature_ack_cannot_cache(self):
        for intent in ('express', 'chat'):
            with self.subTest(intent=intent):
                if self.tts.client is not None:
                    await self.tts.client.aclose()
                self.policy.last_turn.clear()
                release = asyncio.Event()
                stream = ChunkStream([cloud.PCM[:2881], cloud.PCM[2881:]], release)
                self.use_stream(stream)
                conn = self.connection(intent=intent)
                conn.mvp_task = asyncio.create_task(englishMvp.process(conn, [cloud.PCM]))
                await asyncio.wait_for(conn.websocket.first_packet.wait(), 2)
                self.assertFalse(conn.mvp_task.done())
                self.assertFalse(stream.closed)
                self.assertIsNone(conn.mvp_pending_wav)
                await self.acknowledge(conn)
                self.assertTrue(conn.mvp_playback_reported)
                self.assertFalse(conn.mvp_playback_completed)
                self.assertEqual(conn.mvp_last_english, 'Old sentence.')
                self.assertEqual(conn.mvp_last_wav, b'old-wav')
                self.assertNotIn(conn.mvp_request_id, self.policy.pending_audio)
                release.set()
                await asyncio.wait_for(conn.mvp_task, 3)
                self.assertFalse(conn.mvp_streaming_tts)
                packets = [x for x in conn.websocket.sent if isinstance(x, bytes)]
                self.assertEqual(packets, list(PCMOpusStream().feed(cloud.PCM, final=True)))
                await self.acknowledge(conn)
                self.assertTrue(conn.mvp_playback_completed)
                self.assertEqual(conn.mvp_last_english, 'I want water.')
                self.assertEqual(conn.mvp_last_wav, self.policy.last_played_audio(
                    conn.device_id, 'I want water.'))
                events = [row['event'] for row in self.rows()
                          if row['request_id'] == conn.mvp_request_id]
                self.assertLess(events.index('audio_first_packet'), events.index('tts_usage'))
                self.assertEqual(events.count('playback_confirmed'), 1)
                self.assertEqual([x['status'] for x in self.rows('billing_turns')][-1], 'success')
                self.assertEqual([x['status'] for x in self.rows('billing_stages')][-3:],
                                 ['estimated'] * 3)
                if getattr(conn, 'audio_rate_controller', None): conn.audio_rate_controller.reset()

    async def test_truncated_stream_does_not_replace_previous_played_context(self):
        stream = ChunkStream([cloud.PCM[:2881]], failure=httpx.ReadError('secret-provider-body'))
        self.use_stream(stream)
        conn = self.connection()
        conn.mvp_task = asyncio.create_task(englishMvp.process(conn, [cloud.PCM]))
        await asyncio.wait_for(conn.mvp_task, 3)
        self.assertTrue(conn.websocket.first_packet.is_set())
        await self.acknowledge(conn)
        self.assertFalse(conn.mvp_playback_completed)
        self.assertEqual(conn.mvp_last_english, 'Old sentence.')
        self.assertEqual(conn.mvp_last_wav, b'old-wav')
        self.assertEqual(conn.mvp_help_context['assistant'], 'old help')
        self.assertIsNone(conn.mvp_pending_wav)
        self.assertFalse(self.policy.pending_audio)
        self.assertEqual(self.policy.last_played_audio(conn.device_id, 'Old sentence.'), b'old-wav')
        self.assertEqual(self.rows('billing_turns')[0]['status'], 'failed')
        self.assertEqual(self.rows('billing_stages')[-1]['status'], 'unknown')
        errors = [json.loads(x) for x in conn.websocket.sent if isinstance(x, str)
                  and json.loads(x).get('type') == 'error']
        self.assertEqual(len(errors), 1)
        self.assertNotIn('secret-provider-body', json.dumps(errors))
        self.assertEqual(len(self.requests), 1)

    async def test_abort_during_http_stream_closes_without_promoting_or_finishing_audio(self):
        release = asyncio.Event()
        stream = ChunkStream([cloud.PCM[:2881], cloud.PCM[2881:]], release)
        self.use_stream(stream)
        conn = self.connection()
        conn.mvp_task = asyncio.create_task(englishMvp.process(conn, [cloud.PCM]))
        await asyncio.wait_for(conn.websocket.first_packet.wait(), 2)
        await self.acknowledge(conn, completed=False)
        await englishMvp.handle_message(conn, '{"type":"abort"}')
        await asyncio.wait_for(conn.mvp_task, 3)
        self.assertTrue(stream.closed)
        await self.acknowledge(conn)
        self.assertEqual(conn.mvp_last_english, 'Old sentence.')
        self.assertEqual(conn.mvp_last_wav, b'old-wav')
        self.assertFalse(self.policy.pending_audio)
        self.assertEqual(self.policy.last_played_audio(conn.device_id, 'Old sentence.'), b'old-wav')
        self.assertEqual(len([x for x in conn.websocket.sent if isinstance(x, bytes)]), 1)
        self.assertEqual(self.rows('billing_turns')[0]['status'], 'failed')
        self.assertEqual(len(self.requests), 1)

    async def test_too_long_or_odd_final_pcm_fails_and_has_no_complete_metrics(self):
        for chunks in ([b'\0\0' * (24000 * 30 + 1)], [cloud.PCM, b'\1']):
            with self.subTest(lengths=[len(x) for x in chunks]):
                if self.tts.client is not None: await self.tts.client.aclose()
                self.use_stream(ChunkStream(chunks))
                received = []
                async def collect(chunk): received.append(chunk)
                with self.assertRaisesRegex(RuntimeError, 'seed_tts_failed:ValueError'):
                    await self.tts.text_to_speak('I want water.', None, on_pcm=collect)
                self.assertEqual(self.tts.last_metrics, {})
                if len(chunks) == 1: self.assertFalse(received)
        self.assertEqual(len(self.requests), 2)

    async def test_legacy_and_explanations_keep_buffered_playback(self):
        for intent, completion in (('express', False), ('explain_only', True)):
            with self.subTest(intent=intent, completion=completion):
                if self.tts.client is not None: await self.tts.client.aclose()
                self.policy.last_turn.clear()
                release = asyncio.Event()
                stream = ChunkStream([cloud.PCM[:2881], cloud.PCM[2881:]], release)
                self.use_stream(stream)
                conn = self.connection(intent=intent, requires_completion=completion)
                conn.mvp_task = asyncio.create_task(englishMvp.process(conn, [cloud.PCM]))
                # Wait until the mocked HTTP response is actively consumed.
                await asyncio.wait_for(stream.entered.wait(), 2)
                await asyncio.sleep(0)
                self.assertFalse(conn.websocket.first_packet.is_set())
                release.set()
                await asyncio.wait_for(conn.mvp_task, 3)
                self.assertTrue(conn.websocket.first_packet.is_set())
                self.assertNotIn('audio_first_packet', [row['event'] for row in self.rows()
                    if row['request_id'] == conn.mvp_request_id])
                if getattr(conn, 'audio_rate_controller', None): conn.audio_rate_controller.reset()


if __name__ == '__main__': unittest.main()
