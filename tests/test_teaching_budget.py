"""Synthetic long-turn regressions using the real Ark payload and local caller.

No household text, microphone, live provider or paid network request is used.
"""
import asyncio
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

import tests.test_doubao_cloud as cloud
from cloud_report import ERROR_MESSAGES, diagnostics_report, monitor_report
from cloud_service import CloudPolicy
from core.providers.llm.doubao_english.doubao_english import (
    TEACHING_PROMPT, CHAT_PROMPT, CLARIFICATION_PROMPT, teaching_response_format,
)


def request_bytes(request):
    return (sum(len(message['content'].encode('utf8')) for message in request['messages'])
            + len(json.dumps({'response_format': request['response_format']},
                ensure_ascii=False, separators=(',', ':')).encode('utf8')) + 256)


class TeachingBudgetTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = cloud.ContractTests.asyncTearDown
    tts_response = cloud.ContractTests.tts_response

    async def asyncSetUp(self):
        self.enterContext(patch('httpx.HTTPTransport.handle_request',
            side_effect=AssertionError('External HTTP disabled in budget tests')))
        self.enterContext(patch('httpx.AsyncHTTPTransport.handle_async_request',
            new=AsyncMock(side_effect=AssertionError('External HTTP disabled in budget tests'))))
        self.reply_queue = []
        await cloud.ContractTests.asyncSetUp(self)

    def llm_response(self, request):
        if self.reply_queue:
            self.teaching_reply = self.reply_queue.pop(0)
        return cloud.ContractTests.llm_response(self, request)

    async def test_full_400_character_chinese_utterance_is_not_cut(self):
        # Representative accepted length including multibyte characters. The
        # repeated fixture is synthetic, not a recording or a semantic claim.
        text = ('明天我想带着蓝色水壶和一本故事书去公园，然后回家画画。' * 20)[:400]
        self.assertEqual(len(text), 400)
        for mode in ('express', 'chat'):
            with self.subTest(mode=mode):
                self.teaching_reply = {'intent': mode, 'english': 'I want to go to the park tomorrow.'}
                before = len(self.requests)
                await asyncio.to_thread(self.llm.teaching_response, text, 'fixture', mode=mode)
                self.assertEqual(len(self.requests), before + 1)
                request = self.requests[-1][1]
                self.assertEqual(json.loads(request['messages'][1]['content'])['transcript'], text)
                self.assertLessEqual(request_bytes(request), 6144)
                self.assertEqual(self.llm.client.max_retries, 0)

    async def test_long_followup_preserves_two_clarifications_correction_and_previous(self):
        text = ('我说的是桌子上的蓝色盒子，盖子旁边有一张黄色贴纸。我想取出里面的画笔，'
                '但是盖子太紧了，轻轻向上拉也打不开。我不是要把盒子扔掉，只想请妈妈帮我打开，'
                '打开以后还要把盖子放回去。')
        previous = 'The blue box on the desk is difficult to open.'
        clarification = {'turns': [
            {'child': '我想把里面的画笔拿出来，可是那个一直打不开。', 'assistant': '你说的是哪个东西？'},
            {'child': '桌上贴着黄色贴纸的蓝色盒子，里面装着画笔。', 'assistant': '盒盖遇到什么问题？'},
        ]}
        correction = {'transcript': '这个盒子我打不开，想请妈妈帮我。',
                      'rejected_english': 'I want to throw this box away.'}
        history = [{'child': '早' * 400, 'assistant': 'A' * 400},
                   {'child': '旧' * 400, 'assistant': 'B' * 400},
                   {'child': '先前的问题', 'assistant': 'An earlier question.'},
                   {'child': '最近的问题', 'assistant': 'A recent question.'}]
        original = copy.deepcopy((clarification, correction, history))
        for mode in ('express', 'chat'):
            with self.subTest(mode=mode):
                self.teaching_reply = {'intent': 'repair', 'english': 'Mom, can you help me open this box?'}
                await asyncio.to_thread(self.llm.teaching_response, text, 'fixture', previous, correction,
                    mode=mode, clarification_context=clarification, dialogue_context=history)
                request = self.requests[-1][1]
                payload = json.loads(request['messages'][1]['content'])
                self.assertEqual(payload['transcript'], text)
                self.assertEqual(payload['previous_english'], previous)
                self.assertEqual(payload['clarification_context'], clarification)
                self.assertEqual(payload['correction_context'], correction)
                self.assertLess(len(payload['dialogue_context']), len(history))
                kept = payload['dialogue_context']
                self.assertEqual(kept, history[-len(kept):] if kept else [])
                self.assertEqual((clarification, correction, history), original)
                self.assertLessEqual(request_bytes(request), 6144)

    async def test_genuinely_oversized_context_fails_before_sdk_without_truncation(self):
        clarification = {'turns': [{'child': '中' * 400, 'assistant': '问' * 400}] * 2}
        correction = {'transcript': '句' * 400, 'rejected_english': 'X' * 400}
        original = copy.deepcopy((clarification, correction))
        with self.assertRaisesRegex(ValueError, '^english_input_exceeds_budget_bound$'):
            await asyncio.to_thread(self.llm.teaching_response, '原' * 400, 'fixture',
                'Y' * 400, correction, clarification_context=clarification)
        self.assertEqual(self.requests, [])
        self.assertEqual((clarification, correction), original)

    async def test_exact_limit_still_counts_schema_with_valid_clarification(self):
        for mode, prompt in (('express', TEACHING_PROMPT), ('chat', CHAT_PROMPT)):
            with self.subTest(mode=mode):
                clarification = {'turns': [{'child': '那个怎么弄？', 'assistant': '你说的是哪个东西？'}]}
                self.teaching_reply = {'intent': mode, 'english': 'The blue box.'}
                payload = {'transcript': '', 'previous_english': None, 'correction_context': None,
                           'mode': mode, 'dialogue_context': [], 'clarification_context': clarification}
                format_bytes = len(json.dumps({'response_format': teaching_response_format(mode)},
                    ensure_ascii=False, separators=(',', ':')).encode('utf8'))
                available = 6144 - 256 - format_bytes - len((prompt + CLARIFICATION_PROMPT).encode('utf8'))
                available -= len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf8'))
                self.assertGreater(available, 0)
                await asyncio.to_thread(self.llm.teaching_response, 'x' * available, 'fixture', mode=mode,
                    clarification_context=clarification)
                self.assertEqual(request_bytes(self.requests[-1][1]), 6144)
                before = len(self.requests)
                with self.assertRaisesRegex(ValueError, '^english_input_exceeds_budget_bound$'):
                    await asyncio.to_thread(self.llm.teaching_response, 'x' * (available + 1), 'fixture',
                        mode=mode, clarification_context=clarification)
                self.assertEqual(len(self.requests), before)

    async def test_real_ws_caller_retains_two_confirmed_questions_for_long_answer(self):
        turns = [
            '那个怎么弄？',
            '桌子上那个蓝色的盒子，盒盖边上有一张黄色贴纸，里面装着我画画要用的画笔。',
            '我想把里面的画笔拿出来，可是盖子太紧了，一直打不开。我不是要把盒子扔掉，'
            '而是想请妈妈帮我打开。里面的画笔都是好的，打开以后我还要用它们画一张公园的画。',
        ]
        questions = ['你说的是哪个东西？', '盒盖遇到什么问题？']
        self.reply_queue = [
            {'intent': 'clarify', 'english': questions[0]},
            {'intent': 'clarify', 'english': questions[1]},
            {'intent': 'express', 'english': 'Mom, can you help me open this box so I can get my crayons?'},
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
                            results.append(result)
                            self.assertTrue(result['success'], result)
                            for name, source in (('playback_started', 'codec_output_returned'),
                                                  ('playback_completed', 'codec_queue_drained')):
                                await ws.send(json.dumps({'type': 'mvp_playback', 'request_id': result['request_id'],
                                                          'event': name, 'source': source}))
                            await ws.send('{"type":"ping"}')
                            self.assertEqual(json.loads(await ws.recv()), {'type': 'pong'})
                    finally:
                        await ws.close()
            events = [json.loads(line) for line in (folder / 'events.jsonl').read_text().splitlines()]
        requests = [request for kind, request in self.requests if kind == 'llm']
        self.assertEqual(len(requests), 3)
        self.assertFalse(self.reply_queue)
        payload = json.loads(requests[-1]['messages'][1]['content'])
        self.assertEqual(payload['transcript'], turns[-1])
        self.assertEqual(payload['clarification_context'], {'turns': [
            {'child': turns[0], 'assistant': questions[0]},
            {'child': turns[1], 'assistant': questions[1]},
        ]})
        self.assertTrue(all(request_bytes(request) <= 6144 for request in requests))
        self.assertEqual(sum(row['event'] == 'playback_confirmed' for row in events), 3)


class BudgetErrorReportingTests(unittest.TestCase):
    def test_input_bound_error_has_safe_actionable_report_and_recovery_code(self):
        raw = 'ValueError: english_input_exceeds_budget_bound: Bearer fixture-secret'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'usage.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE events (time REAL, request_id TEXT, device_id TEXT, event TEXT, data TEXT)')
                for event, data in [('error', {'error': raw}), ('request_complete', {'success': False})]:
                    db.execute('INSERT INTO events VALUES (?,?,?,?,?)',
                               (time.time(), 'fixture', 'fixture-device', event, json.dumps(data)))
            monitor = monitor_report(path)['requests'][0]
            diagnostic = diagnostics_report(path)['recent'][0]
            # error_response is pure and must not read or mutate cloud state.
            recovery = CloudPolicy.error_response(None, raw)
            self.assertEqual(monitor['error_code'], 'input_too_long')
            self.assertEqual(diagnostic['error_code'], 'input_too_long')
            self.assertEqual(monitor['error'], ERROR_MESSAGES['input_too_long'])
            self.assertIn('短句', monitor['error'])
            self.assertEqual(recovery, {'error_code': 'input_too_long', 'retry_after_ms': 3000})
            serialized = json.dumps((monitor, diagnostic, recovery), ensure_ascii=False)
            self.assertNotIn(raw, serialized)
            self.assertNotIn('fixture-secret', serialized)
            self.assertNotIn('Bearer', serialized)


if __name__ == '__main__':
    unittest.main()
