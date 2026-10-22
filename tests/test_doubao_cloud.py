"""Real WS/Opus + real Ark SDK serialization; vendor replies are local fixtures.
No paid calls, no microphones, and no recognition-quality claim.
"""
import asyncio
import base64
import gzip
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch, AsyncMock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from cloud_setup import load_cloud_config, initialize_config
CONFIG = load_cloud_config(require_credentials=False)
initialize_config(CONFIG)
import httpx
import numpy as np
import websockets
from volcenginesdkarkruntime import Ark
from core.providers.asr.doubao_seed import ASRProvider
from core.providers.llm.doubao_english.doubao_english import (
    LLMProvider, PROMPT, TEACHING_PROMPT, CHAT_PROMPT, HELP_FOLLOWUP_PROMPT,
)
from core.providers.tts.doubao_seed import TTSProvider
from core.websocket_server import WebSocketServer
from protocol_benchmark import connect, one

RAW = '嗯，今天两点开始上课，所以我们吃饭要吃快一点。'
ENGLISH = 'Class starts at two today, so we need to eat a little faster.'
PCM = (np.sin(np.arange(4800)*.11)*3000).astype('<i2').tobytes()


def response_packet(body, flags=3, compressed=True, kind=9):
    payload=json.dumps(body,ensure_ascii=False).encode()
    if compressed:payload=gzip.compress(payload)
    header=bytes([0x11,kind*16+flags,0x10+int(compressed),0])
    if kind==15:header+=struct.pack('>I',45000001)
    elif flags&1:header+=struct.pack('>i',-1 if flags&2 else 1)
    return header+struct.pack('>I',len(payload))+payload


class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        original_connect = websockets.connect
        def loopback_only(uri, *args, **kwargs):
            if urlsplit(uri).hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise AssertionError('External WebSocket disabled in local contract tests')
            return original_connect(uri, *args, **kwargs)
        self.enterContext(patch.object(websockets, 'connect', side_effect=loopback_only))
        self.asr=ASRProvider({'api_key':'test-secret'})
        self.tts=TTSProvider({'api_key':'test-secret','speaker':'test-speaker'})
        self.llm=LLMProvider({'api_key':'test-secret','model_name':'test-model'})
        self.requests=[]
        self.llm.client.close()
        self.llm.client=Ark(api_key='test-secret',max_retries=0,
                            http_client=httpx.Client(transport=httpx.MockTransport(self.llm_response)))
        self.tts.client=httpx.AsyncClient(transport=httpx.MockTransport(self.tts_response))
        self.teaching_reply=None
        self.direct_help_reply=None

    async def asyncTearDown(self):
        await self.tts.close(); self.llm.close()

    def llm_response(self,request):
        payload=json.loads(request.content)
        self.requests.append(('llm',payload))
        system_prompt=payload['messages'][0]['content']
        if system_prompt == HELP_FOLLOWUP_PROMPT:
            reply=self.direct_help_reply
            if reply is None:
                reply={'english':self.teaching_reply['english'] if self.teaching_reply else '就是请别人再说一次。'}
            content=json.dumps(reply)
        else:
            content=(json.dumps(self.teaching_reply or {'intent':'express','english':ENGLISH})
                     if '"intent"' in system_prompt else ENGLISH)
        return httpx.Response(200,json={'id':'ark-fixture','object':'chat.completion','created':1,'model':'test-model',
            'choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':120,'completion_tokens':15,'total_tokens':135}})

    def tts_response(self,request):
        self.requests.append(('tts',json.loads(request.content)))
        rows=[{'code':0,'data':base64.b64encode(PCM).decode()},
              {'code':20000000,'usage':{'text_words':len(ENGLISH)}}]
        return httpx.Response(200,content='\n'.join(json.dumps(x) for x in rows)+'\n')

    async def test_official_sdk_semantics_usage_and_no_cleanup(self):
        output, usage=await asyncio.to_thread(self.llm.response_result,RAW,'request')
        self.assertEqual(output,ENGLISH); self.assertEqual(usage['input_tokens'],120)
        request=self.requests[0][1]
        self.assertEqual(request['messages'][1]['content'],RAW)
        self.assertEqual(request['thinking'],{'type':'disabled'})
        self.assertFalse(request['stream']); self.assertEqual(request['max_tokens'],256)
        self.assertIn('negation',PROMPT); self.assertEqual(self.llm.client.max_retries,0)

    async def test_teaching_model_receives_context_and_validates_intent(self):
        intent, english, usage = await asyncio.to_thread(
            self.llm.teaching_response, '妈妈，我的 water bottle 在哪里？', 'request', 'I want some water.')
        self.assertEqual((intent,english),('express',ENGLISH))
        self.assertEqual(usage['input_tokens'],120)
        request=self.requests[-1][1]
        self.assertIn('mixes Chinese and English',TEACHING_PROMPT)
        self.assertIn('does not prove',TEACHING_PROMPT)
        self.assertEqual(json.loads(request['messages'][1]['content'])['previous_english'],'I want some water.')
        self.assertEqual(request['max_tokens'],256)
        self.assertEqual(request['thinking'],{'type':'disabled'})
        correction={'transcript':'我的帽子在哪里？','rejected_english':'Where is my red hat?'}
        await asyncio.to_thread(self.llm.teaching_response,'是蓝色的帽子','request',None,correction)
        context=json.loads(self.requests[-1][1]['messages'][1]['content'])
        self.assertEqual(context['correction_context'],correction)

    async def test_teaching_model_explanation_and_control_contract(self):
        for intent, reply in [('explain','some more 是“再多一点”。'),
                              ('explain_only','Say again 是“再说一遍”的意思。'),
                              ('clarify','你说的是哪个东西？'),
                              ('correct','你原本想表达什么？'),
                              ('repair','Where is my blue hat?'),
                              ('slow','')]:
            self.teaching_reply={'intent':intent,'english':reply}
            actual, spoken, _ = await asyncio.to_thread(
                self.llm.teaching_response, 'some more 是什么意思？','request','Can I have some more juice?')
            self.assertEqual((actual,spoken),(intent,reply))
        self.teaching_reply={'intent':'repeat','english':'changed words'}
        with self.assertRaisesRegex(RuntimeError,'doubao_english_failed:ValueError'):
            await asyncio.to_thread(self.llm.teaching_response,'say again','request','Original sentence.')
        self.teaching_reply={'intent':'explain','english':''}
        with self.assertRaisesRegex(RuntimeError,'doubao_english_failed:ValueError'):
            await asyncio.to_thread(self.llm.teaching_response,'什么意思','request','Original sentence.')
        self.teaching_reply={'intent':'explain_only','english':''}
        with self.assertRaisesRegex(RuntimeError,'doubao_english_failed:ValueError'):
            await asyncio.to_thread(self.llm.teaching_response,'say again 是什么意思','request')
        self.teaching_reply={'intent':'stop','english':''}
        with self.assertRaisesRegex(RuntimeError,'doubao_english_failed:ValueError'):
            await asyncio.to_thread(self.llm.teaching_response,'停','request','Original sentence.')

    async def test_partial_replay_and_hint_must_use_previous_words(self):
        previous='Can I have some more juice?'
        for intent,spoken in [('partial_repeat','some more juice'),('hint','Can I have')]:
            self.teaching_reply={'intent':intent,'english':spoken}
            actual=await asyncio.to_thread(self.llm.teaching_response,'后半句再说一下',
                                           'request',previous)
            self.assertEqual(actual[:2],(intent,spoken))
        for intent,spoken in [('partial_repeat','some more water'),
                              ('hint','Can I ha'),('hint',previous)]:
            self.teaching_reply={'intent':intent,'english':spoken}
            with self.assertRaisesRegex(RuntimeError,'doubao_english_failed:ValueError'):
                await asyncio.to_thread(self.llm.teaching_response,'给我一点提示','request',previous)

    async def test_chat_model_contract_requires_explicit_mode_and_bounded_context(self):
        self.teaching_reply={'intent':'chat','english':'What would you like to eat?'}
        intent, spoken, _=await asyncio.to_thread(
            self.llm.teaching_response,'我饿了','request',None,None,
            mode='chat',dialogue_context=[{'child':'Hello.','assistant':'Hi!'}])
        self.assertEqual((intent,spoken),('chat','What would you like to eat?'))
        context=json.loads(self.requests[-1][1]['messages'][1]['content'])
        self.assertEqual(context['mode'],'chat')
        self.assertEqual(context['dialogue_context'][0]['child'],'Hello.')
        self.assertEqual(self.requests[-1][1]['messages'][0]['content'],CHAT_PROMPT)
        long_turn={'child':'中'*400,'assistant':'A'*400}
        await asyncio.to_thread(self.llm.teaching_response,'我饿了','request',None,None,
                                mode='chat',dialogue_context=[long_turn]*4)
        trimmed=json.loads(self.requests[-1][1]['messages'][1]['content'])['dialogue_context']
        self.assertLess(len(trimmed),4)
        with self.assertRaisesRegex(ValueError,'invalid_dialogue_context'):
            self.llm.teaching_response('Hello','request',mode='chat',
                dialogue_context=[{'child':'x','assistant':'y'}]*5)
        with self.assertRaisesRegex(RuntimeError,'doubao_english_failed:ValueError'):
            await asyncio.to_thread(self.llm.teaching_response,'我饿了','request')
        self.teaching_reply={'intent':'express','english':"I'm hungry."}
        with self.assertRaisesRegex(RuntimeError,'doubao_english_failed:ValueError'):
            await asyncio.to_thread(self.llm.teaching_response,'我饿了','request',mode='chat')

    async def test_clarification_model_receives_confirmed_question_and_rejects_bad_context(self):
        clarification={'turns':[{'child':'那个怎么弄？','assistant':'你说的是哪个东西？'}]}
        await asyncio.to_thread(self.llm.teaching_response,'这个瓶盖','request',
                                clarification_context=clarification)
        context=json.loads(self.requests[-1][1]['messages'][1]['content'])
        self.assertEqual(context['clarification_context'],clarification)
        invalid_contexts=[
            [], {}, {'turns':[]}, {'turns':'question'},
            {'turns':[{'child':'x','assistant':'y'}]*3},
            {'turns':[{'child':'x','assistant':'y','extra':'z'}]},
            {'turns':[{'child':None,'assistant':'y'}]},
            {'turns':[{'child':'','assistant':'y'}]},
            {'turns':[{'child':'x','assistant':'  '}]},
            {'turns':[{'child':'x'*401,'assistant':'y'}]},
            {'turns':[{'child':'x','assistant':'y'*401}]},
            {'turns':[{'child':'x','assistant':'y'}],'extra':True},
        ]
        calls=len(self.requests)
        for invalid in invalid_contexts:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError,'invalid_clarification_context'):
                    self.llm.teaching_response('这个瓶盖','request',clarification_context=invalid)
        self.assertEqual(len(self.requests),calls)

    async def test_clarification_context_is_preserved_within_budget_or_rejected_before_sdk(self):
        self.teaching_reply={'intent':'chat','english':'Try turning the cap.'}
        clarification={'turns':[{'child':'那个怎么弄？','assistant':'你说的是哪个东西？'}]}
        long_turn={'child':'中'*400,'assistant':'A'*400}
        await asyncio.to_thread(self.llm.teaching_response,'这个瓶盖','request',mode='chat',
                                dialogue_context=[long_turn]*4,
                                clarification_context=clarification)
        request=self.requests[-1][1]
        context=json.loads(request['messages'][1]['content'])
        self.assertEqual(context['clarification_context'],clarification)
        self.assertLess(len(context['dialogue_context']),4)
        self.assertLessEqual(sum(len(message['content'].encode('utf-8'))
                                 for message in request['messages'])+256,6144)
        oversized={'turns':[{'child':'中'*400,'assistant':'问'*400}]*2}
        calls=len(self.requests)
        with self.assertRaisesRegex(ValueError,'english_input_exceeds_budget_bound'):
            self.llm.teaching_response('答'*400,'request',mode='chat',
                                      clarification_context=oversized)
        self.assertEqual(len(self.requests),calls)

    async def test_help_model_receives_confirmed_explanation_and_rejects_bad_context(self):
        self.teaching_reply={'intent':'explain_only','english':'就是请别人再说一次。'}
        help_context={'child':'Say again 是什么意思？','assistant':'Say again 是“再说一遍”的意思。',
                      'replay_previous':False}
        await asyncio.to_thread(self.llm.teaching_response,'说简单一点','request',
                                'I want some water.',help_context=help_context)
        context=json.loads(self.requests[-1][1]['messages'][1]['content'])
        self.assertEqual(context,{'source_kind':'explanation','source_text':help_context['assistant'],
                                 'original_question':help_context['child'],'request':'说简单一点'})
        invalid_contexts=[
            [], {}, {'child':'x'}, {'assistant':'y'}, {'child':'x','assistant':'y'},
            {'child':'x','assistant':'y','replay_previous':False,'extra':'z'},
            {'child':None,'assistant':'y','replay_previous':False},
            {'child':'x','assistant':1,'replay_previous':False},
            {'child':'','assistant':'y','replay_previous':False},
            {'child':'x','assistant':'  ','replay_previous':False},
            {'child':'x'*401,'assistant':'y','replay_previous':False},
            {'child':'x','assistant':'y'*401,'replay_previous':False},
            *[{'child':'x','assistant':'y','replay_previous':value} for value in (0,1,'false',None)],
        ]
        calls=len(self.requests)
        for invalid in invalid_contexts:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError,'invalid_help_context'):
                    self.llm.teaching_response('说简单一点','request',help_context=invalid)
        self.assertEqual(len(self.requests),calls)

    async def test_general_context_keeps_budget_bound_without_sending_old_help(self):
        self.teaching_reply={'intent':'explain_only','english':'就是请别人再说一次。'}
        help_context={'child':'Say again 是什么意思？','assistant':'Say again 是“再说一遍”的意思。',
                      'replay_previous':False}
        clarification={'turns':[{'child':'那个怎么弄？','assistant':'你说的是哪个东西？'}]}
        long_turn={'child':'中'*400,'assistant':'A'*400}
        await asyncio.to_thread(self.llm.teaching_response,'这个解释里的词是什么含义？','request',mode='chat',
                                dialogue_context=[long_turn]*4,
                                clarification_context=clarification,help_context=help_context)
        request=self.requests[-1][1]
        self.assertNotEqual(request['messages'][0]['content'],HELP_FOLLOWUP_PROMPT)
        context=json.loads(request['messages'][1]['content'])
        self.assertNotIn('help_context',context)
        self.assertEqual(context['clarification_context'],clarification)
        self.assertLess(len(context['dialogue_context']),4)
        self.assertLessEqual(sum(len(message['content'].encode('utf-8'))
                                 for message in request['messages'])+256,6144)
        calls=len(self.requests)
        with self.assertRaisesRegex(ValueError,'english_input_exceeds_budget_bound'):
            self.llm.teaching_response('答'*400,'request',mode='chat',
                clarification_context={'turns':[{'child':'中'*400,'assistant':'问'*400}]*2},
                help_context={'child':'中'*400,'assistant':'解'*400,'replay_previous':True})
        self.assertEqual(len(self.requests),calls)

    async def test_exact_help_followups_use_program_selected_intent_in_both_modes(self):
        self.direct_help_reply={'english':'就是问能不能再要一点果汁。'}
        previous='Can I have some more juice?'
        for mode in ('express','chat'):
            for replay in (False,True):
                help_context={'child':'some more 是什么意思？','assistant':'就是再多一点。',
                              'replay_previous':replay}
                for text,expected in [('再简单一点','explain' if replay else 'explain_only'),
                                      ('说简单点。','explain' if replay else 'explain_only'),
                                      ('再简单一点，只用中文说','explain_only')]:
                    with self.subTest(mode=mode,replay=replay,text=text):
                        intent,spoken,usage=await asyncio.to_thread(self.llm.teaching_response,
                            text,'request',previous,mode=mode,help_context=help_context)
                        self.assertEqual((intent,spoken),(expected,self.direct_help_reply['english']))
                        request=self.requests[-1][1]
                        self.assertEqual(request['messages'][0]['content'],HELP_FOLLOWUP_PROMPT)
                        context=json.loads(request['messages'][1]['content'])
                        self.assertEqual(context,{'source_kind':'explanation',
                            'source_text':help_context['assistant'],
                            'original_question':help_context['child'],'request':text})
                        self.assertEqual(usage['input_tokens'],120)
                        self.assertEqual(request['max_tokens'],256)
                        self.assertFalse(request['stream'])

    async def test_direct_help_preserves_chinese_only_across_successive_simplifications(self):
        previous='Can I have some more juice?'
        help_context={'child':'只用中文解释','assistant':'你想问能不能再要一点果汁。',
                      'replay_previous':False}
        for text,reply in [('再简单一点','就是问能不能再要点果汁。'),('再简单点','想再要点果汁。')]:
            self.direct_help_reply={'english':reply}
            intent,spoken,_=await asyncio.to_thread(self.llm.teaching_response,
                text,'request',previous,help_context=help_context)
            self.assertEqual(intent,'explain_only')
            self.assertEqual(spoken,reply)
            self.assertEqual(json.loads(self.requests[-1][1]['messages'][1]['content']),
                             {'source_kind':'explanation','source_text':help_context['assistant'],
                              'original_question':help_context['child'],'request':text})
            help_context={'child':text,'assistant':spoken,'replay_previous':intent=='explain'}

    async def test_exact_meaning_request_can_use_previous_english_without_help_context(self):
        self.direct_help_reply={'english':'就是想再要一点果汁。'}
        for text,intent in [('什么意思？','explain'),('我没听懂','explain'),
                            ('用中文解释一下','explain'),('只用中文解释','explain_only')]:
            with self.subTest(text=text):
                actual=await asyncio.to_thread(self.llm.teaching_response,text,'request',
                                               'Can I have some more juice?')
                self.assertEqual(actual[:2],(intent,self.direct_help_reply['english']))
                request=self.requests[-1][1]
                self.assertEqual(request['messages'][0]['content'],HELP_FOLLOWUP_PROMPT)
                context=json.loads(request['messages'][1]['content'])
                self.assertEqual(context,{'source_kind':'english',
                    'source_text':'Can I have some more juice?','request':text})

    async def test_quoted_nonmatching_or_contextless_help_stays_with_general_classifier(self):
        self.teaching_reply={'intent':'express','english':'Could you say that more simply?'}
        help_context={'child':'some more 是什么意思？','assistant':'就是再多一点。','replay_previous':True}
        for text in ('“再简单一点”','"再简单一点"',"'再简单一点'",'他说再简单一点。',
                     '再简单一点是什么意思？','some more 什么意思？','“什么意思”'):
            with self.subTest(text=text):
                actual=await asyncio.to_thread(self.llm.teaching_response,text,'request',
                    'Can I have some more juice?',help_context=help_context)
                self.assertEqual(actual[:2],('express',self.teaching_reply['english']))
                self.assertNotEqual(self.requests[-1][1]['messages'][0]['content'],HELP_FOLLOWUP_PROMPT)
                context=json.loads(self.requests[-1][1]['messages'][1]['content'])
                self.assertNotIn('help_context',context)
                self.assertEqual(context['previous_english'],'Can I have some more juice?')
        for text in ('再简单一点','什么意思？','只用中文解释'):
            with self.subTest(no_context=text):
                actual=await asyncio.to_thread(self.llm.teaching_response,text,'request')
                self.assertEqual(actual[0],'express')
                self.assertNotEqual(self.requests[-1][1]['messages'][0]['content'],HELP_FOLLOWUP_PROMPT)

    async def test_direct_help_rejects_model_intent_extra_keys_and_non_chinese_replies(self):
        help_context={'child':'some more 是什么意思？','assistant':'就是再多一点。','replay_previous':False}
        invalid_replies=[[],{}, {'intent':'express','english':'Please say it more simply.'},
            {'english':'再多一点。','extra':True}, {'english':''}, {'english':'  '},
            {'english':None},{'english':123},{'english':'中'*401},
            {'english':'Please say it more simply.'}]
        for reply in invalid_replies:
            with self.subTest(reply=reply):
                self.direct_help_reply=reply
                calls=len(self.requests)
                with self.assertRaisesRegex(RuntimeError,'doubao_english_failed:ValueError'):
                    await asyncio.to_thread(self.llm.teaching_response,'再简单一点','request',
                        'Can I have some more juice?',help_context=help_context)
                self.assertEqual(len(self.requests),calls+1)
                self.assertEqual(self.requests[-1][1]['messages'][0]['content'],HELP_FOLLOWUP_PROMPT)

    async def test_direct_help_rejects_oversized_request_before_sdk(self):
        calls=len(self.requests)
        with self.assertRaisesRegex(ValueError,'english_input_exceeds_budget_bound'):
            self.llm.teaching_response(' '*7000+'再简单一点','request','Can I have some more juice?',
                clarification_context={'turns':[{'child':'中'*400,'assistant':'问'*400}]*2},
                help_context={'child':'中'*400,'assistant':'解'*400,'replay_previous':True})
        self.assertEqual(len(self.requests),calls)

    async def test_direct_help_sends_only_selected_target_despite_unrelated_large_contexts(self):
        previous='Can I have some more juice?'
        old_turn={'child':'旧'*400,'assistant':'Old unrelated context. '*16}
        clarification={'turns':[{'child':'问'*400,'assistant':'答'*400}]*2}
        correction={'transcript':'错'*400,'rejected_english':'Wrong interpretation. '*16}
        cases=[
            ({'child':'Say again 是什么意思？','assistant':'就是请别人再说一次。',
              'replay_previous':False},'再简单一点','explain_only'),
            ({'child':'some more 是什么意思？','assistant':'就是再多一点。',
              'replay_previous':True},'再简单一点','explain'),
            (None,'什么意思？','explain'),
        ]
        for help_context,text,expected_intent in cases:
            with self.subTest(help_context=help_context):
                self.direct_help_reply={'english':help_context['assistant'] if help_context else '想再要一点果汁。'}
                intent,spoken,_=await asyncio.to_thread(self.llm.teaching_response,text,'request',
                    previous,correction,mode='chat',dialogue_context=[old_turn]*4,
                    clarification_context=clarification,help_context=help_context)
                self.assertEqual(intent,expected_intent)
                self.assertEqual(spoken,self.direct_help_reply['english'])
                request=self.requests[-1][1]
                self.assertEqual(request['messages'][0]['content'],HELP_FOLLOWUP_PROMPT)
                context=json.loads(request['messages'][1]['content'])
                if help_context:
                    self.assertEqual(context,{'source_kind':'explanation',
                        'source_text':help_context['assistant'],
                        'original_question':help_context['child'],'request':text})
                    self.assertNotIn(previous,request['messages'][1]['content'])
                else:
                    self.assertEqual(context,{'source_kind':'english','source_text':previous,'request':text})
                self.assertLessEqual(sum(len(message['content'].encode('utf-8'))
                                         for message in request['messages'])+256,6144)

    async def test_new_complete_topic_excludes_old_help_even_when_supplied_to_provider(self):
        self.teaching_reply={'intent':'express','english':"I'm looking for my blue schoolbag."}
        help_context={'child':'some more 是什么意思？','assistant':'就是问能不能再要一点果汁。',
                      'replay_previous':False}
        actual=await asyncio.to_thread(self.llm.teaching_response,'我在找我的蓝色书包','request',
            'Can I have some more juice?',help_context=help_context)
        self.assertEqual(actual[:2],('express',self.teaching_reply['english']))
        request=self.requests[-1][1]
        self.assertEqual(request['messages'][0]['content'],TEACHING_PROMPT)
        context=json.loads(request['messages'][1]['content'])
        self.assertEqual(context['transcript'],'我在找我的蓝色书包')
        self.assertEqual(context['previous_english'],'Can I have some more juice?')
        self.assertNotIn('help_context',context)
        self.assertNotIn(help_context['assistant'],request['messages'][1]['content'])

    async def _run_clarification_turns(self, turns, replies, completions=None, *, return_events=False):
        """Exercise the real local WS/Opus path with deterministic vendor responses."""
        from types import SimpleNamespace
        class Teacher:
            def __init__(self):
                self.calls=[]
                self.replies=iter(replies)
            def teaching_response(self,text,rid,previous,correction_context=None,**context):
                self.calls.append(json.loads(json.dumps({'text':text,'previous':previous,**context})))
                reply=next(self.replies)
                if isinstance(reply,Exception):
                    raise reply
                return *reply, {'input_tokens':20,'output_tokens':8}
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with wave.open(str(folder/'sample.wav'),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(PCM)
            config=load_cloud_config(require_credentials=False);config['selected_module']={}
            config['english_mvp']['log_path']=str(folder/'events.jsonl')
            teacher=Teacher();server=WebSocketServer(config);server._llm=teacher
            server._asr=SimpleNamespace(close=AsyncMock(),speech_to_text=AsyncMock(
                side_effect=[(text,None) for text in turns]))
            completions=completions or [True]*len(turns)
            with patch('core.connection.initialize_tts',lambda c:self.tts):
                async with websockets.serve(server._handle_connection,'127.0.0.1',0) as listener:
                    ws=await connect(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/',
                                     features={'mvp_playback_completed':True})
                    results=[]
                    try:
                        for complete in completions:
                            result=await one(ws,{'id':'sample'},folder,False,save_audio=False)
                            results.append(result)
                            if result.get('english'):
                                events=[('playback_started','codec_output_returned')]
                                if complete:events.append(('playback_completed','codec_queue_drained'))
                                for event,source in events:
                                    await ws.send(json.dumps({'type':'mvp_playback',
                                        'request_id':result['request_id'],'event':event,'source':source}))
                            await ws.send('{"type":"ping"}')
                            self.assertEqual(json.loads(await ws.recv())['type'],'pong')
                    finally:await ws.close()
            if return_events:
                events=[json.loads(line) for line in (folder/'events.jsonl').read_text().splitlines()]
                return results,teacher.calls,events
            return results,teacher.calls

    async def test_confirmed_clarification_follows_short_answer_once_and_bounds_question_chain(self):
        questions=['你说的是哪个东西？','瓶盖遇到什么问题？','是拧不开还是盖不上？']
        results,calls=await self._run_clarification_turns(
            ['那个怎么弄？','这个瓶盖','弄不了','拧不开','我想喝水'],
            [('clarify',question) for question in questions]+
            [('express',"I can't get this bottle cap off."),('express','I want some water.')])
        self.assertTrue(all(result['success'] for result in results),results)
        self.assertFalse(calls[0].get('clarification_context'))
        self.assertEqual(calls[1]['clarification_context'],{'turns':[
            {'child':'那个怎么弄？','assistant':questions[0]}]})
        self.assertEqual(calls[2]['clarification_context'],{'turns':[
            {'child':'那个怎么弄？','assistant':questions[0]},
            {'child':'这个瓶盖','assistant':questions[1]}]})
        self.assertEqual(calls[3]['clarification_context'],{'turns':[
            {'child':'这个瓶盖','assistant':questions[1]},
            {'child':'弄不了','assistant':questions[2]}]})
        self.assertFalse(calls[4].get('clarification_context'))

    async def test_unfinished_clarification_playback_is_not_context(self):
        results,calls=await self._run_clarification_turns(
            ['那个怎么弄？','这个瓶盖'],
            [('clarify','你说的是哪个东西？'),('express','This bottle cap.')],
            completions=[False,True])
        self.assertTrue(all(result['success'] for result in results),results)
        self.assertFalse(calls[1].get('clarification_context'))

    async def test_mode_switch_clears_pending_clarification(self):
        results,calls=await self._run_clarification_turns(
            ['那个怎么弄？','进入对话陪练模式','这个瓶盖'],
            [('clarify','你说的是哪个东西？'),('chat','What about the cap?')])
        self.assertTrue(all(result['success'] for result in results),results)
        self.assertEqual(calls[1]['mode'],'chat')
        self.assertFalse(calls[1].get('clarification_context'))

    async def test_failed_followup_preserves_confirmed_clarification_for_retry(self):
        results,calls=await self._run_clarification_turns(
            ['那个怎么弄？','这个瓶盖','这个瓶盖'],
            [('clarify','你说的是哪个东西？'),RuntimeError('local_fixture_failure'),
             ('express','How do I open this bottle cap?')])
        self.assertEqual([result['success'] for result in results],[True,False,True])
        self.assertEqual(calls[1]['clarification_context'],calls[2]['clarification_context'])
        self.assertEqual(calls[2]['clarification_context'],{'turns':[
            {'child':'那个怎么弄？','assistant':'你说的是哪个东西？'}]})

    async def test_unfinished_answer_preserves_clarification_for_child_to_retry(self):
        results,calls=await self._run_clarification_turns(
            ['那个怎么弄？','这个瓶盖','这个瓶盖','我饿了'],
            [('clarify','你说的是哪个东西？'),('express','How do I open this bottle cap?'),
             ('express','How do I open this bottle cap?'),('express',"I'm hungry.")],
            completions=[True,False,True,True])
        self.assertTrue(all(result['success'] for result in results),results)
        self.assertEqual(calls[1]['clarification_context'],calls[2]['clarification_context'])
        self.assertEqual(calls[2]['clarification_context'],{'turns':[
            {'child':'那个怎么弄？','assistant':'你说的是哪个东西？'}]})
        self.assertFalse(calls[3].get('clarification_context'))

    async def test_new_topic_can_resolve_without_binding_it_to_previous_question(self):
        results,calls=await self._run_clarification_turns(
            ['那个怎么弄？','算了，我想喝水','我饿了'],
            [('clarify','你说的是哪个东西？'),('express','I want some water.'),
             ('express',"I'm hungry.")])
        self.assertTrue(all(result['success'] for result in results),results)
        self.assertEqual(calls[1]['text'],'算了，我想喝水')
        self.assertEqual(calls[1]['clarification_context']['turns'][0]['child'],'那个怎么弄？')
        self.assertEqual(results[1]['english'],'I want some water.')
        self.assertFalse(calls[2].get('clarification_context'))

    async def test_clarification_expires_after_two_minutes_without_waiting(self):
        from types import SimpleNamespace
        from core.handle.englishMvp import get_clarification_context
        context={'turns':[{'child':'那个怎么弄？','assistant':'你说的是哪个东西？'}]}
        conn=SimpleNamespace(mvp_clarification_context=context,mvp_clarification_at=100.0)
        self.assertEqual(get_clarification_context(conn,now=219.99),context)
        self.assertIsNone(get_clarification_context(conn,now=220.0))
        self.assertIsNone(conn.mvp_clarification_context)
        self.assertIsNone(get_clarification_context(conn,now=221.0))
        conn=SimpleNamespace(mvp_clarification_context=context,mvp_clarification_at=100.0)
        self.assertIsNone(get_clarification_context(conn,now=99.0))

    async def test_unrelated_explanation_keeps_replay_target_and_follows_help_context(self):
        previous='I want some water.'
        question='Say again 是什么意思？'
        explanation='Say again 是“再说一遍”的意思。'
        simpler='就是请别人再说一次。'
        results,calls,events=await self._run_clarification_turns(
            ['我想喝水',question,'说简单一点','再简单一点','再说一遍'],
            [('express',previous),('explain_only',explanation),('explain_only',simpler),
             ('explain_only','请再说一次。')],
            return_events=True)
        self.assertTrue(all(result['success'] for result in results),results)
        self.assertEqual([result['english'] for result in results],
                         [previous,explanation,simpler,'请再说一次。',previous])
        self.assertEqual(calls[2]['help_context'],
                         {'child':question,'assistant':explanation,'replay_previous':False})
        self.assertEqual(calls[3]['help_context'],
                         {'child':'说简单一点','assistant':simpler,'replay_previous':False})
        self.assertEqual(calls[2]['previous'],previous)
        self.assertEqual([event['intent'] for event in events if event['event']=='intent_classified'],
                         ['express','explain_only','explain_only','explain_only','repeat'])
        self.assertFalse(any(event['event']=='explanation_recap' for event in events))
        self.assertEqual(len([request for kind,request in self.requests if kind=='tts']),4)

    async def test_related_explanation_reuses_audio_and_help_context_excludes_recap(self):
        previous='Where is my water bottle?'
        question='water bottle 是什么意思？'
        explanation='water bottle 是水瓶。'
        results,calls,events=await self._run_clarification_turns(
            ['我的水瓶在哪里',question,'再说简单一点'],
            [('express',previous),('explain',explanation),('explain','就是装水的瓶子。')],
            return_events=True)
        self.assertTrue(all(result['success'] for result in results),results)
        self.assertEqual(results[1]['english'],explanation+' '+previous)
        self.assertEqual(calls[2]['help_context'],
                         {'child':question,'assistant':explanation,'replay_previous':True})
        self.assertEqual(calls[2]['previous'],previous)
        recaps=[event for event in events if event['event']=='explanation_recap']
        self.assertEqual(len(recaps),2)
        self.assertTrue(all(event['source']=='cached_played_audio' for event in recaps))

    async def test_started_but_unfinished_explanation_is_not_help_context(self):
        results,calls=await self._run_clarification_turns(
            ['我想喝水','Say again 是什么意思？','说简单一点'],
            [('express','I want some water.'),('explain_only','Say again 是“再说一遍”的意思。'),
             ('explain_only','就是请别人再说一次。')],completions=[True,False,True])
        self.assertTrue(all(result['success'] for result in results),results)
        self.assertFalse(calls[2].get('help_context'))
        self.assertEqual(calls[2]['previous'],'I want some water.')

    async def test_only_confirmed_replay_returns_meaning_target_to_previous_english(self):
        previous='Can I have some more juice?'
        question='Say again 是什么意思？'
        explanation='Say again 是“再说一遍”的意思。'
        simpler='就是请别人再说一次。'
        meaning='就是问能不能再要一点果汁。'
        results,calls=await self._run_clarification_turns(
            ['我能再要一点果汁吗',question,'再说一遍','再简单一点','再说一遍','什么意思'],
            [('express',previous),('explain_only',explanation),
             ('explain_only',simpler),('explain',meaning)],
            completions=[True,True,False,True,True,True])
        self.assertTrue(all(result['success'] for result in results),results)
        self.assertEqual([result['english'] for result in results],
                         [previous,explanation,previous,simpler,previous,meaning+' '+previous])
        self.assertEqual(calls[2]['help_context'],
                         {'child':question,'assistant':explanation,'replay_previous':False})
        self.assertEqual(calls[2]['previous'],previous)
        self.assertFalse(calls[3].get('help_context'))
        self.assertEqual(calls[3]['previous'],previous)

    async def test_failed_and_unfinished_help_preserve_last_confirmed_explanation(self):
        first={'child':'Say again 是什么意思？','assistant':'Say again 是“再说一遍”的意思。',
               'replay_previous':False}
        results,calls=await self._run_clarification_turns(
            [first['child'],'再简单点','说简单点','用更简单的话说','再简单一点'],
            [('explain_only',first['assistant']),RuntimeError('local_fixture_failure'),
             ('explain_only','就是请别人重复。'),('explain_only','请别人再说一次。'),
             ('explain_only','再说一次。')],completions=[True,True,False,True,True])
        self.assertEqual([result['success'] for result in results],[True,False,True,True,True])
        for call in calls[1:4]:self.assertEqual(call['help_context'],first)
        self.assertEqual(calls[4]['help_context'],
                         {'child':'用更简单的话说','assistant':'请别人再说一次。','replay_previous':False})

    async def test_mode_switch_clears_help_before_mode_announcement_finishes(self):
        results,calls=await self._run_clarification_turns(
            ['Say again 是什么意思？','进入对话陪练模式','你好'],
            [('explain_only','Say again 是“再说一遍”的意思。'),('chat','Hello!')],
            completions=[True,False,True])
        self.assertTrue(all(result['success'] for result in results),results)
        self.assertEqual(calls[1]['mode'],'chat')
        self.assertFalse(calls[1].get('help_context'))

    async def test_confirmed_new_expression_clears_help_but_unfinished_expression_does_not(self):
        help_context={'child':'Say again 是什么意思？','assistant':'Say again 是“再说一遍”的意思。',
                      'replay_previous':False}
        results,calls=await self._run_clarification_turns(
            [help_context['child'],'我想喝水','再简单一点','我想喝水','再简单一点'],
            [('explain_only',help_context['assistant']),('express','I want some water.'),
             ('explain_only','请再说一次。'),('express','I want some water.'),
             ('express','Please say it more simply.')],
            completions=[True,False,True,True,True])
        self.assertTrue(all(result['success'] for result in results),results)
        self.assertFalse(calls[1].get('help_context'))
        self.assertEqual(calls[2]['help_context'],help_context)
        self.assertFalse(calls[3].get('help_context'))
        self.assertFalse(calls[4].get('help_context'))

    async def test_help_expires_after_two_minutes_without_waiting(self):
        from types import SimpleNamespace
        from core.handle.englishMvp import get_help_context
        context={'child':'Say again 是什么意思？','assistant':'Say again 是“再说一遍”的意思。',
                 'replay_previous':False}
        conn=SimpleNamespace(mvp_help_context=context,mvp_help_at=100.0)
        self.assertEqual(get_help_context(conn,now=219.99),context)
        self.assertIsNone(get_help_context(conn,now=220.0))
        self.assertIsNone(conn.mvp_help_context)
        self.assertIsNone(get_help_context(conn,now=221.0))
        conn=SimpleNamespace(mvp_help_context=context,mvp_help_at=100.0)
        self.assertIsNone(get_help_context(conn,now=99.0))

    async def test_late_playback_ack_after_failure_or_abort_cannot_commit_help(self):
        from types import SimpleNamespace
        from core.handle.englishMvp import handle_message, confirm_playback
        previous={'child':'some more 是什么意思？','assistant':'就是再多一点。','replay_previous':True}
        pending={'child':'说简单一点','assistant':'再要一点。','replay_previous':False}
        cases=[
            ('failed_completion',True,False,False,True,'playback_completed','codec_queue_drained'),
            ('aborted_completion',True,True,True,True,'playback_completed','codec_queue_drained'),
            ('aborted_legacy_start',False,True,True,False,'playback_started','codec_output_returned'),
        ]
        for name,requires_completion,returning,aborted,reported,ack,source in cases:
            with self.subTest(name=name):
                conn=SimpleNamespace(mvp_request_id='request',mvp_requires_completion=requires_completion,
                    mvp_audio_returning=returning,client_abort=aborted,mvp_playback_reported=reported,
                    mvp_playback_completed=False,mvp_help_context=previous,mvp_pending_help_context=pending,
                    mvp_pending_intent='explain_only',mvp_pending_english=pending['assistant'],mvp_started=0.0)
                with patch('core.handle.englishMvp.event') as logged, \
                     patch('core.handle.englishMvp.confirm_playback',wraps=confirm_playback) as confirmed:
                    await handle_message(conn,json.dumps({'type':'mvp_playback','request_id':'request',
                                                          'event':ack,'source':source}))
                confirmed.assert_not_called()
                logged.assert_not_called()
                self.assertIs(conn.mvp_help_context,previous)
                self.assertFalse(conn.mvp_playback_completed)

    async def test_new_hello_clears_help_context(self):
        from types import SimpleNamespace
        from core.handle.englishMvp import handle_message
        conn=SimpleNamespace(mvp_request_id=None,mvp_help_context={
            'child':'some more 是什么意思？','assistant':'就是再多一点。','replay_previous':True},
            welcome_msg={'type':'hello'},websocket=SimpleNamespace(send=AsyncMock()))
        await handle_message(conn,json.dumps({'type':'hello','version':1,
            'audio_params':{'format':'opus','sample_rate':16000,'channels':1,'frame_duration':60},
            'features':{'mvp_playback_completed':True}}))
        self.assertIsNone(conn.mvp_help_context)
        self.assertTrue(conn.mvp_requires_completion)
        conn.websocket.send.assert_awaited_once()

    async def test_tts_pcm_wrapping_usage_and_keepalive(self):
        client=self.tts.client
        for i in range(2):
            data=await self.tts.text_to_speak(ENGLISH,None)
            with wave.open(io.BytesIO(data)) as f:
                self.assertEqual((f.getframerate(),f.getnchannels(),f.getsampwidth()),(24000,1,2))
                self.assertEqual(f.readframes(f.getnframes()),PCM)
        self.assertIs(self.tts.client,client)
        self.assertEqual(self.tts.last_metrics['billed_text_words'],len(ENGLISH))

    async def test_explanation_audio_reuses_exact_previously_played_samples(self):
        from core.handle.englishMvp import append_played_wav
        def wav_of(pcm):
            output=io.BytesIO()
            with wave.open(output,'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
                wav.writeframes(pcm)
            return output.getvalue()
        first=b'\x01\x00'*2400
        previous=b'\x02\x00'*4800
        joined=append_played_wav(wav_of(first),wav_of(previous))
        with wave.open(io.BytesIO(joined),'rb') as wav:
            self.assertEqual(wav.readframes(wav.getnframes()),
                             first+b'\0\0'*4800+previous)

    async def test_tts_slow_rate_is_sent_to_official_http_contract(self):
        await self.tts.text_to_speak('Can I have some more juice?',None,speech_rate=-25)
        self.assertEqual(self.requests[-1][1]['req_params']['audio_params']['speech_rate'],-25)
        with self.assertRaisesRegex(ValueError,'invalid_speech_rate'):
            await self.tts.text_to_speak('A sentence.',None,speech_rate=-51)

    async def test_tts_incomplete_and_credential_safe_error_then_recovers(self):
        await self.tts.client.aclose()
        class Truncated(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (json.dumps({'code':0,'data':base64.b64encode(PCM).decode()})+'\n').encode()
                raise httpx.ReadError('truncated chunked body')
        self.tts.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200,stream=Truncated())))
        with self.assertRaisesRegex(RuntimeError,'seed_tts_failed:ReadError'):
            await self.tts.text_to_speak(ENGLISH,None)
        await self.tts.client.aclose()
        self.tts.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(401,json={'error':'test-secret'})))
        with self.assertRaises(RuntimeError) as caught:await self.tts.text_to_speak(ENGLISH,None)
        self.assertNotIn('test-secret',str(caught.exception))
        await self.tts.client.aclose()
        self.tts.client=httpx.AsyncClient(transport=httpx.MockTransport(self.tts_response))
        self.assertTrue(await self.tts.text_to_speak(ENGLISH,None))

    async def test_tts_current_zero_success_complete_http_body(self):
        await self.tts.client.aclose()
        self.tts.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200,content=json.dumps({'code':0,'data':base64.b64encode(PCM).decode(),
                                                 'usage':{'text_words':61}})+'\n')))
        self.assertTrue(await self.tts.text_to_speak(ENGLISH,None))

    async def test_asr_all_header_variants_and_bounds(self):
        for flags in range(4):
            for compressed in (True,False):
                r=self.asr.parse_response(response_packet({'result':{'text':RAW}},flags,compressed))
                self.assertEqual(r['payload_msg']['result']['text'],RAW)
                self.assertEqual(r['last'],bool(flags&2))
        error=self.asr.parse_response(response_packet({'message':'denied'},0,False,15))
        self.assertEqual(error['code'],45000001)
        for invalid in (b'',b'bad input',response_packet({})[:-1]):
            with self.assertRaises(ValueError):self.asr.parse_response(invalid)
        with self.assertRaisesRegex(ValueError,'too_large'):
            self.asr.parse_response(response_packet({'x':'a'*(1024*1024)}))

    async def test_asr_pcm_order_final_result_and_language(self):
        received=[]
        async def vendor(ws):
            self.assertEqual(ws.request.headers['X-Api-Resource-Id'],'volc.seedasr.sauc.duration')
            full=await ws.recv(); payload=json.loads(gzip.decompress(full[8:]))
            self.assertNotIn('language',payload['audio'])
            self.assertEqual(payload['audio']['codec'],'raw')
            self.assertFalse(payload['request']['enable_ddc']);self.assertFalse(payload['request']['enable_itn'])
            async for message in ws:
                received.append(gzip.decompress(message[8:]))
                if message[1]&2:
                    await ws.send(response_packet({'result':{'text':'unfinished'}},flags=1))
                    await ws.send(response_packet({'result':{'text':RAW}}));break
        async with websockets.serve(vendor,'127.0.0.1',0) as server:
            self.asr.ws_url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            text,usage=await self.asr.speech_to_text([PCM,PCM], 'rid')
        self.assertEqual(b''.join(received),PCM+PCM);self.assertEqual(text,RAW)
        self.assertEqual(usage['input_audio_seconds'],len(PCM+PCM)/32000)

    async def test_asr_timeout_is_bounded_and_next_turn_recovers(self):
        async def stalled(ws):
            async for _ in ws:pass
        async with websockets.serve(stalled,'127.0.0.1',0) as server:
            self.asr.ws_url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            self.asr.timeout_s=.1
            with self.assertRaisesRegex(RuntimeError,'TimeoutError'):
                await self.asr.speech_to_text([PCM],'rid')
        self.asr.timeout_s=25
        await self.test_asr_pcm_order_final_result_and_language()

    async def test_real_xiaozhi_ws_opus_failure_then_complete_turn(self):
        attempt=0
        async def vendor(ws):
            nonlocal attempt
            attempt+=1
            if attempt==1:
                await ws.recv();await ws.send(response_packet({'message':'test-secret'},0,False,15));return
            await ws.recv()
            async for packet in ws:
                if packet[1]&2:
                    await ws.send(response_packet({'result':{'text':RAW}}));return
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            with wave.open(str(folder/'sample.wav'),'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(PCM)
            config=load_cloud_config(require_credentials=False)
            config['selected_module']={}
            config['english_mvp']['log_path']=str(folder/'events.jsonl')
            server=WebSocketServer(config);server._asr=self.asr;server._llm=self.llm
            async with websockets.serve(vendor,'127.0.0.1',0) as vendor_server:
                self.asr.ws_url=f'ws://127.0.0.1:{vendor_server.sockets[0].getsockname()[1]}'
                with patch('core.connection.initialize_tts',lambda c:self.tts):
                    async with websockets.serve(server._handle_connection,'127.0.0.1',0) as listener:
                        ws=await connect(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/')
                        try:
                            first=await one(ws,{'id':'sample'},folder,False,returned_dir=folder/'returned')
                            self.assertFalse(first['success']);self.assertNotIn('test-secret',first['error'])
                            second=await one(ws,{'id':'sample'},folder,False,returned_dir=folder/'returned')
                            self.assertTrue(second['success'],second)
                            self.assertEqual(second['asr'],RAW);self.assertEqual(second['english'],ENGLISH)
                            events=[json.loads(x) for x in (folder/'events.jsonl').read_text().splitlines()]
                            turn=[x for x in events if x.get('request_id')==second['request_id']]
                            for name in ('asr_usage','llm_usage','tts_usage','audio_sent','request_complete'):
                                self.assertTrue(any(x['event']==name for x in turn),name)
                        finally:await ws.close()

    async def test_mixed_expression_then_exact_and_semantic_repeat_on_one_device(self):
        from types import SimpleNamespace
        from core.handle.englishMvp import is_exact_repeat_request
        self.assertTrue(is_exact_repeat_request('你刚才说什么？'))
        self.assertTrue(is_exact_repeat_request('Say again!'))
        self.assertFalse(is_exact_repeat_request('他说 say again'))
        self.assertFalse(is_exact_repeat_request('我没听懂'))
        self.assertFalse(is_exact_repeat_request('Say again 是什么意思？'))
        class Teacher:
            def __init__(self):self.calls=[]
            def teaching_response(self,text,rid,previous):
                self.calls.append((text,previous))
                if len(self.calls)==1:return 'express','Where is my water bottle?',{'input_tokens':30,'output_tokens':8}
                if len(self.calls)==2:return 'repeat','',{'input_tokens':30,'output_tokens':3}
                return 'explain','water bottle 是水瓶。',{'input_tokens':30,'output_tokens':8}
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with wave.open(str(folder/'sample.wav'),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(PCM)
            config=load_cloud_config(require_credentials=False);config['selected_module']={}
            config['english_mvp']['log_path']=str(folder/'events.jsonl')
            teacher=Teacher();server=WebSocketServer(config);server._llm=teacher
            server._asr=SimpleNamespace(close=AsyncMock(),speech_to_text=AsyncMock(side_effect=[
                ('Say again!',None),
                ('妈妈，我的 water bottle 在哪里？',None),
                ('Say again!',None),
                ('我刚才那句没听懂，能再讲一遍吗？',None),
                ('water bottle 是什么意思？',None),
                ('慢一点。',None)]))
            with patch('core.connection.initialize_tts',lambda c:self.tts):
                async with websockets.serve(server._handle_connection,'127.0.0.1',0) as listener:
                    ws=await connect(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/')
                    try:
                        results=[]
                        for _ in range(6):
                            result=await one(ws,{'id':'sample'},folder,False,save_audio=False)
                            results.append(result)
                            if result.get('english'):
                                await ws.send(json.dumps({'type':'mvp_playback','request_id':result['request_id'],
                                    'event':'playback_started','source':'codec_output_returned'}))
                                await ws.send('{"type":"ping"}')
                                self.assertEqual(json.loads(await ws.recv())['type'],'pong')
                    finally:await ws.close()
            self.assertTrue(all(item['success'] for item in results),results)
            self.assertEqual([item.get('english') for item in results],
                             ["I don't have a sentence to repeat yet."]+['Where is my water bottle?']*3+
                             ['water bottle 是水瓶。 Where is my water bottle?',
                              'Where is my water bottle?'])
            self.assertEqual(len(teacher.calls),3)
            self.assertEqual(teacher.calls[1][1],'Where is my water bottle?')
            self.assertEqual(teacher.calls[2][1],'Where is my water bottle?')
            events=[json.loads(line) for line in (folder/'events.jsonl').read_text().splitlines()]
            self.assertEqual([item['intent'] for item in events if item['event']=='intent_classified'],
                             ['repeat','express','repeat','slow','explain','slow'])
            self.assertEqual(len([item for item in events if item['event']=='help_adjusted']),1)
            self.assertTrue(any(item['event']=='explanation_recap' and
                                item['source']=='cached_played_audio' for item in events))
            self.assertEqual([item['success'] for item in events if item['event']=='request_complete'][-1],True)
            tts_requests=[request for kind,request in self.requests if kind=='tts']
            self.assertEqual(len(tts_requests),5)  # First replays the WAV; second is slower.
            self.assertEqual(tts_requests[-3]['req_params']['audio_params']['speech_rate'],-25)
            self.assertEqual(tts_requests[-2]['req_params']['speaker'],self.tts.help_speaker)
            self.assertEqual(tts_requests[-1]['req_params']['audio_params']['speech_rate'],-25)
            self.assertEqual(tts_requests[-1]['req_params']['speaker'],self.tts.speaker)

    async def test_conversation_mode_is_explicit_and_replay_uses_heard_chat_turn(self):
        from types import SimpleNamespace
        class Teacher:
            def __init__(self):self.calls=[]
            def teaching_response(self,text,rid,previous,*,mode='express',dialogue_context=None):
                self.calls.append((text,mode,dialogue_context))
                if mode == 'chat':
                    return 'chat','What would you like to eat?',{'input_tokens':20,'output_tokens':8}
                return 'express',"I'm hungry.",{'input_tokens':20,'output_tokens':6}
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with wave.open(str(folder/'sample.wav'),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(PCM)
            config=load_cloud_config(require_credentials=False);config['selected_module']={}
            config['english_mvp']['log_path']=str(folder/'events.jsonl')
            teacher=Teacher();server=WebSocketServer(config);server._llm=teacher
            server._asr=SimpleNamespace(close=AsyncMock(),speech_to_text=AsyncMock(side_effect=[
                ('进入对话陪练模式',None),('我饿了',None),('再说一遍',None),
                ('回到生活表达模式',None),('我饿了',None)]))
            with patch('core.connection.initialize_tts',lambda c:self.tts):
                async with websockets.serve(server._handle_connection,'127.0.0.1',0) as listener:
                    ws=await connect(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/')
                    try:
                        results=[]
                        for _ in range(5):
                            result=await one(ws,{'id':'sample'},folder,False,save_audio=False)
                            results.append(result)
                            await ws.send(json.dumps({'type':'mvp_playback',
                                'request_id':result['request_id'],'event':'playback_started',
                                'source':'codec_output_returned'}))
                            await ws.send('{"type":"ping"}')
                            self.assertEqual(json.loads(await ws.recv())['type'],'pong')
                    finally:await ws.close()
            self.assertEqual([result['english'] for result in results],
                ["Let's chat in English.",'What would you like to eat?',
                 'What would you like to eat?','Back to everyday English.',"I'm hungry."])
            self.assertEqual([(call[0],call[1]) for call in teacher.calls],
                             [('我饿了','chat'),('我饿了','express')])
            events=[json.loads(line) for line in (folder/'events.jsonl').read_text().splitlines()]
            self.assertEqual([row['intent'] for row in events if row['event']=='intent_classified'],
                             ['mode','chat','repeat','mode','express'])

    async def test_spoken_stop_is_expression_not_device_control(self):
        from types import SimpleNamespace
        class Teacher:
            def teaching_response(self, text, rid, previous):
                return 'express', 'Stop.', {'input_tokens': 20, 'output_tokens': 3}
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with wave.open(str(folder/'sample.wav'),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(PCM)
            config=load_cloud_config(require_credentials=False);config['selected_module']={}
            config['english_mvp']['log_path']=str(folder/'events.jsonl')
            server=WebSocketServer(config);server._llm=Teacher()
            server._asr=SimpleNamespace(close=AsyncMock(),speech_to_text=AsyncMock(return_value=('停',None)))
            with patch('core.connection.initialize_tts',lambda c:self.tts):
                async with websockets.serve(server._handle_connection,'127.0.0.1',0) as listener:
                    ws=await connect(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/',
                                     features={'mvp_control_pause':True})
                    try:result=await one(ws,{'id':'sample'},folder,False,save_audio=False)
                    finally:await ws.close()
            self.assertTrue(result['success'])
            self.assertEqual(result['english'],'Stop.')
            self.assertGreater(result['output_frames'],0)
            self.assertNotIn('controls',result)

    async def test_new_device_only_repeats_after_matching_playback_completion(self):
        from types import SimpleNamespace
        class Teacher:
            def teaching_response(self,text,rid,previous):
                return 'express','Can I have some more juice?',{'input_tokens':20,'output_tokens':8}
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with wave.open(str(folder/'sample.wav'),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(PCM)
            config=load_cloud_config(require_credentials=False);config['selected_module']={}
            config['english_mvp']['log_path']=str(folder/'events.jsonl')
            server=WebSocketServer(config);server._llm=Teacher()
            server._asr=SimpleNamespace(close=AsyncMock(),speech_to_text=AsyncMock(side_effect=[
                ('我还想喝点果汁',None),('再说一遍',None),('我还想喝点果汁',None),
                ('再说一遍',None),('再说一遍',None)]))
            with patch('core.connection.initialize_tts',lambda c:self.tts):
                async with websockets.serve(server._handle_connection,'127.0.0.1',0) as listener:
                    ws=await connect(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/',
                                     features={'mvp_playback_completed':True})
                    try:
                        first=await one(ws,{'id':'sample'},folder,False,save_audio=False)
                        started={'type':'mvp_playback','request_id':first['request_id'],
                                 'event':'playback_started','source':'codec_output_returned'}
                        await ws.send(json.dumps(started))
                        await ws.send('{"type":"ping"}')
                        self.assertEqual(json.loads(await ws.recv())['type'],'pong')
                        unheard=await one(ws,{'id':'sample'},folder,False,save_audio=False)
                        second=await one(ws,{'id':'sample'},folder,False,save_audio=False)
                        await ws.send(json.dumps({**started,'event':'playback_completed',
                            'source':'codec_queue_drained','request_id':first['request_id']}))
                        await ws.send(json.dumps({**started,'request_id':second['request_id']}))
                        await ws.send(json.dumps({**started,'event':'playback_completed',
                            'source':'codec_queue_drained','request_id':second['request_id']}))
                        await ws.send(json.dumps({**started,'event':'playback_completed',
                            'source':'codec_queue_drained','request_id':second['request_id']}))
                        await ws.send('{"type":"ping"}')
                        self.assertEqual(json.loads(await ws.recv())['type'],'pong')
                        heard=await one(ws,{'id':'sample'},folder,False,save_audio=False)
                        unconfirmed_repeat=await one(ws,{'id':'sample'},folder,False,save_audio=False)
                    finally:await ws.close()
            self.assertEqual(unheard['english'],"I don't have a sentence to repeat yet.")
            self.assertEqual(heard['english'],'Can I have some more juice?')
            self.assertEqual(unconfirmed_repeat['english'],'Can I have some more juice?')
            rows=[json.loads(line) for line in (folder/'events.jsonl').read_text().splitlines()]
            self.assertEqual(len([row for row in rows if row['event']=='playback_confirmed']),1)
            self.assertEqual(len([row for row in rows if row['event']=='playback_completed']),1)
            self.assertFalse(any(row['event']=='help_adjusted' for row in rows))

    async def test_correction_question_followup_and_same_turn_repair(self):
        from types import SimpleNamespace
        class Teacher:
            def __init__(self):self.calls=[]
            def teaching_response(self,text,rid,previous,correction_context=None):
                self.calls.append((text,previous,correction_context))
                if len(self.calls)==1:
                    return 'express','Where is my red hat?',{'input_tokens':20,'output_tokens':8}
                if len(self.calls)==2:
                    return 'express','Where is my blue hat?',{'input_tokens':25,'output_tokens':8}
                return 'repair','Where is my blue coat?',{'input_tokens':25,'output_tokens':8}
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with wave.open(str(folder/'sample.wav'),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(PCM)
            config=load_cloud_config(require_credentials=False);config['selected_module']={}
            config['english_mvp']['log_path']=str(folder/'events.jsonl')
            teacher=Teacher();server=WebSocketServer(config);server._llm=teacher
            server._asr=SimpleNamespace(close=AsyncMock(),speech_to_text=AsyncMock(side_effect=[
                ('我的帽子在哪里？',None),('不是这个意思',None),('是蓝色的帽子',None),
                ('不对，我是说我的蓝色外套在哪里',None),('再说一遍',None)]))
            with patch('core.connection.initialize_tts',lambda c:self.tts):
                async with websockets.serve(server._handle_connection,'127.0.0.1',0) as listener:
                    ws=await connect(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/')
                    try:
                        results=[]
                        for _ in range(5):
                            result=await one(ws,{'id':'sample'},folder,False,save_audio=False)
                            results.append(result)
                            await ws.send(json.dumps({'type':'mvp_playback','request_id':result['request_id'],
                                'event':'playback_started','source':'codec_output_returned'}))
                            await ws.send('{"type":"ping"}')
                            self.assertEqual(json.loads(await ws.recv())['type'],'pong')
                    finally:await ws.close()
            self.assertEqual([item['english'] for item in results],
                             ['Where is my red hat?','你原本想表达什么？',
                              'Where is my blue hat?','Where is my blue coat?',
                              'Where is my blue coat?'])
            self.assertEqual(teacher.calls[1][1],None)
            self.assertEqual(teacher.calls[1][2],
                             {'transcript':'我的帽子在哪里？','rejected_english':'Where is my red hat?'})
            self.assertEqual(teacher.calls[2][1],'Where is my blue hat?')
            rows=[json.loads(line) for line in (folder/'events.jsonl').read_text().splitlines()]
            self.assertEqual([row['intent'] for row in rows if row['event']=='intent_classified'],
                             ['express','correct','express','express','repeat'])
            self.assertEqual(len([row for row in rows if row['event']=='expression_rejected']),2)
            self.assertEqual(len([row for row in rows if row['event']=='correction_applied']),1)

    async def test_no_local_model_modules_imported(self):
        for name in ('torch','mlx','mlx_whisper','kokoro','onnxruntime','core.providers.local_worker'):
            self.assertNotIn(name,sys.modules)

    async def test_cloud_replay_uses_auth_budget_and_billing_without_audio_files(self):
        import hashlib
        from cloud_replay import replay
        from cloud_service import CloudPolicy, attach_policy
        from cloud_report import billing_report
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with wave.open(str(folder/'sample.wav'),'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(PCM)
            original=(folder/'sample.wav').read_bytes()
            registry=folder/'devices.json'
            registry.write_text(json.dumps({'replay':hashlib.sha256(b'test-token').hexdigest()}));registry.chmod(0o600)
            env=dict(ASR_CNY_PER_HOUR='1',LLM_INPUT_CNY_PER_MILLION='.2',LLM_OUTPUT_CNY_PER_MILLION='2',
                     TTS_CNY_PER_10K_CHARS='3',DAILY_CNY_LIMIT='1',DAILY_TURN_LIMIT='1')
            with patch.dict(os.environ,env):policy=CloudPolicy(folder/'state',registry,live=True)
            config=load_cloud_config(require_credentials=False);config['selected_module']={}
            config['english_mvp']['log_path']=str(folder/'unused.jsonl')
            server=attach_policy(WebSocketServer(config),policy);server._asr=self.asr;server._llm=self.llm
            self.asr.speech_to_text=AsyncMock(return_value=(RAW,{'input_audio_seconds':.3}))
            self.asr.supports_input_streaming=False  # This replay fixture exercises the final-input fallback.
            with patch('core.connection.initialize_tts',lambda c:self.tts):
                async with websockets.serve(server._handle_connection,'127.0.0.1',0,process_request=server._http_response) as listener:
                    url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/'
                    summary=await replay(url,{'device_id':'replay','token':'test-token'},
                        [{'id':'sample','transcript':RAW}]*3,folder,folder/'results',realtime=False)
            self.assertEqual(summary['attempted'],2);self.assertEqual(summary['pipeline_successes'],1)
            self.assertEqual(self.asr.speech_to_text.await_count,1)
            self.assertFalse((folder/'results/generated-audio').exists())
            self.assertEqual((folder/'sample.wav').read_bytes(),original)
            results=[json.loads(l) for l in (folder/'results/results.jsonl').read_text().splitlines()]
            self.assertEqual(results[0]['asr'],RAW);self.assertEqual(results[0]['reference_transcript'],RAW)
            self.assertFalse(results[0]['hardware']);self.assertEqual(results[0]['quality_review'],'pending')
            bill=billing_report(policy.db_path)
            self.assertEqual(bill['recent'][0]['request_id'],results[0]['request_id'])
            for stage in ('asr','english','tts'):self.assertEqual(bill['stages'][stage]['calls'],1)
            self.assertGreater(bill['estimated_total_cny'],0)

    async def test_cloud_wav_opus_path_does_not_require_ffmpeg(self):
        from pydub import AudioSegment
        from core.utils.util import audio_bytes_to_data_stream
        import opuslib_next
        wav=await self.tts.text_to_speak(ENGLISH,None)
        packets=[]
        with patch.object(AudioSegment,'converter','/missing-ffmpeg-intentionally'):
            audio_bytes_to_data_stream(wav,'wav',True,packets.append,24000)
        decoder=opuslib_next.Decoder(24000,1)
        self.assertTrue(packets)
        self.assertTrue(all(len(decoder.decode(packet,1440))==2880 for packet in packets))


if __name__ == '__main__':unittest.main()
