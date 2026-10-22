"""Unified practice-mode routing over local WS/Opus and mocked vendor SDK replies.

No microphones, external network requests, or claims about model teaching quality.
"""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import tests.test_doubao_cloud as cloud
from core.handle.englishMvp import (
    handle_message, confirm_playback, is_exact_request, EXACT_CHAT_REQUESTS, EXACT_EXPRESS_REQUESTS,
)


class PracticeModeTests(unittest.IsolatedAsyncioTestCase):
    # Share only the fixture methods, without inheriting/re-running its test suite.
    asyncTearDown = cloud.ContractTests.asyncTearDown
    llm_response = cloud.ContractTests.llm_response
    tts_response = cloud.ContractTests.tts_response
    async def asyncSetUp(self):
        self.enterContext(patch('httpx.HTTPTransport.handle_request',
                                side_effect=AssertionError('Real HTTP is disabled in practice tests')))
        self.enterContext(patch('httpx.AsyncHTTPTransport.handle_async_request',
                                new=AsyncMock(side_effect=AssertionError('Real HTTP is disabled in practice tests'))))
        await cloud.ContractTests.asyncSetUp(self)

    async def _run_turns(self, *args, **kwargs):
        # Closing a WS releases its TTS client. Reuse the fixture with a fresh
        # mock transport so a later connection cannot create a real HTTP client.
        if self.tts.client is None or self.tts.client.is_closed:
            self.tts.client = cloud.httpx.AsyncClient(transport=cloud.httpx.MockTransport(self.tts_response))
        return await cloud.ContractTests._run_clarification_turns(self, *args, **kwargs)

    def test_practice_commands_and_old_aliases_require_complete_unquoted_requests(self):
        commands = ('陪练模式', '进入陪练模式', '开始陪练', '开始陪练模式',
            '开始吃饭陪练', '进入吃饭场景。', '开始出门陪练', '进入出门场景！',
            '开始收拾陪练', '进入收拾场景', '开始收拾物品陪练', '进入收拾物品场景',
            '练习吃饭英语', '练习出门英语', '练习收拾东西的英语')
        for text in commands:
            with self.subTest(text=text):
                self.assertTrue(is_exact_request(text, EXACT_CHAT_REQUESTS))
        for text in ('结束陪练', '退出陪练', '退出陪练模式', '表达模式', '生活表达模式',
                     '关闭陪练模式', '关掉陪练模式',
                     '结束场景陪练', '退出场景陪练', '只告诉我英文。', '只给我英文表达！'):
            with self.subTest(exit=text):
                self.assertTrue(is_exact_request(text, EXACT_EXPRESS_REQUESTS))
        for text in ('我想吃饭', '不要开始吃饭陪练', '他说开始出门陪练',
                     '不要关闭陪练模式', '关闭陪练模式是什么意思', '“关闭陪练模式”',
                     '妈妈说关掉陪练模式',
                     '开始吃饭陪练是什么意思', '“开始吃饭陪练”', '"进入收拾场景"',
                     "'开始出门陪练'", '不要进入陪练模式', '陪练模式是什么意思',
                     '“陪练模式”', '他说退出陪练', '“只告诉我英文”', '"只给我英文表达"',
                     '不要只告诉我英文', '不要只给我英文表达', '他说只告诉我英文',
                     '只告诉我英文是什么意思', '只给我英文表达是什么意思',
                     '只告诉我英文：我想喝水', '不要解释', '只说英文'):
            with self.subTest(not_command=text):
                self.assertFalse(is_exact_request(text, EXACT_CHAT_REQUESTS))
                self.assertFalse(is_exact_request(text, EXACT_EXPRESS_REQUESTS))

    async def test_practice_sdk_uses_chat_without_any_scene_selection(self):
        self.teaching_reply = {'intent': 'chat', 'english': 'What would you like?'}
        actual = await asyncio.to_thread(self.llm.teaching_response, '我想喝水', 'request', mode='chat')
        self.assertEqual(actual[:2], ('chat', self.teaching_reply['english']))
        context = json.loads(self.requests[-1][1]['messages'][1]['content'])
        self.assertNotIn('scene', context)
        self.assertEqual(context['mode'], 'chat')
        calls = len(self.requests)
        for invalid in ('unknown', '', 0, True, [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.llm.teaching_response('你好', 'request', mode=invalid)
        self.assertEqual(len(self.requests), calls)

    async def test_practice_sdk_budget_trims_history_and_rejects_oversized_input(self):
        self.teaching_reply = {'intent': 'chat', 'english': 'What would you like?'}
        long_turn = {'child': '中' * 400, 'assistant': 'A' * 400}
        await asyncio.to_thread(self.llm.teaching_response, '我想喝水', 'request',
                                mode='chat', dialogue_context=[long_turn] * 4)
        request = self.requests[-1][1]
        context = json.loads(request['messages'][1]['content'])
        self.assertNotIn('scene', context)
        self.assertLess(len(context['dialogue_context']), 4)
        self.assertLessEqual(sum(len(row['content'].encode('utf-8'))
                                 for row in request['messages']) + 256, 6144)
        calls = len(self.requests)
        with self.assertRaisesRegex(ValueError, 'english_input_exceeds_budget_bound'):
            self.llm.teaching_response('句' * 2000, 'request', mode='chat')
        self.assertEqual(len(self.requests), calls)

    async def test_direct_help_excludes_unrelated_conversation(self):
        self.direct_help_reply = {'english': '就是请别人再说一次。'}
        help_context = {'child': 'Say again 是什么意思？', 'assistant': '就是请别人再说一次。',
                        'replay_previous': False}
        actual = await asyncio.to_thread(self.llm.teaching_response, '再简单一点', 'request',
            'What would you like to drink?', mode='chat', help_context=help_context,
            dialogue_context=[{'child': '我想喝果汁', 'assistant': 'What would you like to drink?'}])
        self.assertEqual(actual[:2], ('explain_only', self.direct_help_reply['english']))
        payload = json.loads(self.requests[-1][1]['messages'][1]['content'])
        self.assertEqual(payload, {'source_kind': 'explanation', 'source_text': help_context['assistant'],
                                  'original_question': help_context['child'], 'request': '再简单一点'})

    async def test_teaching_validation_errors_include_only_safe_failure_codes(self):
        cases = [
            ({'intent': 'unknown', 'english': 'Hello.'}, 'stop', 'invalid_teaching_response'),
            ({'intent': 'chat', 'english': 'Hello.', 'extra': 'test-secret 家庭私密内容'},
             'stop', 'invalid_teaching_response'),
            ({'intent': 'chat', 'english': ''}, 'stop', 'invalid_english_empty'),
            ({'intent': 'chat', 'english': 'A' * 401}, 'stop', 'invalid_english_length'),
            ({'intent': 'chat', 'english': 'I want 玩积木。'}, 'stop', 'invalid_english_language'),
            ({'intent': 'chat', 'english': 'Hello.'}, 'length', 'incomplete_teaching_response'),
        ]
        for payload, finish_reason, code in cases:
            with self.subTest(code=code, finish_reason=finish_reason):
                response = SimpleNamespace(usage=None, id="fixture", choices=[SimpleNamespace(
                    finish_reason=finish_reason, message=SimpleNamespace(content=json.dumps(payload)))])
                with patch.object(self.llm.client.chat.completions, 'create', return_value=response):
                    with self.assertRaises(RuntimeError) as caught:
                        await asyncio.to_thread(self.llm.teaching_response, '我想玩积木', 'request',
                                                mode='chat')
                suffix = ':finish_reason=length' if finish_reason == 'length' else ''
                self.assertEqual(str(caught.exception), f'doubao_english_failed:ValueError:{code}{suffix}')
                self.assertNotIn('test-secret', str(caught.exception))
                self.assertNotIn('家庭私密内容', str(caught.exception))

    async def test_unknown_sdk_value_error_and_invalid_json_never_expose_raw_content(self):
        for detail in ('test-secret 家庭私密内容', 'invalid_english_length: test-secret 家庭私密内容'):
            with self.subTest(detail=detail):
                with patch.object(self.llm.client.chat.completions, 'create', side_effect=ValueError(detail)):
                    with self.assertRaises(RuntimeError) as caught:
                        await asyncio.to_thread(self.llm.teaching_response, '我想玩积木', 'request',
                                                mode='chat')
                self.assertEqual(str(caught.exception), 'doubao_english_failed:ValueError')
        response = SimpleNamespace(usage=None, id="fixture", choices=[SimpleNamespace(finish_reason='stop',
            message=SimpleNamespace(content='{"test-secret": 家庭私密内容'))])
        with patch.object(self.llm.client.chat.completions, 'create', return_value=response):
            with self.assertRaises(RuntimeError) as caught:
                await asyncio.to_thread(self.llm.teaching_response, '我想玩积木', 'request',
                                        mode='chat')
        self.assertEqual(str(caught.exception), 'doubao_english_failed:JSONDecodeError:invalid_json')
        self.assertNotIn('test-secret', str(caught.exception))
        self.assertNotIn('家庭私密内容', str(caught.exception))

    async def test_practice_opener_enters_chat_without_adding_a_dialogue_turn(self):
        command = '陪练模式'
        results, calls, events = await self._run_turns(
            [command, '我想喝水'], [('chat', 'What would you like to drink?')], return_events=True)
        self.assertTrue(all(result['success'] for result in results), results)
        self.assertEqual(results[0]['english'], "Let's chat in English.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['mode'], 'chat')
        self.assertNotIn('scene', calls[0])
        self.assertEqual(calls[0]['dialogue_context'], [])
        self.assertEqual([row['intent'] for row in events if row['event'] == 'intent_classified'],
                         ['mode', 'chat'])

    async def test_practice_mode_switch_is_immediate_even_if_opener_is_unfinished(self):
        results, calls = await self._run_turns(
            ['开始陪练', '我的鞋子在哪里'], [('chat', 'Where did you last see them?')],
            completions=[False, True])
        self.assertTrue(all(result['success'] for result in results), results)
        self.assertEqual(calls[0]['mode'], 'chat')
        self.assertNotIn('scene', calls[0])
        self.assertFalse(calls[0].get('dialogue_context'))

    async def test_only_confirmed_chat_replies_enter_conversation_history(self):
        heard = 'What would you like to drink?'
        unheard = 'Would you like some juice?'
        results, calls = await self._run_turns(
            ['陪练模式', '我想喝水', '我想喝果汁', '我饿了'],
            [('chat', heard), ('chat', unheard), ('chat', 'What would you like to eat?')],
            completions=[True, True, False, True])
        self.assertTrue(all(result['success'] for result in results), results)
        self.assertEqual(calls[0]['dialogue_context'], [])
        expected = [{'child': '我想喝水', 'assistant': heard}]
        self.assertEqual(calls[1]['dialogue_context'], expected)
        self.assertEqual(calls[2]['dialogue_context'], expected)
        self.assertEqual(calls[2]['previous'], heard)

    async def test_quoted_and_negated_commands_reach_model_without_switching_mode(self):
        results, calls, events = await self._run_turns(
            ['“陪练模式”是什么意思', '不要进入陪练模式', '我想吃饭'],
            [('express', 'What does practice mode mean?'), ('express', "Don't start practice mode."),
             ('express', "I'd like to eat.")], return_events=True)
        self.assertTrue(all(result['success'] for result in results), results)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call.get('mode', 'express') == 'express' for call in calls))
        self.assertEqual([row['intent'] for row in events if row['event'] == 'intent_classified'],
                         ['express', 'express', 'express'])

    async def test_mode_switch_clears_correction_before_unfinished_or_failed_opener(self):
        import core.handle.englishMvp as mvp
        for command, mode, fail_opener in [('陪练模式', 'chat', False), ('表达模式', 'express', True)]:
            with self.subTest(command=command, failed=fail_opener):
                before, after = [], []
                original_process = mvp.process
                original_tts = self.tts.text_to_speak
                opener = "Let's chat in English." if mode == 'chat' else 'Back to everyday English.'

                async def observe_process(conn, frames):
                    before.append(getattr(conn, 'mvp_correction_context', None))
                    await original_process(conn, frames)
                    after.append(getattr(conn, 'mvp_correction_context', None))

                async def synthesize(text, *args, **kwargs):
                    if fail_opener and text == opener:
                        raise RuntimeError('local_mode_tts_failure')
                    return await original_tts(text, *args, **kwargs)

                with patch.object(mvp, 'process', side_effect=observe_process), \
                     patch.object(self.tts, 'text_to_speak', side_effect=synthesize):
                    results, calls = await self._run_turns(
                        ['我的帽子在哪里', '不是这个意思', command, '我在找蓝色书包'],
                        [('express', 'Where is my red hat?'), (mode, "I'm looking for my blue schoolbag.")],
                        completions=[True, True, False, True])
                self.assertEqual([result['success'] for result in results], [True, True, not fail_opener, True])
                self.assertEqual(after[1], {'transcript': '我的帽子在哪里', 'rejected_english': 'Where is my red hat?'})
                self.assertIsNone(after[2])
                self.assertIsNone(before[3])
                self.assertEqual(calls[-1].get('mode', 'express'), mode)

    async def test_expression_only_request_leaves_chat_and_help_until_practice_is_requested(self):
        for command in ('只告诉我英文', '只给我英文表达'):
            with self.subTest(command=command):
                results, calls, events = await self._run_turns(
                    ['陪练模式', '我想喝水', '什么意思', command, '我饿了', '陪练模式', '我想看书'],
                    [('chat', 'What would you like to drink?'), ('explain', '这是问你想喝什么。'),
                     ('express', "I'm hungry."), ('chat', 'What kind of books do you like?')],
                    return_events=True)
                self.assertTrue(all(result['success'] for result in results), results)
                self.assertEqual(results[3]['english'], 'Back to everyday English.')
                self.assertEqual(results[4]['english'], "I'm hungry.")
                self.assertEqual([call['text'] for call in calls],
                                 ['我想喝水', '什么意思', '我饿了', '我想看书'])
                self.assertEqual([call.get('mode', 'express') for call in calls],
                                 ['chat', 'chat', 'express', 'chat'])
                self.assertFalse(calls[2].get('dialogue_context'))
                self.assertFalse(calls[2].get('help_context'))
                self.assertFalse(calls[2].get('clarification_context'))
                self.assertEqual(calls[3]['dialogue_context'], [])
                self.assertEqual([row['intent'] for row in events if row['event'] == 'intent_classified'],
                                 ['mode', 'chat', 'explain', 'mode', 'express', 'mode', 'chat'])
                self.assertEqual([row['mode'] for row in events if row['event'] == 'teaching_mode_changed'],
                                 ['chat', 'express', 'chat'])

    async def test_quoted_negated_and_meaning_requests_do_not_exit_practice(self):
        utterances = ['“只告诉我英文”', '不要只给我英文表达', '只告诉我英文是什么意思']
        results, calls, events = await self._run_turns(
            ['陪练模式', *utterances],
            [('chat', 'You said, "Just tell me the English."'),
             ('chat', 'We can keep chatting.'), ('explain_only', '就是只想知道对应的英文表达。')],
            return_events=True)
        self.assertTrue(all(result['success'] for result in results), results)
        self.assertEqual([call['text'] for call in calls], utterances)
        self.assertTrue(all(call['mode'] == 'chat' for call in calls))
        self.assertEqual([row['mode'] for row in events if row['event'] == 'teaching_mode_changed'],
                         ['chat'])

    async def test_old_scene_commands_reenter_same_practice_mode_without_topic_binding(self):
        results, calls = await self._run_turns(
            ['开始吃饭陪练', '我想喝水', '开始出门陪练', '我的鞋呢', '开始收拾陪练', '玩具在哪里'],
            [('chat', 'What would you like to drink?'), ('chat', 'Where are your shoes?'),
             ('chat', 'Where do your toys go?')], completions=[True, True, False, True, True, True])
        self.assertTrue(all(result['success'] for result in results), results)
        self.assertEqual([call['mode'] for call in calls], ['chat', 'chat', 'chat'])
        self.assertTrue(all('scene' not in call for call in calls))
        self.assertEqual([results[index]['english'] for index in (0, 2, 4)], ["Let's chat in English."] * 3)
        self.assertEqual(calls[1]['previous'], 'What would you like to drink?')
        self.assertEqual(calls[1]['dialogue_context'], [])
        self.assertEqual(calls[2]['dialogue_context'], [])

    async def test_replay_slow_and_explanation_return_to_confirmed_chat_history(self):
        reply = 'What would you like to drink?'
        results, calls = await self._run_turns(
            ['陪练模式', '我想喝水', '什么意思', '再简单一点', '再说一遍', '慢一点', '我想喝果汁'],
            [('chat', reply), ('explain', '这是问你想喝什么。'), ('explain_only', '你想喝什么。'),
             ('chat', 'Would you like some juice?')])
        self.assertTrue(all(result['success'] for result in results), results)
        self.assertTrue(all(call['mode'] == 'chat' and 'scene' not in call for call in calls))
        self.assertEqual(calls[-1]['dialogue_context'],
                         [{'child': '我想喝水', 'assistant': reply}])

    async def test_exit_and_reenter_commands_restore_the_requested_mode(self):
        for command, mode in [('结束陪练', 'express'), ('退出陪练模式', 'express'),
                              ('结束场景陪练', 'express'), ('进入陪练模式', 'chat')]:
            with self.subTest(command=command):
                reply = ('express', "I'm hungry.") if mode == 'express' else ('chat', 'What would you like?')
                results, calls = await self._run_turns(['陪练模式', command, '我饿了'], [reply])
                self.assertTrue(all(result['success'] for result in results), results)
                self.assertEqual(calls[0].get('mode', 'express'), mode)
                self.assertNotIn('scene', calls[0])
                self.assertFalse(calls[0].get('dialogue_context'))

    async def test_failed_or_aborted_output_cannot_commit_chat_history_on_late_ack(self):
        for returning, aborted in ((False, False), (True, True)):
            with self.subTest(returning=returning, aborted=aborted):
                conn = SimpleNamespace(mvp_request_id='request', mvp_requires_completion=True,
                    mvp_audio_returning=returning, client_abort=aborted, mvp_playback_reported=True,
                    mvp_playback_completed=False, mvp_dialogue_context=[])
                with patch('core.handle.englishMvp.event'), \
                     patch('core.handle.englishMvp.confirm_playback', wraps=confirm_playback) as confirmed:
                    await handle_message(conn, json.dumps({'type': 'mvp_playback', 'request_id': 'request',
                        'event': 'playback_completed', 'source': 'codec_queue_drained'}))
                confirmed.assert_not_called()
                self.assertEqual(conn.mvp_dialogue_context, [])

    async def test_hello_clears_practice_history_before_late_completion(self):
        conn = SimpleNamespace(mvp_request_id='request', mvp_teaching_mode='chat',
            mvp_dialogue_context=[{'child': '我想喝水', 'assistant': 'What would you like?'}],
            mvp_requires_completion=True, mvp_audio_returning=True, client_abort=False,
            mvp_playback_reported=True, mvp_playback_completed=False, mvp_pending_intent='chat',
            mvp_pending_english='Would you like some water?', mvp_pending_wav=b'local-wav',
            mvp_pending_transcript='我想喝水', welcome_msg={'type': 'hello'},
            websocket=SimpleNamespace(send=AsyncMock()))
        await handle_message(conn, json.dumps({'type': 'hello', 'version': 1,
            'audio_params': {'format': 'opus', 'sample_rate': 16000, 'channels': 1, 'frame_duration': 60},
            'features': {'mvp_playback_completed': True}}))
        self.assertEqual(conn.mvp_dialogue_context, [])
        self.assertEqual(conn.mvp_teaching_mode, 'express')
        with patch('core.handle.englishMvp.event'):
            await handle_message(conn, json.dumps({'type': 'mvp_playback', 'request_id': 'request',
                'event': 'playback_completed', 'source': 'codec_queue_drained'}))
        self.assertEqual(conn.mvp_dialogue_context, [])
        self.assertEqual(conn.mvp_teaching_mode, 'express')

    async def test_mode_confirmation_never_adds_the_opener_to_chat_history(self):
        conn = SimpleNamespace(mvp_pending_intent='mode', mvp_pending_english="Let's chat in English.",
            mvp_pending_wav=b'local-wav', mvp_pending_transcript='陪练模式',
            mvp_teaching_mode='chat', mvp_correction_context={'transcript': '旧的纠正'},
            mvp_clarification_context={'turns': [{'child': '那个', 'assistant': '哪个？'}]},
            mvp_help_context={'child': '什么意思', 'assistant': '旧解释', 'replay_previous': False},
            mvp_dialogue_context=[])
        with patch('core.handle.englishMvp.event'):
            confirm_playback(conn, 'codec_queue_drained')
        self.assertEqual(conn.mvp_teaching_mode, 'chat')
        self.assertIsNone(conn.mvp_correction_context)
        self.assertIsNone(conn.mvp_clarification_context)
        self.assertIsNone(conn.mvp_help_context)
        self.assertEqual(conn.mvp_dialogue_context, [])

    async def test_hello_during_pending_inference_cannot_restore_old_teaching_state(self):
        from core.connection import ConnectionHandler
        from threading import Event
        for phase in ('asr', 'tts', 'policy', 'llm'):
            with self.subTest(phase=phase), cloud.tempfile.TemporaryDirectory() as temporary:
                folder = cloud.Path(temporary)
                with cloud.wave.open(str(folder / 'sample.wav'), 'wb') as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(cloud.PCM)
                if self.tts.client is None or self.tts.client.is_closed:
                    self.tts.client = cloud.httpx.AsyncClient(transport=cloud.httpx.MockTransport(self.tts_response))
                entered, release, hello_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
                worker_release = Event()
                loop = asyncio.get_running_loop()
                connections, calls = [], []
                asr_count, tts_count, policy_count = 0, 0, 0

                def wait_in_worker():
                    loop.call_soon_threadsafe(entered.set)
                    if not worker_release.wait(5):
                        raise RuntimeError('local_fixture_gate_timeout')

                def remember_connection(*args, **kwargs):
                    conn = ConnectionHandler(*args, **kwargs)
                    connections.append(conn)
                    return conn

                async def recognize(*args, **kwargs):
                    nonlocal asr_count
                    asr_count += 1
                    if phase == 'asr' and asr_count == 1:
                        entered.set()
                        await release.wait()
                    if asr_count != 1:
                        return '我想喝水', None
                    transcript = ('进入对话陪练模式' if phase == 'policy' else
                                  '刚才不对，我不是这个意思' if phase == 'llm' else '开始陪练')
                    return transcript, None

                original_tts = self.tts.text_to_speak
                async def synthesize(*args, **kwargs):
                    nonlocal tts_count
                    tts_count += 1
                    if phase == 'tts' and tts_count == 1:
                        entered.set()
                        await release.wait()
                    return await original_tts(*args, **kwargs)

                def teach(text, rid, previous, **context):
                    calls.append(context)
                    if phase == 'llm' and len(calls) == 1:
                        wait_in_worker()
                        return 'correct', '你原本想表达什么？', {'input_tokens': 20, 'output_tokens': 8}
                    return 'express', 'I want some water.', {'input_tokens': 20, 'output_tokens': 8}

                def last_played_english(device_id):
                    nonlocal policy_count
                    policy_count += 1
                    if policy_count == 1:
                        wait_in_worker()
                    return 'An earlier expression.'

                config = cloud.load_cloud_config(require_credentials=False)
                config['selected_module'] = {}
                config['english_mvp']['log_path'] = str(folder / 'events.jsonl')
                server = cloud.WebSocketServer(config)
                server._asr = SimpleNamespace(close=AsyncMock(), speech_to_text=recognize)
                server._llm = SimpleNamespace(teaching_response=teach)
                if phase == 'policy':
                    server.cloud_policy = SimpleNamespace(
                        record=lambda conn, row: None, before_turn=lambda conn, seconds: {},
                        last_played_english=last_played_english, last_played_audio=lambda *args: None,
                        cache_pending_audio=lambda *args: None, reject_last_expression=lambda *args: None)
                with patch('core.websocket_server.ConnectionHandler', side_effect=remember_connection), \
                     patch('core.connection.initialize_tts', lambda c: self.tts), \
                     patch.object(self.tts, 'text_to_speak', side_effect=synthesize):
                    async with cloud.websockets.serve(server._handle_connection, '127.0.0.1', 0) as listener:
                        ws = await cloud.connect(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/',
                                                 features={'mvp_playback_completed': True})
                        old_turn = asyncio.create_task(cloud.one(ws, {'id': 'sample'}, folder, False, save_audio=False))
                        try:
                            await asyncio.wait_for(entered.wait(), 5)
                            conn = connections[0]
                            old_process, old_request = conn.mvp_task, conn.mvp_request_id
                            conn.mvp_last_english = 'An earlier expression.'
                            conn.mvp_last_transcript = '之前的表达'
                            conn.mvp_correction_context = {'transcript': '旧纠正', 'rejected_english': 'Old error.'}
                            original_send = conn.websocket.send

                            async def observe_send(message):
                                await original_send(message)
                                if isinstance(message, str) and json.loads(message).get('type') == 'hello':
                                    hello_seen.set()

                            with patch.object(conn.websocket, 'send', side_effect=observe_send):
                                await ws.send(json.dumps({'type': 'hello', 'version': 1,
                                    'audio_params': {'format': 'opus', 'sample_rate': 16000,
                                                     'channels': 1, 'frame_duration': 60},
                                    'features': {'mvp_playback_completed': True}}))
                                await asyncio.wait_for(hello_seen.wait(), 5)
                                self.assertTrue(conn.client_abort)
                                self.assertIsNone(conn.mvp_correction_context)
                                release.set()
                                worker_release.set()
                                stale = await asyncio.wait_for(old_turn, 5)
                                await asyncio.wait_for(asyncio.shield(old_process), 5)
                            self.assertFalse(stale['success'])
                            self.assertEqual(stale['output_frames'], 0)
                            for event, source in [('playback_started', 'codec_output_returned'),
                                                  ('playback_completed', 'codec_queue_drained')]:
                                await ws.send(json.dumps({'type': 'mvp_playback', 'request_id': old_request,
                                                          'event': event, 'source': source}))
                            await ws.send('{"type":"ping"}')
                            self.assertEqual(json.loads(await ws.recv())['type'], 'pong')
                            self.assertEqual(conn.mvp_dialogue_context, [])
                            self.assertEqual(conn.mvp_teaching_mode, 'express')
                            self.assertIsNone(conn.mvp_correction_context)
                            fresh = await asyncio.wait_for(cloud.one(ws, {'id': 'sample'}, folder, False, save_audio=False), 5)
                            self.assertTrue(fresh['success'], fresh)
                            self.assertEqual(fresh['english'], 'I want some water.')
                            self.assertEqual(len(calls), 2 if phase == 'llm' else 1)
                            self.assertFalse(calls[-1].get('scene'))
                            self.assertEqual(calls[-1].get('mode', 'express'), 'express')
                        finally:
                            release.set()
                            worker_release.set()
                            if not old_turn.done():
                                old_turn.cancel()
                                await asyncio.gather(old_turn, return_exceptions=True)
                            await ws.close()


if __name__ == '__main__':
    unittest.main()
