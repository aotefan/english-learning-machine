"""Optional duplex turn ownership on the existing authenticated v1 socket.

AEC capability must be explicitly negotiated. Capture/ASR overlap output;
generation/playback remain ordered, bounded, and independently cancellable.
"""
import asyncio
import json
import time
from collections import deque

CONTEXT_FIELDS = ('mvp_teaching_mode', 'mvp_dialogue_context', 'mvp_last_english',
    'mvp_last_wav', 'mvp_last_transcript', 'mvp_help_context', 'mvp_help_at',
    'mvp_clarification_context', 'mvp_clarification_at', 'mvp_correction_context',
    'mvp_repeat_streak')
STOP_REQUESTS = {'停止播放', '暂停播报', '先别说', '别说了',
                 'stop', 'stop speaking', 'stop playback'}


class TurnConnection:
    def __init__(self, parent):
        self.parent = parent
        self.asr_audio = []
        self.client_listen_mode = 'manual'  # VAD remains solely on the device.
        self.mvp_auto_listening = False
        self.client_is_speaking = False
        self.mvp_task = None
        self.mvp_duplex_session = None
        self.playback_done = asyncio.Event()
        self.cloud_usage = {}
        self.mvp_duplex_turn = True
        self.recognition_ready = asyncio.Event()

    def __getattr__(self, key):
        return getattr(self.parent, key)

    def clearSpeakStatus(self):
        self.client_is_speaking = False


class DuplexSession:
    def __init__(self, conn, *, queue_limit=3, max_queue_age=45):
        self.conn = conn
        self.capture = None
        self.active = None
        self.pending = deque()
        self.submitting = set()
        self.queue_limit = queue_limit
        self.max_queue_age = max_queue_age
        self.ready = asyncio.Event()
        self.closing = False
        self.worker = asyncio.create_task(self.run())

    async def notice(self, code, rid=None):
        await self.conn.websocket.send(json.dumps({'type':'error', 'error':code,
            'request_id':rid, 'terminal':False}))

    async def finish_silent(self, turn, reason):
        from core.handle import englishMvp as mvp
        if not turn.mvp_request_finished:
            mvp.event(turn, 'queue_turn_ended', reason=reason)
            mvp.event(turn, 'request_complete', success=False, reason=reason)
        stream = getattr(turn, 'mvp_asr_stream', None)
        if stream:await stream.close()
        await self.conn.websocket.send(json.dumps({'type':'tts','state':'stop',
            'request_id':turn.mvp_request_id,'no_audio':True,'reason':reason}))

    async def handle(self, message):
        from core.handle import englishMvp as mvp
        if self.closing:return
        self.conn.mvp_last_activity = time.perf_counter()
        if isinstance(message, bytes):
            if self.capture is None:
                return  # No accepted input turn owns these bytes.
            turn = self.capture
            if len(message)>1275 or len(turn.asr_audio)>=250:
                self.capture = None
                await self.finish_silent(turn, 'audio_too_long_or_packet_too_large')
                await self.notice('audio_too_long_or_packet_too_large', turn.mvp_request_id)
                return
            pcm = self.conn._decode_opus_packet(message)
            if pcm is None or len(pcm)!=1920:
                self.capture = None
                await self.finish_silent(turn, 'audio_decode_failed')
                return
            turn.asr_audio.append(pcm)
            mvp.feed_asr_stream(turn)
            return
        data = json.loads(message)
        if not isinstance(data, dict):raise ValueError('expected_object')
        kind = data.get('type')
        if kind == 'listen' and data.get('state') == 'start':
            if self.capture is not None:return
            if data.get('mode') != 'manual':
                await self.notice('duplex_requires_device_vad');return
            if len(self.pending)>=self.queue_limit:
                mvp.event(self.conn, 'queue_overloaded', request_id=None,
                    pending=len(self.pending), limit=self.queue_limit)
                await self.notice('voice_queue_full');return
            turn = TurnConnection(self.conn)
            mvp.begin_request(turn)
            turn.device_turn_id = data.get('turn_id')
            self.capture = turn
            await self.conn.websocket.send(json.dumps({'type':'input_ack',
                'request_id':turn.mvp_request_id,'turn_id':turn.device_turn_id}))
        elif kind == 'listen' and data.get('state') == 'stop':
            turn, self.capture = self.capture, None
            if turn is None:return
            turn.mvp_recording = False
            turn.mvp_submitted_at = time.perf_counter()
            turn.queued_at = turn.mvp_submitted_at
            ended, submitted = data.get('speech_end_us'), data.get('submitted_us')
            if type(ended) is int and type(submitted) is int and 0<=ended<=submitted:
                turn.speech_end_us = ended
                mvp.event(turn,'device_turn_timing',vad_stop_ms=(submitted-ended)/1000)
            if turn.mvp_asr_stream is not None:turn.mvp_asr_stream.finish()
            # Enqueue by capture order, never by which ASR request finishes first.
            self.pending.append(turn)
            mvp.event(turn,'playback_queued',depth=len(self.pending))
            self.ready.set()
            task = asyncio.create_task(self.submit(turn))
            self.submitting.add(task)
            task.add_done_callback(self.submitting.discard)
        elif kind == 'mvp_playback':
            turn = self.active
            if turn is not None and data.get('request_id') == turn.mvp_request_id:
                await mvp.handle_message(turn, message)
                stamp = data.get('device_us')
                ended = getattr(turn,'speech_end_us',None)
                if (data.get('event')=='playback_started' and turn.mvp_playback_reported
                        and type(stamp) is int and type(ended) is int and stamp>=ended):
                    mvp.event(turn,'device_latency',speech_end_to_playback_ms=(stamp-ended)/1000,
                        queue_wait_ms=turn.queue_wait_ms,clock='device_monotonic',
                        timing_basis='device_vad_edge_to_codec_write_return',actual_sound_measured=False)
                if turn.mvp_playback_completed:turn.playback_done.set()
        elif kind == 'abort':
            await self.cancel_playback('explicit_stop')
        elif kind == 'ping':
            await self.conn.websocket.send(json.dumps({'type':'pong'}))
        elif kind == 'hello':
            await self.notice('duplex_session_already_started')

    async def submit(self, turn):
        from core.handle import englishMvp as mvp
        try:
            # Recognition of this new turn continues while older audio plays.
            if turn.mvp_asr_stream is not None:
                text, usage = await turn.mvp_asr_stream.task
                if mvp.is_exact_request(text, STOP_REQUESTS):
                    self.pending = deque(item for item in self.pending if item is not turn)
                    mvp.event(turn,'asr_result',text=text)
                    mvp.event(turn,'asr_usage',**usage)
                    mvp.event(turn,'intent_classified',intent='playback_stop')
                    mvp.event(turn,'llm_usage',provider='local-stop',input_tokens=0,output_tokens=0)
                    mvp.event(turn,'tts_usage',provider='not-needed',input_characters=0,billed_text_words=0)
                    await self.cancel_playback('voice_stop')
                    mvp.event(turn,'request_complete',success=True)
                    await self.conn.websocket.send(json.dumps({'type':'tts','state':'stop',
                        'request_id':turn.mvp_request_id,'no_audio':True,'reason':'playback_stop'}))
                    await turn.mvp_asr_stream.close()
                    return
            if self.closing:
                await self.finish_silent(turn,'connection_closed');return
        except asyncio.CancelledError:
            await self.finish_silent(turn,'input_cancelled')
            raise
        except Exception:
            await self.finish_silent(turn,'asr_failed')
            await self.notice('seed_asr_failed',turn.mvp_request_id)
        finally:
            turn.recognition_ready.set()

    async def cancel_playback(self, reason):
        from core.handle import englishMvp as mvp
        while self.pending:
            await self.finish_silent(self.pending.popleft(),reason)
        active = self.active
        if active is not None:
            active.client_abort = True
            active.mvp_cancel_output = True
            if active.mvp_task is not None and not active.mvp_task.done():
                active.mvp_task.cancel()
            active.playback_done.set()
            mvp.event(active,'playback_cancelled',reason=reason)
        await self.conn.websocket.send(json.dumps({'type':'mvp_control',
            'action':'cancel_playback','reason':reason}))
        # Capture/ASR for the next input stays independent and enabled.

    async def run(self):
        from core.handle import englishMvp as mvp
        try:
            while not self.closing:
                await self.ready.wait()
                while self.pending:
                    turn = self.pending.popleft()
                    turn.queue_wait_ms = (time.perf_counter()-turn.queued_at)*1000
                    if turn.queue_wait_ms>self.max_queue_age*1000:
                        await self.finish_silent(turn,'voice_queue_expired')
                        await self.notice('voice_queue_expired',turn.mvp_request_id)
                        continue
                    self.active = turn
                    await turn.recognition_ready.wait()
                    if turn.mvp_request_finished or turn.client_abort:
                        self.active = None
                        continue
                    for key in CONTEXT_FIELDS:
                        if hasattr(self.conn,key):setattr(turn,key,getattr(self.conn,key))
                    mvp.event(turn,'queue_released',queue_wait_ms=turn.queue_wait_ms)
                    turn.mvp_task = asyncio.create_task(mvp.process(turn,turn.asr_audio))
                    try:
                        await turn.mvp_task
                        if turn.mvp_audio_returning and not turn.client_abort:
                            await asyncio.wait_for(turn.playback_done.wait(),45)
                        if not turn.client_abort:
                            for key in CONTEXT_FIELDS:
                                if key in turn.__dict__:setattr(self.conn,key,getattr(turn,key))
                    except asyncio.TimeoutError:
                        await self.cancel_playback('playback_ack_timeout')
                    except asyncio.CancelledError:
                        if self.closing:raise
                    finally:
                        self.active = None
                self.ready.clear()
        finally:
            if self.active is not None and self.active.mvp_task is not None:
                self.active.mvp_task.cancel()
                await asyncio.gather(self.active.mvp_task,return_exceptions=True)

    async def close(self):
        if self.closing:return
        self.closing = True
        if self.capture is not None:
            stream = getattr(self.capture,'mvp_asr_stream',None)
            if stream:await stream.close()
        for task in self.submitting:task.cancel()
        await asyncio.gather(*self.submitting,return_exceptions=True)
        for turn in self.pending:
            stream = getattr(turn,'mvp_asr_stream',None)
            if stream:await stream.close()
        self.worker.cancel()
        await asyncio.gather(self.worker,return_exceptions=True)
