"""Local provider protocol tests; no recognition-quality or paid-call claims."""
import gzip
import asyncio
import json
import unittest
import hashlib
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import tests.test_doubao_cloud as cloud
from cloud_service import CloudPolicy, attach_policy
from cloud_report import billing_report
from core.utils.opus_encoder_utils import OpusEncoderUtils


class MixedAsrTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_uploads_before_end_and_sends_exactly_one_final_block(self):
        asr=cloud.ASRProvider({'api_key':'fixture'})
        seen=[];flags=[];first=asyncio.Event()
        async def vendor(ws):
            await ws.recv()
            async for packet in ws:
                seen.append(gzip.decompress(packet[8:]));flags.append(bool(packet[1]&2));first.set()
                if flags[-1]:
                    await ws.send(cloud.response_packet({'result':{'text':'妈妈，我想喝水。'}}));return
        async with cloud.websockets.serve(vendor,'127.0.0.1',0) as server:
            asr.ws_url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            turn=asr.start_stream('fixture')
            pcm=cloud.PCM*3
            try:
                for start in range(0,len(pcm),1920):turn.feed(pcm[start:start+1920])
                await asyncio.wait_for(first.wait(),2)
                self.assertFalse(turn.task.done());self.assertFalse(any(flags))
                turn.finish();text,usage=await asyncio.wait_for(turn.task,2)
                self.assertEqual(text,'妈妈，我想喝水。')
                self.assertEqual(b''.join(seen),pcm);self.assertEqual(sum(flags),1)
                self.assertTrue(flags[-1]);self.assertTrue(usage['input_streamed'])
                self.assertEqual(usage['input_audio_seconds'],len(pcm)/32000)
            finally:await turn.close()

    async def test_cancelled_stream_closes_socket_without_retry_or_final(self):
        asr=cloud.ASRProvider({'api_key':'fixture'});opened=0;closed=asyncio.Event();first=asyncio.Event();flags=[]
        async def vendor(ws):
            nonlocal opened
            opened+=1;await ws.recv()
            try:
                async for packet in ws:
                    flags.append(bool(packet[1]&2));first.set()
            finally:closed.set()
        async with cloud.websockets.serve(vendor,'127.0.0.1',0) as server:
            asr.ws_url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            turn=asr.start_stream('fixture')
            turn.feed(cloud.PCM)
            await asyncio.wait_for(first.wait(),2);await turn.close()
            await asyncio.wait_for(closed.wait(),2)
        self.assertEqual(opened,1);self.assertFalse(any(flags));self.assertTrue(turn.task.cancelled())
        self.assertTrue(turn.queue.empty())

    async def test_corpus_hints_on_wire_keep_pcm_and_returned_pinyin_unchanged(self):
        asr = cloud.ASRProvider({'api_key': 'fixture', 'hotwords': ['桂圆干', '龙眼干', '桂圆干']})
        raw = 'Do you want to eat long yan gan?'
        chunks = []
        async def vendor(ws):
            envelope = await ws.recv()
            payload = json.loads(gzip.decompress(envelope[8:]))
            self.assertNotIn('language', payload['audio'])
            context = json.loads(payload['request']['corpus']['context'])
            self.assertEqual(context['context_type'], 'dialog_ctx')
            self.assertEqual(context['hotwords'], [{'word': '桂圆干'}, {'word': '龙眼干'}])
            self.assertIn('中英混说', context['context_data'][0]['text'])
            self.assertFalse(payload['request']['enable_ddc'])
            self.assertFalse(payload['request']['enable_itn'])
            async for envelope in ws:
                chunks.append(gzip.decompress(envelope[8:]))
                if envelope[1] & 2:
                    await ws.send(cloud.response_packet({'result': {'text': raw}}))
                    return
        async with cloud.websockets.serve(vendor, '127.0.0.1', 0) as server:
            asr.ws_url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            text, usage = await asr.speech_to_text([cloud.PCM, cloud.PCM], 'fixture')
        self.assertEqual(b''.join(chunks), cloud.PCM * 2)
        self.assertEqual(text, raw)  # Hints must not disguise provider mistakes as recognition.
        self.assertFalse(usage['language_forced'])

    def test_hotwords_are_bounded_copied_and_never_enter_errors(self):
        for invalid in ('private-fixture', ['x'] * 65, ['x' * 33], [''], [1]):
            with self.subTest(kind=type(invalid).__name__):
                with self.assertRaisesRegex(ValueError, '^invalid_asr_hotwords$'):
                    cloud.ASRProvider({'api_key': 'fixture', 'hotwords': invalid})
        words = ['桂圆干']
        asr = cloud.ASRProvider({'api_key': 'fixture', 'hotwords': words})
        words.append('unexpected')
        first = asr.request_payload()
        first['request']['corpus']['context'] = 'changed'
        context = json.loads(asr.request_payload()['request']['corpus']['context'])
        self.assertEqual(context['hotwords'], [{'word': '桂圆干'}])
        self.assertEqual(cloud.load_cloud_config(require_credentials=False)['ASR']['SeedASR2']['hotwords'],
                         ['桂圆干', '龙眼干', '多多益善'])


class InputStreamBudgetTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=cloud.ContractTests.asyncSetUp
    asyncTearDown=cloud.ContractTests.asyncTearDown
    llm_response=cloud.ContractTests.llm_response
    tts_response=cloud.ContractTests.tts_response

    async def test_upload_overlaps_capture_with_one_admission_and_no_model_until_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);registry=folder/'devices.json'
            registry.write_text(json.dumps({'fixture':hashlib.sha256(b'test-token').hexdigest()}));registry.chmod(0o600)
            with patch.dict(os.environ,ASR_CNY_PER_HOUR='1',LLM_INPUT_CNY_PER_MILLION='.2',
                            LLM_OUTPUT_CNY_PER_MILLION='2',TTS_CNY_PER_10K_CHARS='3',
                            DAILY_CNY_LIMIT='1',DAILY_TURN_LIMIT='1'):
                policy=CloudPolicy(folder/'state',registry,live=True)
            first=asyncio.Event();vendor_calls=0;received=[]
            async def vendor(ws):
                nonlocal vendor_calls
                vendor_calls+=1;await ws.recv()
                async for packet in ws:
                    received.append(gzip.decompress(packet[8:]));first.set()
                    if packet[1]&2:
                        await ws.send(cloud.response_packet({'result':{'text':cloud.RAW}}));return
            config=cloud.load_cloud_config(require_credentials=False);config['selected_module']={}
            server=attach_policy(cloud.WebSocketServer(config),policy);server._asr=self.asr;server._llm=self.llm
            packets=[];OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(cloud.PCM*3,True,packets.append)
            quiet=[];OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(b'\0'*11520,True,quiet.append)
            async with cloud.websockets.serve(vendor,'127.0.0.1',0) as upstream:
                self.asr.ws_url=f'ws://127.0.0.1:{upstream.sockets[0].getsockname()[1]}'
                with patch('core.connection.initialize_tts',lambda _:self.tts):
                    async with cloud.websockets.serve(server._handle_connection,'127.0.0.1',0,process_request=server._http_response) as listener:
                        ws=await cloud.connect(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/',device_id='fixture',token='test-token')
                        try:
                            await ws.send('{"type":"listen","state":"start","mode":"manual"}')
                            for packet in quiet:await ws.send(packet)
                            await ws.send('{"type":"ping"}')
                            self.assertEqual(json.loads(await ws.recv()),{'type':'pong'})
                            self.assertEqual(policy.runtime_status()['budget']['turns'],0)
                            self.assertEqual(vendor_calls,0)
                            for packet in packets[:8]:await ws.send(packet)
                            await asyncio.wait_for(first.wait(),2)
                            self.assertEqual(policy.runtime_status()['budget']['turns'],1)
                            self.assertEqual(self.requests,[])  # No LLM/TTS while the child is still speaking.
                            for packet in packets[8:]:await ws.send(packet)
                            await ws.send('{"type":"listen","state":"stop"}')
                            frames=[];audio=0
                            while True:
                                msg=await asyncio.wait_for(ws.recv(),3)
                                if isinstance(msg,bytes):audio+=1;continue
                                item=json.loads(msg);frames.append(item)
                                if item.get('type')=='tts' and item.get('state')=='stop':break
                            self.assertGreater(audio,0);self.assertEqual(sum(item['type']=='stt' for item in frames),1)
                            self.assertEqual(policy.runtime_status()['budget']['turns'],1)
                            # A second valid recording is rejected before opening a vendor socket.
                            policy.last_turn.clear()  # Isolate quota admission from the separately tested cooldown.
                            await ws.send('{"type":"listen","state":"start","mode":"manual"}')
                            for packet in packets:await ws.send(packet)
                            await ws.send('{"type":"listen","state":"stop"}')
                            rejected=[]
                            while True:
                                item=json.loads(await asyncio.wait_for(ws.recv(),3));rejected.append(item)
                                if item.get('type')=='tts' and item.get('state')=='stop':break
                            self.assertTrue(any(item.get('error_code')=='budget_exhausted' for item in rejected))
                        finally:await ws.close()
            self.assertEqual(vendor_calls,1)
            self.assertEqual(len(b''.join(received)),len(cloud.PCM*3)+11520)
            bill=billing_report(policy.db_path)
            self.assertEqual([bill['stages'][stage]['calls'] for stage in ('asr','english','tts')],[1,1,1])
