"""Synthetic semantic contracts; fixture replies do not establish model quality.

Verify that whole utterances and confirmed clarifications reach the real SDK
unchanged, with the same meaning rules in both modes and one provider call.
Real-provider semantic acceptance remains a separate deployment check.
"""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import tests.test_doubao_cloud as cloud
from core.utils.teaching import MEANING_RULES
from core.utils.teaching import is_unchanged_english_expression
from core.providers.llm.doubao_english.doubao_english import CLARIFICATION_PROMPT


class SemanticFidelityTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = cloud.ContractTests.asyncTearDown
    tts_response = cloud.ContractTests.tts_response

    async def test_meaning_requests_are_translated_without_chinese_help_or_recap(self):
        self.llm.meaning_requests_as_translation = True
        for text, english in [('这个词是什么意思？', 'What does this word mean?'),
                              ('用中文解释', 'Explain it in Chinese.')]:
            with self.subTest(text=text):
                self.teaching_reply = {'intent':'express','english':english}
                result = await asyncio.to_thread(self.llm.teaching_response,text,'synthetic',
                    previous_english='I want some more paper.')
                self.assertEqual(result[:2],('express',english))
                body=self.requests[-1][1]
                enum=body['response_format']['json_schema']['schema']['properties']['intent']['enum']
                self.assertNotIn('explain',enum);self.assertNotIn('explain_only',enum)
                payload=json.loads(body['messages'][1]['content'])
                self.assertEqual(payload['transcript'],text)
                self.assertNotIn('source_kind',payload)
        self.teaching_reply={'intent':'explain','english':'这是中文解释。'}
        with self.assertRaisesRegex(RuntimeError,'invalid_teaching_response'):
            await asyncio.to_thread(self.llm.teaching_response,'什么意思','synthetic')

    def test_unchanged_expression_guard_preserves_repairs_question_stance_and_modes(self):
        self.assertTrue(is_unchanged_english_expression('I am ready', 'I am ready.'))
        for source, output, mode in [('I want go home.', 'I want to go home.', 'express'),
                ('Can I 再拿一点纸？', 'Can I have some more paper?', 'express'),
                ('Can I go?', 'Can I go.', 'express'),
                ('Can I go!', 'Can I go?', 'express'),
                ('Can I have guiyuangan?', 'Can I have some dried longan?', 'express'),
                ('I am ready.', 'I am ready.', 'chat')]:
            with self.subTest(source=source, mode=mode):
                self.assertFalse(is_unchanged_english_expression(source, output, mode))
        self.assertFalse(is_unchanged_english_expression('I am ready.', 'I am ready.',
            correction_context={'transcript': '我准备好了', 'rejected_english': 'I am tired.'}))

    def llm_response(self, request):
        response = cloud.ContractTests.llm_response(self, request)
        if hasattr(self, 'finish_reason_fixture'):
            body = json.loads(response.content)
            body['choices'][0]['finish_reason'] = self.finish_reason_fixture
            # This body must not enter logs, errors, or TTS when incomplete.
            body['choices'][0]['message']['content'] = 'fixture-private-provider-body'
            return httpx.Response(200, json=body)
        return response

    async def asyncSetUp(self):
        self.enterContext(patch('httpx.HTTPTransport.handle_request',
            side_effect=AssertionError('External HTTP disabled in semantic contract tests')))
        self.enterContext(patch('httpx.AsyncHTTPTransport.handle_async_request',
            new=AsyncMock(side_effect=AssertionError('External HTTP disabled in semantic contract tests'))))
        await cloud.ContractTests.asyncSetUp(self)

    async def test_full_utterances_and_natural_variants_preserved_in_both_modes(self):
        cases = [
            ('妈妈，我想喝水。', 'Mom, I want some water.'),
            ('爸爸，我找不到帽子。', "Dad, I can't find my hat."),
            ('奶奶，Can I 再拿一点纸？', 'Grandma, can I have some more paper?'),
            ('安娜，等我一下。', 'Anna, wait for me.'),
            ('妈妈想喝水。', 'My mom wants some water.'),
            ('我想喝水。', 'I want some water.'),
            ('妈妈，花生怎么说？', 'Peanuts.'),
            ('先剪一颗星星给我看看，然后我自己剪。',
             "Cut out a star so I can see how, then I'll cut one out myself."),
            ('先剪一颗星星给我看看，然后我自己剪。',
             "Show me how to cut out a star, then I'll cut one out myself."),
            ('我、我想自己搭那个，不是要你替我搭。',
             "I want to build it myself. I don't want you to build it for me."),
            ('明天五点，不对，四点半出发。', "We're leaving at four thirty tomorrow."),
            ('Can I 再拿一点纸？', 'Can I have some more paper?'),
            ('踢球呀？', 'Play soccer?'),
            ('帮我拿一下那个。', 'Can you get that for me?'),
            ('黄色和蓝色的积木一样大吗？', 'Are the yellow and blue blocks the same size?'),
        ]
        for mode in ('express', 'chat'):
            for transcript, english in cases:
                with self.subTest(mode=mode, transcript=transcript, english=english):
                    self.teaching_reply = {'intent': mode, 'english': english}
                    before = len(self.requests)
                    reply = await asyncio.to_thread(self.llm.teaching_response,
                        transcript, 'synthetic', 'An unrelated sentence.', mode=mode)
                    self.assertEqual(reply[:2], (mode, english))
                    self.assertEqual(len(self.requests), before + 1)
                    request = self.requests[-1][1]
                    self.assertIn(MEANING_RULES, request['messages'][0]['content'])
                    self.assertEqual(json.loads(request['messages'][1]['content'])['transcript'], transcript)
                    self.assertTrue(request['response_format']['json_schema']['strict'])
                    self.assertEqual(self.llm.client.max_retries, 0)

    async def test_specific_clarification_and_answer_keep_original_unfinished_intent(self):
        unfinished = '黄色的积木和蓝色的积木是不是'
        question = '你想比较积木的哪个方面？'
        answer = '我想问大小是不是一样。'
        context = {'turns': [{'child': unfinished, 'assistant': question}]}
        for mode in ('express', 'chat'):
            with self.subTest(mode=mode):
                self.teaching_reply = {'intent': 'clarify', 'english': question}
                self.assertEqual((await asyncio.to_thread(self.llm.teaching_response,
                    unfinished, 'synthetic', mode=mode))[:2], ('clarify', question))
                self.teaching_reply = {'intent': mode,
                    'english': 'Are the yellow and blue blocks the same size?'}
                reply = await asyncio.to_thread(self.llm.teaching_response,
                    answer, 'synthetic', mode=mode, clarification_context=context)
                request = self.requests[-1][1]
                payload = json.loads(request['messages'][1]['content'])
                self.assertEqual(payload['transcript'], answer)
                self.assertEqual(payload['clarification_context'], context)
                self.assertTrue(request['messages'][0]['content'].endswith(CLARIFICATION_PROMPT))
                self.assertEqual(reply[1], 'Are the yellow and blue blocks the same size?')
                size = sum(len(message['content'].encode()) for message in request['messages']) + 256
                size += len(json.dumps({'response_format': request['response_format']},
                    ensure_ascii=False, separators=(',', ':')).encode())
                self.assertLessEqual(size, 6144)

    async def test_mixed_pinyin_idioms_keep_whole_input_and_faithful_variants(self):
        # Provider fixtures verify transport/acceptance, not live interpretation.
        cases = [
            ('OK, you can eat more, duō duō yì shàn.', 'express',
             'Okay, you can eat more. The more, the better.'),
            ('OK, you can eat more, duo duo yi shan.', 'repair',
             'Okay, you can have more. The more, the better.'),
            ('Do you like to eat guiyuangan?', 'express',
             'Do you like eating dried longan?'),
            ('Do you want to eat long yan gan?', 'express',
             'Do you want to eat dried longan?'),
            ('No rush, màn màn lái.', 'express', 'No rush. Take your time.'),
            ('We can walk, 这样一举两得。', 'express',
             'We can walk. That way, we get two benefits at once.'),
            ('I want to visit Li tomorrow.', 'express', 'I want to visit Li tomorrow.'),
            ('这个叫 shi，英语怎么说？', 'clarify', '你说的shi是什么东西？'),
        ]
        for mode in ('express', 'chat'):
            for transcript, intent, english in cases:
                with self.subTest(mode=mode, transcript=transcript):
                    intent = mode if intent == 'express' else intent
                    self.teaching_reply = {'intent': intent, 'english': english}
                    before = len(self.requests)
                    reply = await asyncio.to_thread(self.llm.teaching_response,
                        transcript, 'fixture', mode=mode)
                    self.assertEqual(reply[:2], (intent, english))
                    self.assertEqual(len(self.requests), before + 1)
                    request = self.requests[-1][1]
                    self.assertEqual(json.loads(request['messages'][1]['content'])['transcript'], transcript)
                    self.assertIn('Interpret idioms as whole phrases', request['messages'][0]['content'])
                    self.assertIn('never guess uncertain names/homophones', request['messages'][0]['content'])
                    self.assertIn('Keep correct English clauses and their permission/stance',
                                  request['messages'][0]['content'])

    async def test_short_activity_fragments_retain_neutral_specific_meaning(self):
        for transcript, english in (('跳绳呀。', 'Jumping rope.'), ('踢球呀？', 'Play soccer?')):
            with self.subTest(transcript=transcript):
                self.teaching_reply = {'intent': 'express', 'english': english}
                before = len(self.requests)
                reply = await asyncio.to_thread(self.llm.teaching_response, transcript, 'synthetic')
                self.assertEqual(reply[:2], ('express', english))
                request = self.requests[-1][1]
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(json.loads(request['messages'][1]['content'])['transcript'], transcript)
                self.assertIn('neutral English fragment', request['messages'][0]['content'])
                self.assertIn('ASR punctuation alone', request['messages'][0]['content'])

    async def test_explicit_invitation_and_wish_remain_distinct_from_fragments(self):
        for transcript, english in (('咱们一起跳绳吧。', "Let's jump rope together."),
                                    ('我想跳绳。', 'I want to jump rope.')):
            with self.subTest(transcript=transcript):
                self.teaching_reply = {'intent': 'express', 'english': english}
                reply = await asyncio.to_thread(self.llm.teaching_response, transcript, 'synthetic')
                self.assertEqual(reply[:2], ('express', english))

    async def test_specific_action_rule_preserves_idiomatic_provider_variants(self):
        # The contract transports the meaning rule and accepts faithful variants;
        # mocked replies cannot demonstrate whether the live model follows it.
        transcript = '我把那个盒子抱到门口了。'
        for english in ('I carried that box to the door.',
                        'I took that box to the door in my arms.'):
            with self.subTest(english=english):
                self.teaching_reply = {'intent': 'express', 'english': english}
                before = len(self.requests)
                actual = await asyncio.to_thread(self.llm.teaching_response,
                    transcript, 'synthetic')
                self.assertEqual(actual[:2], ('express', english))
                self.assertEqual(len(self.requests), before + 1)
                request = self.requests[-1][1]
                self.assertEqual(json.loads(request['messages'][1]['content'])['transcript'], transcript)
                rules = request['messages'][0]['content']
                self.assertIn('carried is not merely moved', rules)
                self.assertIn('whispered is not merely spoke', rules)
                self.assertIn('Rephrase idiomatically, not word by word', rules)

    async def test_fewshot_contrasts_use_valid_mode_json_and_retain_input(self):
        for mode in ('express', 'chat'):
            prompt = cloud.TEACHING_PROMPT if mode == 'express' else cloud.CHAT_PROMPT
            examples = [line.partition(' -> ') for line in prompt.splitlines()
                        if ' -> {"intent":' in line]
            self.assertEqual(len(examples), 12 if mode == 'express' else 4)
            self.assertNotIn('"MODE"', prompt)
            for transcript, _, serialized in examples:
                reply = json.loads(serialized)
                self.assertIn(reply['intent'], (mode, 'clarify', 'no_change'))
                self.teaching_reply = reply
                actual = await asyncio.to_thread(self.llm.teaching_response,
                    transcript, 'synthetic', mode=mode)
                self.assertEqual(actual[:2], (reply['intent'], reply['english']))
                request = self.requests[-1][1]
                self.assertEqual(json.loads(request['messages'][1]['content'])['transcript'], transcript)
                self.assertIn(reply['intent'], request['response_format']['json_schema']
                              ['schema']['properties']['intent']['enum'])

    async def test_same_complete_question_has_expression_and_chat_response_examples(self):
        transcript = '这件衣服和那件衣服是不是一样大？'
        cases = [('express', cloud.TEACHING_PROMPT, 'Are these clothes the same size?'),
                 ('chat', cloud.CHAT_PROMPT,
                  "I can't see the clothes. You can put them together to check.")]
        for mode, prompt, english in cases:
            with self.subTest(mode=mode):
                line = next(line for line in prompt.splitlines() if line.startswith(transcript + ' -> '))
                example = json.loads(line.partition(' -> ')[2])
                self.assertEqual(example, {'intent': mode, 'english': english})
                self.teaching_reply = example
                actual = await asyncio.to_thread(self.llm.teaching_response,
                    transcript, 'synthetic', mode=mode)
                self.assertEqual(actual[:2], (mode, english))
                self.assertEqual(self.requests[-1][1]['messages'][0]['content'], prompt)
        self.assertNotIn('Are these clothes the same size?', cloud.CHAT_PROMPT)

    async def test_coherent_fragments_and_complete_english_endings_are_not_rewritten(self):
        for transcript, english in (('跳绳呀。', 'Jumping rope.'),
                                    ('我想知道她是谁。', 'I wonder who she is.'),
                                    ('我不知道……', "I don't know..."),
                                    ('就是这样的。', "That's how it is.")):
            with self.subTest(transcript=transcript):
                self.teaching_reply = {'intent': 'express', 'english': english}
                reply = await asyncio.to_thread(self.llm.teaching_response, transcript, 'synthetic')
                self.assertEqual(reply[:2], ('express', english))

    async def test_complete_questions_preserve_underspecified_referents_after_clarification(self):
        context = {'turns': [{'child': '杯子有几个？', 'assistant': '你说的是哪种杯子？'}]}
        for text, english in [('盒子有几个？', 'How many boxes are there?'),
                              ('这个放哪里？', 'Where does this go?'),
                              ('把这些拿过来。', 'Bring these over here.'),
                              ('How many 杯子 are there?', 'How many cups are there?')]:
            with self.subTest(text=text):
                self.teaching_reply = {'intent': 'express', 'english': english}
                before = len(self.requests)
                reply = await asyncio.to_thread(self.llm.teaching_response, text,
                    'synthetic', clarification_context=context)
                self.assertEqual(reply[:2], ('express', english))
                request = self.requests[-1][1]
                self.assertEqual(json.loads(request['messages'][1]['content'])['transcript'], text)
                self.assertEqual(len(self.requests), before+1)
                self.assertIn('express, not an answer', request['messages'][0]['content'])
                self.assertIn('Unknown counts/locations', request['messages'][0]['content'])
                self.assertIn('Complete new meaning supersedes', request['messages'][0]['content'])

    async def test_silent_expression_validates_mode_language_and_empty_output(self):
        self.teaching_reply = {'intent': 'no_change', 'english': ''}
        before = len(self.requests)
        reply = await asyncio.to_thread(self.llm.teaching_response,
            'I found my green pencil.', 'synthetic')
        self.assertEqual(reply[:2], ('no_change', ''))
        self.assertEqual(len(self.requests), before+1)
        schema = self.requests[-1][1]['response_format']['json_schema']['schema']
        self.assertIn('no_change', schema['properties']['intent']['enum'])
        for text, options in [('我找到铅笔了', {}), ('I found 我的铅笔', {}),
                              ('123', {}), ('Hello.', {'mode': 'chat'}),
                              ('A blue pencil.', {'correction_context': {
                                  'transcript': '我的蓝色铅笔', 'rejected_english': 'A red pencil.'}})]:
            with self.subTest(text=text, options=options):
                with self.assertRaisesRegex(RuntimeError, 'invalid_no_change'):
                    await asyncio.to_thread(self.llm.teaching_response, text, 'synthetic', **options)
                schema = self.requests[-1][1]['response_format']['json_schema']['schema']
                self.assertNotIn('no_change', schema['properties']['intent']['enum'])
        self.teaching_reply = {'intent': 'no_change', 'english': 'I found my green pencil.'}
        with self.assertRaisesRegex(RuntimeError, 'invalid_no_change'):
            await asyncio.to_thread(self.llm.teaching_response, 'I found my green pencil.', 'synthetic')

    async def test_chinese_work_statement_cannot_be_silenced_by_previous_english(self):
        for text in ('我得去工作了。', '我得去工作啦。', 'I 得去工作了。'):
            with self.subTest(text=text):
                self.teaching_reply = {'intent': 'express', 'english': 'I have to go to work.'}
                reply = await asyncio.to_thread(self.llm.teaching_response, text, 'synthetic',
                    previous_english='I have to go to work.')
                self.assertEqual(reply[:2], ('express', 'I have to go to work.'))
                body = self.requests[-1][1]
                enum = body['response_format']['json_schema']['schema']['properties']['intent']['enum']
                self.assertNotIn('no_change', enum)
                self.assertEqual(json.loads(body['messages'][1]['content'])['transcript'], text)

    async def test_silent_candidate_does_not_suppress_chat_or_english_repairs(self):
        for text, mode, intent, english in [
            ('I want go home.', 'express', 'express', 'I want to go home.'),
            ('Can I have long yan gan?', 'express', 'express', 'Can I have some dried longan?'),
            ('I found my green pencil.', 'chat', 'chat', 'Great! What will you draw?')]:
            with self.subTest(text=text, mode=mode):
                self.teaching_reply = {'intent': intent, 'english': english}
                reply = await asyncio.to_thread(self.llm.teaching_response, text, 'synthetic', mode=mode)
                self.assertEqual(reply[:2], (intent, english))
                if mode == 'chat':
                    enum = self.requests[-1][1]['response_format']['json_schema']['schema']['properties']['intent']['enum']
                    self.assertNotIn('no_change', enum)

    async def test_incomplete_outputs_report_only_allowlisted_finish_reason_without_retry(self):
        for reason in ('length', 'content_filter', 'tool_calls'):
            with self.subTest(reason=reason):
                self.finish_reason_fixture = reason
                before = len(self.requests)
                with self.assertRaises(RuntimeError) as caught:
                    await asyncio.to_thread(self.llm.teaching_response, '两种颜色的积木是不是', 'synthetic')
                self.assertEqual(str(caught.exception),
                    'doubao_english_failed:ValueError:incomplete_teaching_response:finish_reason=' + reason)
                self.assertNotIn('fixture-private-provider-body', str(caught.exception))
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.requests[-1][1]['max_tokens'], 256)

    async def test_unknown_finish_reason_never_exports_raw_provider_metadata(self):
        for reason in ('Authorization=Bearer fixture-secret', None, 'function_call'):
            with self.subTest(reason=reason):
                self.finish_reason_fixture = reason
                with self.assertRaises(RuntimeError) as caught:
                    await asyncio.to_thread(self.llm.teaching_response, '两种颜色的积木是不是', 'synthetic')
                self.assertEqual(str(caught.exception),
                    'doubao_english_failed:ValueError:incomplete_teaching_response:finish_reason=other')
                self.assertNotIn('fixture-secret', str(caught.exception))
                self.assertNotIn('fixture-private-provider-body', str(caught.exception))

    async def test_silent_turn_keeps_heard_sentence_for_repeat_on_same_connection(self):
        original = 'I want to draw a star.'
        results, calls, events = await cloud.ContractTests._run_clarification_turns(self,
            ['我想画一颗星星。', 'I found my green pencil.', '再说一遍'],
            [('express', original), ('no_change', '')], return_events=True)
        self.assertTrue(all(row['success'] for row in results))
        self.assertTrue(results[1]['no_audio'])
        self.assertEqual(results[1]['output_frames'], 0)
        self.assertNotIn('english', results[1])
        self.assertEqual(results[2]['english'], original)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['previous'], original)
        silent = [row for row in events if row['request_id'] == results[1]['request_id']]
        self.assertTrue(next(row for row in silent if row['event'] == 'request_complete')['success'])
        self.assertFalse(any(row['event'] in ('audio_sent', 'playback_confirmed') for row in silent))

    async def test_specific_clarification_is_played_and_used_for_next_full_expression(self):
        unfinished = '黄色的积木和蓝色的积木是不是'
        question = '你想比较积木的哪个方面？'
        result = 'Are the yellow and blue blocks the same size?'
        results, calls = await cloud.ContractTests._run_clarification_turns(self,
            [unfinished, '我想问大小是不是一样。', '我自己来搭。'],
            [('clarify', question), ('express', result), ('express', "I'll build it myself.")])
        self.assertTrue(all(row['success'] for row in results))
        self.assertEqual(results[0]['english'], question)
        self.assertEqual(results[1]['english'], result)
        self.assertEqual(calls[1]['clarification_context'], {
            'turns': [{'child': unfinished, 'assistant': question}]})
        self.assertFalse(calls[2].get('clarification_context'))


if __name__ == '__main__': unittest.main()
