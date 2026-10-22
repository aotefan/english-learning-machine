"""Local protocol/SDK tests. No acoustic or production latency claims."""
import asyncio,gzip,json,struct,unittest
from unittest.mock import patch
import httpx
import tests.test_doubao_cloud as cloud
from core.utils.teaching_stream import english_prefix
from core.utils.speech_sentences import SpeechSentences
from core.providers.tts import huoshan_double_stream as protocol


def server_packet(event, sid, payload=b'{}', audio=False):
    if isinstance(payload,dict):payload=json.dumps(payload).encode()
    return bytes([0x11,0xb4 if audio else 0x94,0x00 if audio else 0x10,0])+struct.pack('>iI',event,len(sid))+sid.encode()+struct.pack('>I',len(payload))+payload


class BidirectionalTests(unittest.IsolatedAsyncioTestCase):
    def test_sentence_chunks_keep_complete_meaning_and_fragmented_numbers(self):
        splitter = SpeechSentences()
        chunks = []
        for delta in ['Mr. ', 'Green has 2.', '5 cups. ', 'Can I have ', 'one? ', 'Please']:
            chunks.extend(splitter.feed(delta))
        self.assertEqual(chunks, ['Mr. Green has 2.5 cups. ', 'Can I have one? '])
        chunks.extend(splitter.finish())
        self.assertEqual(chunks[-1], 'Please')
        self.assertEqual(''.join(chunks), 'Mr. Green has 2.5 cups. Can I have one? Please')

    def test_json_prefix_handles_fragmented_escapes_and_blocks_controls(self):
        raw='{"intent":"express","english":"I want \\"a pen\\".'
        self.assertEqual(english_prefix(raw,'express'),'I want "a pen".')
        self.assertEqual(english_prefix('{"intent":"express","english":"Hi \\u00','express'),'Hi ')
        self.assertEqual(english_prefix('{"intent":"no_change","english":"','express'),'')
        self.assertEqual(english_prefix('{"intent":"explain","english":"解释','express'),'')
        self.assertEqual(english_prefix('{"intent":"chat","english":"Hi','express'),'')
        with self.assertRaises(ValueError):english_prefix('{"intent":"express","english":"中文','express')

    async def test_actual_ark_sdk_streams_english_before_json_completion(self):
        pieces=['{"intent":"express",','"english":"I want ', 'some paper.', '"}']
        events=[]
        def reply(request):
            body=json.loads(request.content);self.assertTrue(body['stream'])
            self.assertEqual(body['stream_options'],{'include_usage':True})
            lines=[]
            for piece in pieces:
                lines.append('data: '+json.dumps({'id':'fixture','object':'chat.completion.chunk',
                    'created':0,'model':'fixture','choices':[{'index':0,'delta':{'content':piece},'finish_reason':None}]}))
            lines.append('data: '+json.dumps({'id':'fixture','object':'chat.completion.chunk','created':0,
                'model':'fixture','choices':[{'index':0,'delta':{},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':12,'completion_tokens':9,'total_tokens':21}}))
            lines.append('data: [DONE]')
            return httpx.Response(200,content='\n\n'.join(lines)+'\n\n',headers={'content-type':'text/event-stream'})
        llm=cloud.LLMProvider({'api_key':'fixture','model_name':'fixture','streaming':True})
        llm.client.close();llm.client=cloud.Ark(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(reply)))
        try:
            result=await asyncio.to_thread(llm.teaching_response,'我想要一些纸。','fixture',on_text=events.append)
            self.assertEqual(events,['I want ','some paper.'])
            self.assertEqual(result[:2],('express','I want some paper.'))
            self.assertEqual(result[2]['output_tokens'],9)
        finally:llm.close()

    async def test_tts_receives_audio_before_text_finish_and_reuses_sequential_sessions(self):
        sockets=[];sessions=[];text_finished=asyncio.Event();first_audio=asyncio.Event()
        async def serve(ws):
            sockets.append(ws);active=None
            async for packet in ws:
                event=struct.unpack('>i',packet[4:8])[0];offset=8;sid=''
                if event!=1:
                    size=struct.unpack('>I',packet[offset:offset+4])[0];offset+=4
                    sid=packet[offset:offset+size].decode();offset+=size
                size=struct.unpack('>I',packet[offset:offset+4])[0];offset+=4
                body=json.loads(packet[offset:offset+size])
                if event==1:await ws.send(server_packet(50,'fixture-connection'))
                elif event==100:
                    self.assertIsNone(active);active=sid;sessions.append(sid)
                    self.assertEqual(body['req_params']['audio_params']['format'],'pcm')
                    await ws.send(server_packet(150,sid))
                elif event==200:
                    self.assertEqual(sid,active)
                    await ws.send(server_packet(352,sid,b'\x01\x00'*1440,audio=True))
                elif event==102:
                    await ws.send(server_packet(152,sid,{'usage':{'text_words':8}}));active=None
        original=cloud.websockets.connect
        async with cloud.websockets.serve(serve,'127.0.0.1',0) as server:
            address=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            def local(url,**kwargs):
                self.assertEqual(url,'wss://openspeech.bytedance.com/api/v3/tts/bidirection')
                self.assertEqual(kwargs['additional_headers']['X-Api-Resource-Id'],'seed-tts-2.0')
                return original(address,**kwargs)
            tts=cloud.TTSProvider({'api_key':'fixture','speaker':'fixture','bidirectional':True})
            async def source():
                yield 'I want paper.'
                await asyncio.wait_for(first_audio.wait(),2)
                self.assertFalse(text_finished.is_set())
                yield ' Please.'
                text_finished.set()
            async def on_pcm(chunk):first_audio.set()
            with patch.object(cloud.websockets,'connect',side_effect=local):
                try:
                    first=await tts.speak_incremental(source(),on_pcm)
                    self.assertTrue(first.startswith(b'RIFF'))
                    async def again():yield 'Hello.'
                    await tts.speak_incremental(again(),on_pcm)
                    self.assertEqual(len(sockets),1);self.assertEqual(len(sessions),2)
                    self.assertEqual(tts.last_metrics['billed_text_words'],8)
                finally:await tts.close()

    async def test_asr_partial_revision_is_not_appended_and_arrives_before_stop(self):
        partial=asyncio.Event();updates=[];audio=[]
        async def serve(ws):
            await ws.recv()
            async for packet in ws:
                flags=packet[1]&15
                payload=gzip.decompress(packet[8:]);audio.append(payload)
                if not flags:
                    await ws.send(cloud.response_packet({'result':{'text':'一张',
                        'utterances':[{'start_time':0,'end_time':200,'text':'一张','definite':False}]}},flags=1))
                else:
                    await ws.send(cloud.response_packet({'result':{'text':'一张纸。',
                        'utterances':[{'start_time':0,'end_time':400,'text':'一张纸。','definite':True}]}},flags=3))
        original=cloud.websockets.connect
        async with cloud.websockets.serve(serve,'127.0.0.1',0) as server:
            asr=cloud.ASRProvider({'api_key':'fixture'})
            self.assertTrue(asr.ws_url.endswith('bigmodel_async'))
            asr.ws_url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            with patch.object(cloud.websockets,'connect',side_effect=lambda url,**kwargs:original(asr.ws_url,**kwargs)):
                stream=asr.start_stream('fixture')
                async def update(row):updates.append(row);partial.set()
                stream.on_update=update
                try:
                    pcm=b'\x01\x00'*6400;stream.feed(pcm)
                    await asyncio.wait_for(partial.wait(),2)
                    self.assertFalse(stream.finished);self.assertFalse(stream.task.done())
                    stream.finish();text,usage=await stream.task
                    self.assertEqual(text,'一张纸。');self.assertEqual(b''.join(audio),pcm)
                    self.assertEqual(updates[-1]['definite_segments'],1)
                    self.assertEqual(usage['input_audio_seconds'],.4)
                    self.assertIsNotNone(usage['first_result_ms'])
                finally:await stream.close()

# Use existing connection/budget fixture helpers; these assertions are about
# scheduling and cancellation, not model semantics or acoustic behavior.
import tests.test_cloud_tts_streaming as fixtures
import io,threading,wave
from core.handle import englishMvp

class IncrementalPipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.StreamingTests.asyncSetUp
    asyncTearDown=fixtures.StreamingTests.asyncTearDown
    connection=fixtures.StreamingTests.connection
    rows=fixtures.StreamingTests.rows

    async def test_first_audio_precedes_llm_completion_and_budget_records_one_tts(self):
        conn=self.connection();first=threading.Event();final=threading.Event();sent=[]
        class IncrementalTTS:
            supports_incremental_input=True;supports_pcm_streaming=True
            last_metrics={}
            async def speak_incremental(self,source,on_pcm):
                pcm=bytearray()
                async for delta in source:
                    sent.append(delta);pcm.extend(b'\x30\x00'*1440)
                    await on_pcm(b'\x30\x00'*1440);first.set()
                self.last_metrics={'input_characters':len(''.join(sent)),'billed_text_words':10}
                out=io.BytesIO()
                with wave.open(out,'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(pcm)
                return out.getvalue()
        class IncrementalLLM:
            supports_text_streaming=True
            def teaching_response(self,*args,on_text=None,**kwargs):
                on_text('I want water. ')
                if not first.wait(3):raise RuntimeError('audio did not overlap generation')
                on_text('Please.');final.set()
                return 'express','I want water. Please.',{'input_tokens':12,'output_tokens':9}
        conn.tts=IncrementalTTS();conn.llm=IncrementalLLM()
        await englishMvp.process(conn,[cloud.PCM])
        self.assertTrue(first.is_set() and final.is_set())
        self.assertEqual(sent,['I want water. ','Please.'])
        events=[json.loads(row['data']) for row in self.rows()]
        names=[row['event'] for row in events]
        self.assertLess(names.index('audio_first_packet'),names.index('llm_usage'))
        self.assertEqual(names.count('tts_started'),1)
        self.assertTrue(events[-1]['success'])
        self.assertFalse(any(row['event']=='playback_confirmed' for row in events))

    async def test_stream_failure_does_not_confirm_partial_playback(self):
        conn=self.connection();cancelled=asyncio.Event()
        class IncrementalTTS:
            supports_incremental_input=True;supports_pcm_streaming=True
            async def speak_incremental(self,source,on_pcm):
                try:
                    async for _ in source:await on_pcm(b'\x30\x00'*1440)
                    await asyncio.Event().wait()
                finally:cancelled.set()
        class BrokenLLM:
            supports_text_streaming=True
            def teaching_response(self,*args,on_text=None,**kwargs):
                on_text('I want ');raise RuntimeError('fixture failure')
        conn.tts=IncrementalTTS();conn.llm=BrokenLLM()
        await asyncio.wait_for(englishMvp.process(conn,[cloud.PCM]),3)
        self.assertTrue(cancelled.is_set())
        events=[json.loads(row['data']) for row in self.rows()]
        self.assertFalse(events[-1]['success'])
        self.assertNotIn('playback_confirmed',[row['event'] for row in events])
