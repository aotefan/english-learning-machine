"""A narrow local incomplete-input/output intersection, with mocked suppliers."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

import tests.test_doubao_cloud as cloud
from core.providers.llm.doubao_english.doubao_english import (
    INCOMPLETE_UTTERANCE_QUESTION, paired_incomplete_utterance,
)


class UtteranceCompletenessTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = cloud.ContractTests.asyncTearDown
    tts_response = cloud.ContractTests.tts_response

    async def asyncSetUp(self):
        self.enterContext(patch('httpx.HTTPTransport.handle_request',
            side_effect=AssertionError('External HTTP disabled in completeness tests')))
        self.enterContext(patch('httpx.AsyncHTTPTransport.handle_async_request',
            new=AsyncMock(side_effect=AssertionError('External HTTP disabled in completeness tests'))))
        self.reply_queue = []
        await cloud.ContractTests.asyncSetUp(self)

    def llm_response(self, request):
        if self.reply_queue:
            self.teaching_reply = self.reply_queue.pop(0)
        return cloud.ContractTests.llm_response(self, request)

    async def test_paired_unfinished_outputs_clarify_once_and_preserve_usage(self):
        cases = (
            ('红色的纸和蓝色的纸是不是', 'Are red paper and blue paper...'),
            ('红色的纸和蓝色的纸是否', 'Are red paper and blue paper…'),
            ('红色的纸和蓝色的纸是不是', 'Were the red paper and the blue paper...'),
            ('放在桌上的这个盒子是', 'The red box is.'),
            ('铅笔和橡皮是', 'A pencil and an eraser are.'),
            ('铅笔和橡皮是', 'Pencils and erasers are.'),
        )
        for mode, intent in (('express', 'express'), ('chat', 'chat'), ('express', 'repair')):
            for text, reply in cases:
                with self.subTest(mode=mode, intent=intent, text=text):
                    self.teaching_reply = {'intent': intent, 'english': reply}
                    before = len(self.requests)
                    actual, spoken, usage = await asyncio.to_thread(
                        self.llm.teaching_response, text, 'fixture', mode=mode)
                    self.assertEqual((actual, spoken), ('clarify', INCOMPLETE_UTTERANCE_QUESTION))
                    self.assertEqual((usage['input_tokens'], usage['output_tokens']), (120, 15))
                    self.assertEqual(len(self.requests), before + 1)
                    request = self.requests[-1][1]
                    self.assertEqual(json.loads(request['messages'][1]['content'])['transcript'], text)
                    self.assertEqual(request['max_tokens'], 256)
                    self.assertEqual(self.llm.client.max_retries, 0)

    async def test_complete_contextual_quoted_and_reported_cases_are_not_rewritten(self):
        cases = (
            ('是', 'It is...'),
            ('是不是？', 'Is it...'),
            ('放在桌上的这个盒子是', "That's what it is."),
            ('放在桌上的这个盒子是', 'It is what it is...'),
            ('放在桌上的这个盒子是', "I don't know..."),
            ('放在桌上的这个盒子是', "I'm not sure…"),
            ('放在桌上的这个盒子是', 'I think it is...'),
            ('放在桌上的这个盒子是', "That's all it is..."),
            ('放在桌上的这个盒子是', 'I wonder who she is.'),
            ('放在桌上的这个盒子是', 'The box is red...'),
            ('放在桌上的这个盒子是', 'It is...'),
            ('放在桌上的这个盒子是', 'The way it is...'),
            ('铅笔和橡皮是', 'They and we are.'),
            ('铅笔和橡皮是', 'What pencils and erasers are.'),
            ('铅笔和橡皮是', 'Pencils and erasers are useful...'),
            ('铅笔和橡皮是', "Pencils and erasers aren't..."),
            ('这种纸我想要', 'I want this kind of paper...'),
            ('这个办法我想要', 'I want this approach...'),
            ('我明天放学以后想要', 'Tomorrow after school, I want to...'),
            ('门口那两个小朋友想要', 'The two children want'),
            ('猫和狗不是', "No, cats and dogs aren't..."),
            ('红色的纸和蓝色的纸不是', 'The red paper is...'),
            ('让我先仔细想一想', 'Let me think...'),
            ('请大家认真地想', 'Think carefully...'),
            ('红色的纸和蓝色的纸是不是一样厚吗？', 'Are they...'),
            ('这就是红色的纸，是不是？', 'It is...'),
            ('妈妈说红色的纸和蓝色的纸是不是', 'Mom said red paper and blue paper...'),
            ('老师问红色的纸和蓝色的纸是不是', 'The teacher asked whether they...'),
            ('“红色的纸和蓝色的纸是不是”', 'Are red paper and blue paper...'),
            ('红色的纸和蓝色的纸是不是', 'Are red paper and blue paper the same?'),
            ('红色的纸和蓝色的纸是不是', 'Are red paper and blue paper the same...'),
            # Outside this high-confidence cue: no claim to parse all grammar.
            ('红色的纸和蓝色的纸是不是', 'Are red paper and blue paper'),
            ('放在桌上的这个盒子是', 'The box on the desk is.'),
        )
        for text, reply in cases:
            with self.subTest(text=text, reply=reply):
                self.assertFalse(paired_incomplete_utterance(text, reply))
                self.teaching_reply = {'intent': 'express', 'english': reply}
                actual = await asyncio.to_thread(self.llm.teaching_response, text, 'fixture')
                self.assertEqual(actual[:2], ('express', reply))

    async def test_existing_validation_still_rejects_bad_outputs_before_guard(self):
        for reply in ('这是 Are they...', 'A' * 401 + '...', ''):
            self.teaching_reply = {'intent': 'express', 'english': reply}
            before = len(self.requests)
            with self.assertRaisesRegex(RuntimeError, '^doubao_english_failed:ValueError:invalid_english_'):
                await asyncio.to_thread(self.llm.teaching_response,
                    '红色的纸和蓝色的纸是不是', 'fixture')
            self.assertEqual(len(self.requests), before + 1)

    async def test_real_ws_guarded_question_supplies_followup_context_then_clears(self):
        turns = ['红色的纸和蓝色的纸是不是', '一样厚', '我想画画。']
        expected = 'Are the red paper and blue paper the same thickness?'
        self.reply_queue = [
            {'intent': 'express', 'english': 'Are red paper and blue paper...'},
            {'intent': 'express', 'english': expected},
            {'intent': 'express', 'english': 'I want to draw.'},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with wave.open(str(folder / 'sample.wav'), 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                wav.writeframes(cloud.PCM)
            config = cloud.load_cloud_config(require_credentials=False)
            config['selected_module'] = {}
            config['english_mvp']['log_path'] = str(folder / 'events.jsonl')
            server = cloud.WebSocketServer(config)
            server._llm = self.llm
            server._asr = SimpleNamespace(close=AsyncMock(),
                speech_to_text=AsyncMock(side_effect=[(text, None) for text in turns]))
            with patch('core.connection.initialize_tts', lambda _: self.tts):
                async with cloud.websockets.serve(server._handle_connection, '127.0.0.1', 0) as listener:
                    ws = await cloud.connect(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/',
                                             features={'mvp_playback_completed': True})
                    results = []
                    try:
                        for _ in turns:
                            result = await cloud.one(ws, {'id': 'sample'}, folder, False, save_audio=False)
                            self.assertTrue(result['success'], result)
                            results.append(result)
                            for name, source in (('playback_started', 'codec_output_returned'),
                                                  ('playback_completed', 'codec_queue_drained')):
                                await ws.send(json.dumps({'type': 'mvp_playback', 'request_id': result['request_id'],
                                                          'event': name, 'source': source}))
                            await ws.send('{"type":"ping"}')
                            self.assertEqual(json.loads(await ws.recv()), {'type': 'pong'})
                    finally:
                        await ws.close()
            events = [json.loads(line) for line in (folder / 'events.jsonl').read_text().splitlines()]
        self.assertEqual([row['english'] for row in results],
                         [INCOMPLETE_UTTERANCE_QUESTION, expected, 'I want to draw.'])
        requests = [request for kind, request in self.requests if kind == 'llm']
        self.assertEqual(len(requests), 3)
        payload = json.loads(requests[1]['messages'][1]['content'])
        self.assertEqual(payload['clarification_context'], {'turns': [
            {'child': turns[0], 'assistant': INCOMPLETE_UTTERANCE_QUESTION}]})
        self.assertIsNone(json.loads(requests[2]['messages'][1]['content'])['clarification_context'])
        self.assertEqual([row['intent'] for row in events if row['event'] == 'intent_classified'],
                         ['clarify', 'express', 'express'])
        self.assertFalse(self.reply_queue)


if __name__ == '__main__':
    unittest.main()
