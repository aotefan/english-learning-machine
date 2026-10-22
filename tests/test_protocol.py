"""Fault injection against upstream WebSocketServer and genuine Opus transport."""
import asyncio
import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]; SERVER=ROOT/'upstream/backend/main/xiaozhi-server'
os.chdir(SERVER);sys.path.insert(0,str(SERVER));sys.path.insert(0,str(ROOT/'scripts'))
import websockets
from core.websocket_server import WebSocketServer
from config.config_loader import load_config
from core.providers.tts.base import TTSProviderBase
from protocol_benchmark import connect, one

class ASR:
    async def speech_to_text(self,*args):return '我要喝水',None
    async def close(self):pass
class English:
    def response(self,*args):yield 'I want some water.'
class TTS(TTSProviderBase):
    async def text_to_speak(self,*args):return (ROOT/'artifacts/tts/01.wav').read_bytes()
class BrokenASR(ASR):
    async def speech_to_text(self,*args):
        if getattr(self,'failed',False):return await super().speech_to_text(*args)
        self.failed=True
        raise TimeoutError('injected_asr_timeout')
class BrokenEnglish:
    def response(self,*args):
        if getattr(self,'failed',False):return iter(['I want some water.'])
        self.failed=True
        raise RuntimeError('injected_english_failure')
class BrokenTTS(TTS):
    async def text_to_speak(self,*args):
        if getattr(self,'failed',False):return await super().text_to_speak(*args)
        self.failed=True
        raise RuntimeError('injected_tts_failure')

class SlowASR(ASR):
    def __init__(self):
        self.started = asyncio.Event()
        self.resume = asyncio.Event()
    async def speech_to_text(self, *args):
        self.started.set()
        await self.resume.wait()
        return await super().speech_to_text(*args)

class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        config=copy.deepcopy(await load_config());config['selected_module']={};config['english_mvp']['log_path']=str(ROOT/'artifacts/fault-tests.jsonl')
        self.server=WebSocketServer(config);self.server._asr=ASR();self.server._llm=English()
        self.patch=patch('core.connection.initialize_tts',lambda config:TTS({},True));self.patch.start()
        self.listener=await websockets.serve(self.server._handle_connection,'127.0.0.1',0)
        self.url=f"ws://127.0.0.1:{self.listener.sockets[0].getsockname()[1]}/xiaozhi/v1/"
        self.ws=await connect(self.url)
    async def asyncTearDown(self):
        await self.ws.close();self.listener.close();await self.listener.wait_closed();self.patch.stop()
    async def good(self):
        result=await one(self.ws,{'id':'01'},ROOT/'artifacts/synthetic',False)
        self.assertTrue(result['success'],result);self.assertIsNotNone(result['request_id'])
        self.assertEqual(result['english'],'I want some water.')

    async def test_diagnostic_capture_failure_does_not_break_voice(self):
        from unittest.mock import Mock
        capture = Mock()
        capture.save.side_effect = OSError('injected_disk_failure')
        offset = len(self.events())
        with patch('core.handle.englishMvp.diagnostic_capture', capture):
            await self.good()
        self.assertTrue(any(e['event'] == 'diagnostic_audio_error' for e in self.events()[offset:]))
        await self.good()

    async def test_diagnostic_capture_writes_correlated_wav(self):
        import tempfile
        from core.utils.diagnostic_audio import DiagnosticAudioCapture
        with tempfile.TemporaryDirectory() as directory:
            capture = DiagnosticAudioCapture(directory, limit=1)
            offset = len(self.events())
            with patch('core.handle.englishMvp.diagnostic_capture', capture):
                await self.good()
            saved = [e for e in self.events()[offset:] if e['event'] == 'diagnostic_audio_saved']
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]['filename'], saved[0]['request_id'] + '.wav')
            self.assertTrue((Path(directory) / saved[0]['filename']).is_file())

    async def test_custom_wav_failure_continues_and_preserves_evidence(self):
        import tempfile
        import shutil
        from types import SimpleNamespace
        from protocol_benchmark import main
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);inputs=folder/'inputs';inputs.mkdir()
            (inputs/'01.wav').write_bytes(b'bad wav')
            shutil.copyfile(ROOT/'artifacts/synthetic/02.wav',inputs/'02.wav')
            output=folder/'results.jsonl'
            args=SimpleNamespace(count=2,url=self.url,input_dir=str(inputs),source=None,
                                 output=str(output),returned_dir=None,burst=True)
            self.assertEqual(await main(args),1)
            rows=[json.loads(line) for line in output.read_text().splitlines()]
            self.assertFalse(rows[0]['success']);self.assertTrue(rows[1]['success'])
            self.assertTrue(all(row['source']=='file_wav' and row['hardware'] is False for row in rows))
            self.assertEqual(rows[1]['reference_zh'],'我的书在哪里？')
            self.assertEqual(rows[1]['reference_english'],'Where is my book?')
            self.assertTrue(Path(rows[1]['returned_audio']).is_file())
            evidence=output.read_bytes()
            with self.assertRaises(FileExistsError):await main(args)
            self.assertEqual(output.read_bytes(),evidence)

    def events(self):
        path=ROOT/'artifacts/fault-tests.jsonl'
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    async def terminal_since(self, offset):
        async def wait():
            while True:
                rows=self.events()[offset:]
                if any(r['event']=='request_complete' for r in rows):return rows
                await asyncio.sleep(.01)
        return await asyncio.wait_for(wait(),3)

    async def test_watchdog_missing_hello_closes_then_new_connection_works(self):
        await self.ws.close()
        with patch('core.handle.englishMvp.HELLO_TIMEOUT_S', .2), patch('core.handle.englishMvp.WATCHDOG_INTERVAL_S', .03):
            offset = len(self.events())
            self.ws = await websockets.connect(self.url, additional_headers={'Device-Id':'watchdog-test'})
            await asyncio.wait_for(self.ws.wait_closed(), 2)
            self.assertEqual(self.ws.close_reason, 'hello_timeout')
            self.assertTrue(any(e.get('reason') == 'hello_timeout' for e in self.events()[offset:]))
        self.ws = await connect(self.url)
        await self.good()

    async def test_watchdog_abandoned_recording_ignores_repeated_start_and_ping(self):
        from protocol_benchmark import OpusEncoderUtils
        await self.ws.close()
        with patch('core.handle.englishMvp.RECORDING_TIMEOUT_S', .4), patch('core.handle.englishMvp.WATCHDOG_INTERVAL_S', .03):
            self.ws = await connect(self.url)
            offset = len(self.events())
            start = json.dumps({'type':'listen','state':'start','mode':'manual'})
            await self.ws.send(start)
            packets = []
            OpusEncoderUtils(16000, 1, 60).encode_pcm_to_opus_stream(bytes(1920), True, packets.append)
            await self.ws.send(packets[0])
            await asyncio.sleep(.2)
            await self.ws.send(start)
            await self.ws.send(json.dumps({'type':'ping'}))
            self.assertEqual(json.loads(await self.ws.recv()), {'type':'pong'})
            await asyncio.wait_for(self.ws.wait_closed(), 1)
            self.assertEqual(self.ws.close_reason, 'recording_submit_timeout')
            rows = self.events()[offset:]
            terminal = [e for e in rows if e['event']=='request_complete']
            self.assertEqual(len(terminal), 1)
            self.assertFalse(terminal[0]['success'])
            self.assertLess(terminal[0]['total_ms'], 800)
            self.assertFalse(any(e['event']=='asr_result' for e in rows))
            self.assertEqual([e['frames'] for e in rows if e['event']=='audio_received'], [1])
        self.ws = await connect(self.url)
        await self.good()

    async def test_watchdog_pings_keep_idle_connection_then_silence_closes(self):
        await self.ws.close()
        with patch('core.handle.englishMvp.HEARTBEAT_TIMEOUT_S', .25), patch('core.handle.englishMvp.WATCHDOG_INTERVAL_S', .03):
            self.ws = await connect(self.url)
            for _ in range(8):
                await self.ws.send(json.dumps({'type':'ping'}))
                self.assertEqual(json.loads(await self.ws.recv()), {'type':'pong'})
                await asyncio.sleep(.05)
            await asyncio.wait_for(self.ws.wait_closed(), 2)
            self.assertEqual(self.ws.close_reason, 'heartbeat_timeout')
        self.ws = await connect(self.url)
        await self.good()

    async def test_watchdog_does_not_interrupt_slow_provider_or_audio_return(self):
        await self.ws.close()
        slow = SlowASR()
        self.server._asr = slow
        with patch('core.handle.englishMvp.RECORDING_TIMEOUT_S', .2), patch('core.handle.englishMvp.HEARTBEAT_TIMEOUT_S', .2), patch('core.handle.englishMvp.WATCHDOG_INTERVAL_S', .03):
            self.ws = await connect(self.url)
            request = asyncio.create_task(one(self.ws, {'id':'01'}, ROOT/'artifacts/synthetic', False))
            try:
                await asyncio.wait_for(slow.started.wait(), 3)
                await asyncio.sleep(.5)
                slow.resume.set()
                result = await asyncio.wait_for(request, 10)
                self.assertTrue(result['success'], result)
                await self.ws.send(json.dumps({'type':'ping'}))
                self.assertEqual(json.loads(await self.ws.recv()), {'type':'pong'})
            finally:
                slow.resume.set()
                if not request.done():
                    request.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await request
        await self.good()

    async def test_disconnect_during_recording_logs_terminal_failure(self):
        offset=len(self.events())
        await self.ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}))
        await self.ws.close()
        rows=await self.terminal_since(offset)
        terminal=[r for r in rows if r['event']=='request_complete']
        self.assertEqual(len(terminal),1)
        self.assertFalse(terminal[0]['success'])
        self.assertIn('connection_closed_during_recording',[r.get('error') for r in rows])
        self.ws=await connect(self.url);await self.good()

    async def test_abort_recording_then_next_request_same_socket(self):
        offset=len(self.events())
        await self.ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}))
        await self.ws.send(json.dumps({'type':'abort'}))
        await self.error_until_stop()
        rows=await self.terminal_since(offset)
        self.assertEqual(len([r for r in rows if r['event']=='request_complete']),1)
        self.assertIn('request_aborted',[r.get('error') for r in rows])
        await self.good()

    async def test_abort_during_audio_return_then_next_request_same_socket(self):
        import wave
        from protocol_benchmark import OpusEncoderUtils
        offset=len(self.events())
        await self.ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}))
        with wave.open(str(ROOT/'artifacts/synthetic/01.wav')) as wav:
            pcm=wav.readframes(wav.getnframes())
        packets=[]
        OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(pcm,True,packets.append)
        for packet in packets:
            await self.ws.send(packet)
        await self.ws.send(json.dumps({'type':'listen','state':'stop'}))
        while not isinstance(await asyncio.wait_for(self.ws.recv(),5), bytes):
            pass
        await self.ws.send(json.dumps({'type':'abort'}))
        errors=[]
        async def wait_stop():
            while True:
                message=await self.ws.recv()
                if isinstance(message,bytes):continue  # Already in-flight frames.
                row=json.loads(message)
                if row['type']=='error':errors.append(row['error'])
                if row.get('type')=='tts' and row.get('state')=='stop':return
        await asyncio.wait_for(wait_stop(),3)
        self.assertTrue(any('request_aborted' in error for error in errors),errors)
        rows=await self.terminal_since(offset)
        terminal=[r for r in rows if r['event']=='request_complete']
        self.assertEqual(len(terminal),1)
        self.assertIs(terminal[0]['success'],False)
        self.assertNotIn('audio_sent',[r['event'] for r in rows])
        await self.good()

    async def test_close_before_processing_task_starts_logs_terminal_failure(self):
        from core.connection import ConnectionHandler
        original=ConnectionHandler._route_message
        async def close_immediately(conn,message):
            await original(conn,message)
            if isinstance(message,str) and json.loads(message).get('state')=='stop':
                # Deterministically close before the just-created task gets a turn.
                await conn.close()
        offset=len(self.events())
        with patch.object(ConnectionHandler,'_route_message',close_immediately):
            await self.ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}))
            await self.ws.send(json.dumps({'type':'listen','state':'stop'}))
            await asyncio.wait_for(self.ws.wait_closed(),5)
            rows=await self.terminal_since(offset)
        terminal=[r for r in rows if r['event']=='request_complete']
        self.assertEqual(len(terminal),1)
        self.assertIs(terminal[0]['success'],False)
        self.assertIn('connection_closed_before_processing',[r.get('error') for r in rows])
        self.ws=await connect(self.url)
        await self.good()

    async def test_background_audio_failure_then_next_request_same_socket(self):
        from core.handle import sendAudioHandle
        original=sendAudioHandle._do_send_audio
        async def break_background_send(conn, packet, flow):
            # The first five prebuffer frames use the foreground sender. Fail
            # the background sender with queued frames still awaiting drain.
            if flow['packet_count']>=5:
                raise OSError('injected_background_send_failure')
            await original(conn,packet,flow)
        offset=len(self.events())
        with patch.object(sendAudioHandle,'_do_send_audio',break_background_send):
            result=await asyncio.wait_for(
                one(self.ws,{'id':'01'},ROOT/'artifacts/synthetic',False),35)
        self.assertFalse(result['success'],result)
        self.assertIn('TimeoutError',result['error'])
        rows=await self.terminal_since(offset)
        terminal=[r for r in rows if r['event']=='request_complete']
        self.assertEqual(len(terminal),1)
        self.assertIs(terminal[0]['success'],False)
        await self.good()

    async def test_disconnect_during_asr_logs_terminal_failure(self):
        await self.ws.close()
        slow=SlowASR();self.server._asr=slow;self.ws=await connect(self.url)
        offset=len(self.events())
        request=asyncio.create_task(one(self.ws,{'id':'01'},ROOT/'artifacts/synthetic',False))
        await asyncio.wait_for(slow.started.wait(),5)
        await self.ws.close()
        with self.assertRaises(websockets.exceptions.ConnectionClosed):await request
        rows=await self.terminal_since(offset)
        terminal=[r for r in rows if r['event']=='request_complete']
        self.assertEqual(len(terminal),1)
        self.assertFalse(terminal[0]['success'])
        self.assertIn('connection_closed_during_request',[r.get('error') for r in rows])
        self.server._asr=ASR();self.ws=await connect(self.url);await self.good()
    async def error_until_stop(self):
        errors=[]
        while True:
            data=json.loads(await asyncio.wait_for(self.ws.recv(),10))
            if data['type']=='error':errors.append(data)
            if data['type']=='tts' and data['state']=='stop':break
        self.assertTrue(errors);self.assertTrue(errors[0]['request_id'])
        return errors[0]['request_id']
    async def test_burst_audio_is_not_lost_at_stop(self):
        await self.good()
        records=[json.loads(x) for x in (ROOT/'artifacts/fault-tests.jsonl').read_text().splitlines()]
        self.assertEqual([x for x in records if x['event']=='audio_received'][-1]['frames'],33)
    async def test_empty_then_valid_same_socket(self):
        await self.ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}));await self.ws.send(json.dumps({'type':'listen','state':'stop'}))
        await self.error_until_stop();await self.good()
    async def test_bad_json_then_valid_same_socket(self):
        await self.ws.send('{');await self.error_until_stop();await self.good()
    async def test_corrupt_opus_then_valid_same_socket(self):
        await self.ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}));await self.ws.send(b'')
        request_id=await self.error_until_stop()
        rows=[json.loads(x) for x in (ROOT/'artifacts/fault-tests.jsonl').read_text().splitlines()]
        terminal=[x for x in rows if x['event']=='request_complete' and x['request_id']==request_id]
        self.assertEqual(len(terminal),1)
        self.assertFalse(terminal[0]['success'])
        self.assertIsNone(terminal[0]['asr_ms'])
        self.assertGreaterEqual(terminal[0]['total_ms'],0)
        await self.good()

    async def test_rejected_audio_trailing_stop_does_not_poison_next_request(self):
        offset=len(self.events())
        await self.ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}))
        await self.ws.send(b'\x00'*1276)
        # The device may already have queued stop when it learns the audio failed.
        await self.ws.send(json.dumps({'type':'listen','state':'stop'}))
        await self.error_until_stop()
        await self.ws.send(json.dumps({'type':'ping'}))
        self.assertEqual(json.loads(await self.ws.recv()),{'type':'pong'})
        rows=await self.terminal_since(offset)
        self.assertEqual(sum(r['event']=='request_complete' for r in rows),1)
        self.assertEqual(sum(r['event']=='error' for r in rows),1)
        await self.good()

    async def test_duplicate_stop_during_asr_does_not_add_error(self):
        import wave
        from protocol_benchmark import OpusEncoderUtils
        await self.ws.close()
        slow=SlowASR();self.server._asr=slow;self.ws=await connect(self.url)
        await self.ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}))
        with wave.open(str(ROOT/'artifacts/synthetic/01.wav')) as wav:
            pcm=wav.readframes(wav.getnframes())
        packets=[]
        OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(pcm,True,packets.append)
        for packet in packets:await self.ws.send(packet)
        await self.ws.send(json.dumps({'type':'listen','state':'stop'}))
        await asyncio.wait_for(slow.started.wait(),5)
        await self.ws.send(json.dumps({'type':'listen','state':'stop'}))
        await self.ws.send(json.dumps({'type':'ping'}))
        try:
            self.assertEqual(json.loads(await self.ws.recv()),{'type':'pong'})
        finally:
            slow.resume.set()
        audio=0
        while True:
            message=await asyncio.wait_for(self.ws.recv(),5)
            if isinstance(message,bytes):audio+=1;continue
            row=json.loads(message)
            self.assertNotEqual(row['type'],'error',row)
            if row.get('type')=='tts' and row.get('state')=='stop':break
        self.assertGreater(audio,0)
        await self.good()

    async def test_duplicate_start_preserves_captured_audio_and_request(self):
        from protocol_benchmark import read_input, OpusEncoderUtils
        offset=len(self.events())
        pcm,_=read_input(ROOT/'artifacts/synthetic','01')
        packets=[];OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(pcm,True,packets.append)
        start=json.dumps({'type':'listen','state':'start','mode':'manual'})
        await self.ws.send(start)
        for index,packet in enumerate(packets):
            await self.ws.send(packet)
            if index in (0,10,len(packets)-1):await self.ws.send(start)
        await self.ws.send(json.dumps({'type':'listen','state':'stop'}))
        audio=0
        while True:
            message=await asyncio.wait_for(self.ws.recv(),5)
            if isinstance(message,bytes):audio+=1;continue
            row=json.loads(message)
            self.assertNotEqual(row['type'],'error',row)
            if row.get('type')=='tts' and row.get('state')=='stop':break
        self.assertGreater(audio,0)
        events=await self.terminal_since(offset)
        self.assertEqual(sum(row['event']=='recording_started' for row in events),1)
        terminals=[row for row in events if row['event']=='request_complete']
        self.assertEqual(len(terminals),1);self.assertTrue(terminals[0]['success'])
        received=[row for row in events if row['event']=='audio_received']
        self.assertEqual(received[0]['frames'],len(packets))
        self.assertEqual(len({row['request_id'] for row in events}),1)
        await self.good()

    async def test_new_start_during_asr_is_busy_without_replacing_request(self):
        from protocol_benchmark import read_input, OpusEncoderUtils
        await self.ws.close()
        slow=SlowASR();self.server._asr=slow;self.ws=await connect(self.url)
        offset=len(self.events())
        pcm,_=read_input(ROOT/'artifacts/synthetic','01')
        packets=[];OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(pcm,True,packets.append)
        start=json.dumps({'type':'listen','state':'start','mode':'manual'})
        await self.ws.send(start)
        for packet in packets:await self.ws.send(packet)
        await self.ws.send(json.dumps({'type':'listen','state':'stop'}))
        await asyncio.wait_for(slow.started.wait(),5)
        try:
            await self.ws.send(start)
            error=json.loads(await asyncio.wait_for(self.ws.recv(),5))
            self.assertEqual(error['error'],'busy')
            await self.ws.send(json.dumps({'type':'listen','state':'stop'}))
            await self.ws.send(json.dumps({'type':'ping'}))
            self.assertEqual(json.loads(await asyncio.wait_for(self.ws.recv(),5)),{'type':'pong'})
        finally:slow.resume.set()
        audio=0
        while True:
            message=await asyncio.wait_for(self.ws.recv(),5)
            if isinstance(message,bytes):audio+=1;continue
            row=json.loads(message)
            self.assertNotEqual(row['type'],'error',row)
            if row.get('type')=='tts' and row.get('state')=='stop':break
        self.assertGreater(audio,0)
        events=await self.terminal_since(offset)
        self.assertEqual(sum(row['event']=='recording_started' for row in events),1)
        terminals=[row for row in events if row['event']=='request_complete']
        self.assertEqual(len(terminals),1);self.assertTrue(terminals[0]['success'])
        self.assertEqual(terminals[0]['request_id'],error['request_id'])
        await self.good()
    async def test_asr_failure_then_valid_same_socket(self):
        await self.ws.close();self.server._asr=BrokenASR();self.ws=await connect(self.url)
        result=await one(self.ws,{'id':'01'},ROOT/'artifacts/synthetic',False);self.assertFalse(result['success']);self.assertIn('injected_asr_timeout',result['error'])
        await self.good()
    async def test_english_failure_then_valid_same_socket(self):
        await self.ws.close();self.server._llm=BrokenEnglish();self.ws=await connect(self.url)
        result=await one(self.ws,{'id':'01'},ROOT/'artifacts/synthetic',False);self.assertFalse(result['success']);self.assertIn('injected_english_failure',result['error'])
        await self.good()
    async def test_tts_failure_then_valid_same_socket(self):
        await self.ws.close();self.patch.stop();self.patch=patch('core.connection.initialize_tts',lambda config:BrokenTTS({},True));self.patch.start();self.ws=await connect(self.url)
        result=await one(self.ws,{'id':'01'},ROOT/'artifacts/synthetic',False);self.assertFalse(result['success']);self.assertIn('injected_tts_failure',result['error'])
        await self.good()
    async def test_disconnect_and_reconnect(self):
        await self.ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}));await self.ws.close();self.ws=await connect(self.url);await self.good()
    async def test_server_restart_client_can_reconnect(self):
        await self.ws.close();port=self.listener.sockets[0].getsockname()[1];self.listener.close();await self.listener.wait_closed()
        self.listener=await websockets.serve(self.server._handle_connection,'127.0.0.1',port)
        self.ws=await connect(self.url);await self.good()

    async def test_ping_and_playback_receipt_is_correlated_and_deduplicated(self):
        await self.ws.send(json.dumps({'type':'ping'}))
        self.assertEqual(json.loads(await self.ws.recv()), {'type':'pong'})
        result=await one(self.ws,{'id':'01'},ROOT/'artifacts/synthetic',False)
        self.assertTrue(result['success'])
        request_id=result['request_id']
        receipt={'type':'mvp_playback','event':'playback_started',
                 'source':'codec_output_returned','request_id':request_id}
        await self.ws.send(json.dumps({**receipt,'request_id':'f'*32}))
        await self.ws.send(json.dumps({**receipt,'event':'wrong'}))
        await self.ws.send(json.dumps(receipt))
        await self.ws.send(json.dumps(receipt))
        await self.ws.send(json.dumps({'type':'ping'}))
        self.assertEqual(json.loads(await self.ws.recv()), {'type':'pong'})
        rows=[json.loads(x) for x in (ROOT/'artifacts/fault-tests.jsonl').read_text().splitlines()]
        receipts=[x for x in rows if x['event']=='playback_started' and x['request_id']==request_id]
        self.assertEqual(len(receipts),1)
        self.assertEqual(receipts[0]['source'],'codec_output_returned')

    async def test_bad_control_during_asr_does_not_end_valid_response(self):
        import wave
        from protocol_benchmark import OpusEncoderUtils
        await self.ws.close()
        slow=SlowASR();self.server._asr=slow;self.ws=await connect(self.url)
        await self.ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}))
        with wave.open(str(ROOT/'artifacts/synthetic/01.wav')) as wav:
            pcm=wav.readframes(wav.getnframes())
        packets=[]
        OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(pcm,True,packets.append)
        for packet in packets:
            await self.ws.send(packet)
        await self.ws.send(json.dumps({'type':'listen','state':'stop'}))
        await asyncio.wait_for(slow.started.wait(),5)
        await self.ws.send('{')
        error=json.loads(await asyncio.wait_for(self.ws.recv(),5))
        self.assertEqual(error['type'],'error')
        await self.ws.send(json.dumps({'type':'ping'}))
        self.assertEqual(json.loads(await self.ws.recv()),{'type':'pong'})
        slow.resume.set()
        binary_frames=0
        while True:
            frame=await asyncio.wait_for(self.ws.recv(),10)
            if isinstance(frame,bytes):
                binary_frames+=1
            else:
                data=json.loads(frame)
                if data.get('type')=='tts' and data.get('state')=='stop':break
        self.assertGreater(binary_frames,0)
        await self.good()

    async def test_provider_init_failure_closes_only_that_connection(self):
        await self.ws.close();self.patch.stop()
        self.patch=patch('core.connection.initialize_tts',side_effect=RuntimeError('injected_init_failure'))
        self.patch.start()
        with self.assertRaises(websockets.exceptions.ConnectionClosedError):
            await connect(self.url)
        self.patch.stop()
        self.patch=patch('core.connection.initialize_tts',lambda config:TTS({},True))
        self.patch.start();self.ws=await connect(self.url)
        await self.good()

    async def test_keepalive_updates_upstream_idle_timer(self):
        await self.ws.close()
        # Upstream adds 60 seconds and checks every 10 seconds. Make the idle
        # budget 3 seconds, then keep the same socket alive across its check.
        self.server.config['close_connection_no_voice_time']=-57
        self.ws=await connect(self.url)
        for _ in range(6):
            await self.ws.send(json.dumps({'type':'ping'}))
            self.assertEqual(json.loads(await self.ws.recv()),{'type':'pong'})
            await asyncio.sleep(2)
        await self.good()

if __name__=='__main__':unittest.main()
