"""No-network turn ownership tests; do not establish acoustic duplex."""
import asyncio,json,tempfile,time,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tests.test_doubao_cloud as cloud
from core.handle import englishMvp as mvp
from core.handle.englishDuplex import DuplexSession
from tests.test_cloud_tts_streaming import FakeSocket,SilentLogger

class FakeStream:
    def __init__(self,text,gate=None):
        self.frames=[];self.ended=asyncio.Event();self.gate=gate;self.text=text;self.on_update=None
        async def recognize():
            await self.ended.wait()
            if gate:await gate.wait()
            return text,{'input_audio_seconds':.3}
        self.task=asyncio.create_task(recognize())
    def feed(self,pcm):self.frames.append(pcm)
    def finish(self):self.ended.set()
    async def close(self):
        if not self.task.done():self.task.cancel()
        await asyncio.gather(self.task,return_exceptions=True)

class DuplexTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory());self.streams=[];self.texts=[];self.gates=[]
        def start(_):
            stream=FakeStream(self.texts.pop(0),self.gates.pop(0) if self.gates else None)
            self.streams.append(stream);return stream
        self.root=SimpleNamespace(config={'english_mvp':{'enabled':True,'cloud':True,'log_path':str(Path(self.temp)/'events.jsonl')}},
            websocket=FakeSocket(),logger=SilentLogger(),mvp_last_activity=time.perf_counter(),mvp_hello=True,
            mvp_requires_completion=True,mvp_teaching_mode='express',stop_event=threading.Event(),
            session_id='fixture',server=SimpleNamespace(cloud_policy=None),sample_rate=24000,device_id='fixture',
            asr=SimpleNamespace(supports_input_streaming=True,start_stream=start),
            _decode_opus_packet=lambda _:cloud.PCM[:1920],mvp_closing=False,mvp_duplex_session=None)
        self.session=DuplexSession(self.root);self.started=[];self.release=asyncio.Event()
        async def process(turn,frames):
            self.started.append(turn)
            turn.mvp_audio_returning=True
            turn.mvp_playback_reported=True
            await self.release.wait()
            mvp.event(turn,'request_complete',success=True)
        self.enterContext(patch.object(mvp,'process',side_effect=process))
    async def asyncTearDown(self):await self.session.close()
    async def capture(self,text):
        self.texts.append(text)
        await self.session.handle(json.dumps({'type':'listen','state':'start','mode':'manual'}))
        turn=self.session.capture;self.assertIsNotNone(turn)
        for _ in range(3):await self.session.handle(b'opus-fixture')
        await self.session.handle(json.dumps({'type':'listen','state':'stop'}))
        return turn
    async def settle(self):
        for _ in range(8):await asyncio.sleep(.001)

    async def test_new_input_reaches_asr_while_previous_output_runs_without_auto_abort(self):
        first=await self.capture('我想画画。');await self.settle()
        second=await self.capture('再拿一张纸。');await self.settle()
        self.assertIs(self.session.active,first)
        self.assertEqual(len(self.streams[1].frames),3)
        self.assertTrue(self.streams[1].task.done())
        self.assertFalse(first.client_abort)
        self.release.set();await self.settle()
        self.assertEqual(self.started,[first]) # still waiting actual playback completion
        await self.session.handle(json.dumps({'type':'mvp_playback','request_id':first.mvp_request_id,
            'event':'playback_completed','source':'codec_queue_drained'}))
        await self.settle();self.assertEqual(self.started,[first,second])

    async def test_asr_completion_reordering_cannot_reorder_playback(self):
        slow=asyncio.Event();self.gates=[slow,None]
        first=await self.capture('第一句。');second=await self.capture('第二句。')
        await self.settle();self.assertEqual(self.started,[])
        self.assertTrue(self.streams[1].task.done())
        slow.set();await self.settle();self.assertEqual(self.started,[first])
        self.assertEqual(list(self.session.pending),[second])

    async def test_explicit_stop_cancels_audio_and_pending_but_accepts_next_input(self):
        first=await self.capture('我想画画。');await self.settle()
        await self.capture('再拿一张纸。');await self.settle()
        stop=await self.capture('停止播放。');await self.settle()
        self.assertTrue(first.client_abort)
        self.assertFalse(self.session.pending)
        self.assertTrue(stop.mvp_request_finished)
        third=await self.capture('现在拿一支笔。');await self.settle()
        self.assertFalse(third.client_abort)
        controls=[json.loads(m) for m in self.root.websocket.sent if isinstance(m,str)]
        self.assertTrue(any(m.get('action')=='cancel_playback' for m in controls))

    async def test_overload_is_explicit_and_does_not_interrupt_current_audio(self):
        self.session.queue_limit=1
        first=await self.capture('第一句。');await self.settle()
        await self.capture('第二句。');await self.settle()
        await self.session.handle(json.dumps({'type':'listen','state':'start','mode':'manual'}))
        self.assertIsNone(self.session.capture);self.assertFalse(first.client_abort)
        notice=json.loads(self.root.websocket.sent[-1])
        self.assertEqual(notice['error'],'voice_queue_full');self.assertFalse(notice['terminal'])

    async def test_close_cancels_capture_asr_and_output_tasks(self):
        await self.capture('第一句。');await self.settle()
        self.texts.append('第二句。')
        await self.session.handle(json.dumps({'type':'listen','state':'start','mode':'manual'}))
        for _ in range(3):await self.session.handle(b'opus-fixture')
        await self.session.close()
        self.assertTrue(self.session.worker.done())
        self.assertTrue(all(stream.task.done() for stream in self.streams))
