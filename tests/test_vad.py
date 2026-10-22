"""Actual Silero/Opus/WS segmentation, with controllable downstream providers."""
import asyncio
import copy
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from test_protocol import ROOT, SERVER, ASR, BrokenASR, English, TTS, WebSocketServer, load_config, connect, one, websockets
from core.providers.vad.silero import VADProvider
from vad_benchmark import one_auto
from protocol_benchmark import OpusEncoderUtils


class AutoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        config = copy.deepcopy(await load_config())
        config['selected_module'] = {}
        config['english_mvp']['log_path'] = str(self.out/'events.jsonl')
        self.server = WebSocketServer(config)
        self.server._asr = ASR(); self.server._llm = English()
        self.server._vad = VADProvider({'model_dir': str(SERVER/'models/snakers4_silero-vad'),
                                       'min_silence_duration_ms': 800})
        self.tts = patch('core.connection.initialize_tts', lambda config: TTS({}, True))
        self.tts.start()
        self.listener = await websockets.serve(self.server._handle_connection, '127.0.0.1', 0)
        self.ws = await connect(f'ws://127.0.0.1:{self.listener.sockets[0].getsockname()[1]}/xiaozhi/v1/')

    async def asyncTearDown(self):
        await self.ws.close(); self.listener.close(); await self.listener.wait_closed()
        self.tts.stop(); self.temp.cleanup()

    def events(self):
        p = self.out/'events.jsonl'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    async def manual(self):
        self.assertTrue((await one(self.ws, {'id': '01'}, ROOT/'artifacts/synthetic', False))['success'])

    async def test_two_auto_turns_then_manual(self):
        results = []
        for i in ('01', '02'):
            result = await one_auto(self.ws, {'id': i}, ROOT/'artifacts/synthetic', self.out/'audio')
            self.assertTrue(result['success'], result); results.append(result)
        self.assertNotEqual(results[0]['request_id'], results[1]['request_id'])
        self.assertEqual(sum(e['event'] == 'vad_speech_stopped' for e in self.events()), 2)
        await self.manual()

    async def test_slow_pause_keeps_both_parts_then_manual(self):
        with wave.open(str(ROOT/'artifacts/synthetic/01.wav'), 'rb') as wav:
            pcm = wav.readframes(wav.getnframes())
        # Controlled synthetic pause inside the same Chinese fixture, not child audio.
        cut = int(16000 * .72) * 2
        paused = pcm[:cut] + bytes(int(32000 * 1.05)) + pcm[cut:]
        with wave.open(str(self.out/'pause.wav'), 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(paused)
        frames = []
        for silence in (800, 1400):
            self.server._vad.silence_threshold_ms = silence
            result = await one_auto(self.ws, {'id':'pause'}, self.out, self.out/f'audio-{silence}')
            self.assertTrue(result['success'], result)
            received = next(e for e in self.events() if e['event']=='audio_received' and e['request_id']==result['request_id'])
            frames.append(received['frames'])
            stop = next(e for e in self.events() if e['event']=='vad_speech_stopped' and e['request_id']==result['request_id'])
            self.assertEqual(stop['silence_ms'], silence)
        self.assertGreater(frames[1] - frames[0], 20, frames)
        await self.manual()

    async def test_silence_has_no_request_then_manual(self):
        await self.ws.send(json.dumps({'type': 'listen', 'state': 'start', 'mode': 'auto'}))
        packets = []
        OpusEncoderUtils(16000, 1, 60).encode_pcm_to_opus_stream(bytes(32000*25), True, packets.append)
        for packet in packets: await self.ws.send(packet)
        await self.ws.send(json.dumps({'type': 'ping'}))
        self.assertEqual(json.loads(await self.ws.recv())['type'], 'pong')
        self.assertFalse(any(e['event'] == 'recording_started' for e in self.events()))
        await self.ws.send(json.dumps({'type': 'listen', 'state': 'stop'}))
        self.assertEqual(json.loads(await self.ws.recv())['state'], 'stop')
        await self.manual()

    async def test_provider_error_then_auto(self):
        # The existing connection owns this ASR instance; fail its first call.
        broken = BrokenASR()
        with patch.object(self.server._asr, 'speech_to_text', broken.speech_to_text):
            first = await one_auto(self.ws, {'id': '01'}, ROOT/'artifacts/synthetic', self.out/'audio')
            self.assertFalse(first['success'])
            self.assertIn('injected_asr_timeout', first['error'])
            second = await one_auto(self.ws, {'id': '02'}, ROOT/'artifacts/synthetic', self.out/'audio')
            self.assertTrue(second['success'], second)
        await self.manual()

    async def test_vad_failure_recovers_in_manual_mode(self):
        with patch.object(self.server._vad, 'is_vad', return_value=None):
            await self.ws.send(json.dumps({'type': 'listen', 'state': 'start', 'mode': 'auto'}))
            packets=[]
            OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(bytes(1920),True,packets.append)
            await self.ws.send(packets[0])
            self.assertEqual(json.loads(await self.ws.recv())['error'], 'vad_failed')
            self.assertEqual(json.loads(await self.ws.recv())['state'], 'stop')
        await self.manual()

    async def test_continuous_voice_is_bounded(self):
        with patch.object(self.server._vad, 'is_vad', return_value=True):
            await self.ws.send(json.dumps({'type': 'listen', 'state': 'start', 'mode': 'auto'}))
            packets=[]
            OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(bytes(1920*252),True,packets.append)
            for packet in packets: await self.ws.send(packet)
            self.assertEqual(json.loads(await self.ws.recv())['error'], 'audio_too_long_or_packet_too_large')
            self.assertEqual(json.loads(await self.ws.recv())['state'], 'stop')
        await self.manual()


if __name__ == '__main__': unittest.main()
