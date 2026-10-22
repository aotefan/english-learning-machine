"""Reject invalid PCM before ASR, then recover on the same upstream connection."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

import test_vad
from protocol_benchmark import OpusEncoderUtils
from core.utils.input_audio import rejection_reason
from core.providers.local_models import LocalModels


class InputGateTests(unittest.TestCase):
    def test_threshold_and_int16_negative_peak(self):
        # Preserve legacy boundary exactly, including valid quiet/short words.
        self.assertEqual(rejection_reason(np.full(1600, 65, np.int16)), 'silent_or_too_quiet')
        self.assertIsNone(rejection_reason(np.full(1600, 66, np.int16)))
        self.assertIsNone(rejection_reason(np.full(1600, -32768, np.int16)))
        self.assertEqual(rejection_reason(np.full(1599, 1000, np.int16)), 'too_short')

    def test_direct_worker_keeps_same_guard_without_loading_whisper(self):
        with patch.dict('sys.modules', {'mlx_whisper': None}):
            with self.assertRaisesRegex(ValueError, 'empty_or_silent_audio'):
                LocalModels().run('asr', bytes(32000), {'engine':'mlx_whisper', 'model_path':'unused'})


class GateProtocolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_vad.AutoTests.asyncSetUp
    asyncTearDown = test_vad.AutoTests.asyncTearDown
    events = test_vad.AutoTests.events
    manual = test_vad.AutoTests.manual

    async def test_manual_rejection_skips_models_and_next_sentence_works(self):
        for pcm, reason in [(bytes(32000), 'silent_or_too_quiet'),
                            (np.full(16000, 20, np.int16).tobytes(), 'silent_or_too_quiet'),
                            (np.full(960, 1000, np.int16).tobytes(), 'too_short')]:
            with self.subTest(reason=reason):
                await self.reject(pcm, reason, 'manual')
                await self.manual()

    async def test_auto_false_trigger_recovers_then_real_vad_and_manual_work(self):
        # Simulate a VAD false trigger, not a claim about real-world noise accuracy.
        def trigger(conn, pcm):
            conn.client_voice_stop = True
            return True
        with patch.object(self.server._vad, 'is_vad', side_effect=trigger):
            await self.reject(bytes(1920), 'too_short', 'auto')
        from vad_benchmark import one_auto
        from test_protocol import ROOT
        result = await one_auto(self.ws, {'id':'01'}, ROOT/'artifacts/synthetic', self.out/'audio')
        self.assertTrue(result['success'], result)
        await self.manual()

    async def reject(self, pcm, reason, mode):
        offset = len(self.events())
        packets = []
        OpusEncoderUtils(16000, 1, 60).encode_pcm_to_opus_stream(pcm, True, packets.append)
        with patch.object(self.server._asr, 'speech_to_text', new_callable=AsyncMock) as asr:
            await self.ws.send(json.dumps({'type':'listen', 'state':'start', 'mode':mode}))
            for packet in packets:
                await self.ws.send(packet)
            if mode == 'manual':
                await self.ws.send(json.dumps({'type':'listen', 'state':'stop'}))
            messages = []
            while True:
                message = await asyncio.wait_for(self.ws.recv(), 5)
                self.assertIsInstance(message, str, 'Rejected input must not return audio')
                row = json.loads(message)
                messages.append(row)
                if row.get('type') == 'tts' and row.get('state') == 'stop':
                    break
            async def terminal():
                while not any(e['event']=='request_complete' for e in self.events()[offset:]):
                    await asyncio.sleep(.01)
            await asyncio.wait_for(terminal(), 3)
            asr.assert_not_awaited()
        events = self.events()[offset:]
        rejected = [e for e in events if e['event']=='audio_rejected']
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]['reason'], reason)
        errors = [m for m in messages if m.get('type')=='error']
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]['request_id'], rejected[0]['request_id'])
        completed = [e for e in events if e['event']=='request_complete']
        self.assertEqual(len(completed), 1)
        self.assertFalse(completed[0]['success'])
        for stage in ('asr_ms', 'english_ms', 'tts_ms', 'return_ms'):
            self.assertIsNone(completed[0][stage])
        self.assertFalse(any(e['event'] in ('asr_result','english_result','tts_generated','audio_sent') for e in events))


if __name__ == '__main__':
    unittest.main()
