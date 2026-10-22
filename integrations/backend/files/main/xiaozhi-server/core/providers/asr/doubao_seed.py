"""Seed-ASR 2.0 bidirectional input; final meaning remains turn-owned."""
import asyncio
import gzip
import json
import uuid
import time
import numpy as np
import websockets
from core.providers.asr.doubao_stream import ASRProvider as DoubaoStream
from core.providers.asr.dto.dto import InterfaceType
from core.utils.input_audio import rejection_reason


class ASRProvider(DoubaoStream):
    supports_input_streaming = True
    def __init__(self, config, delete_audio_file=True):
        super().__init__(config, delete_audio_file)
        self.interface_type = InterfaceType.LOCAL  # shared, stateless per turn
        self.ws_url = 'wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async'
        self.api_key = config['api_key']
        self.timeout_s = float(config.get('timeout_s', 25))
        self.resource_id = 'volc.seedasr.sauc.duration'
        hotwords = config.get('hotwords', [])
        if (not isinstance(hotwords, list) or len(hotwords) > 64 or
            any(not isinstance(word, str) or not word.strip() or len(word) > 32
                for word in hotwords)):
            raise ValueError('invalid_asr_hotwords')
        # Official corpus.context hints bias recognition; never rewrite its result.
        # No household dialogue is sent as recognition context.
        self.hotwords = tuple(dict.fromkeys(word.strip() for word in hotwords))

    def request_payload(self):
        context = {'context_type':'dialog_ctx','context_data':[{'text':
            '家庭英语交流，可能一句内中英混说。中文片段用汉字，英文保留英文，按原话转录，保留称呼和成语。'}]}
        if self.hotwords:
            context['hotwords'] = [{'word':word} for word in self.hotwords]
        return {'user':{'uid':'english-edge'},
                'audio':{'format':'pcm','codec':'raw','rate':16000,'bits':16,'channel':1},
                'request':{'model_name':'bigmodel','enable_itn':False,
                           'enable_nonstream':True, 'end_window_size':1200,
                           'enable_punc':True,'enable_ddc':False,'show_utterances':True,
                           'corpus':{'context':json.dumps(context, ensure_ascii=False)}}}

    async def speech_to_text(self, frames, session_id, audio_format='pcm'):
        if audio_format != 'pcm': raise ValueError('asr_requires_decoded_pcm16')
        pcm = b''.join(frames)
        if len(pcm)%2 or len(pcm)>480000 or rejection_reason(np.frombuffer(pcm,dtype='<i2')):
            raise ValueError('empty_or_invalid_audio')
        async def source():
            for start in range(0,len(pcm),6400):
                yield pcm[start:start+6400], start+6400>=len(pcm)
        return await self._recognize(source(), streamed=False)

    def start_stream(self, session_id):
        return SeedASRTurn(self)

    async def _recognize(self, source, *, streamed, on_update=None):
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        progress = {'updates': 0, 'revisions': 0, 'definite_segments': 0,
                    'first_result_ms': None, 'first_upload_ms': None}
        previous_text = ''
        segments = {}
        headers = {'X-Api-Key':self.api_key,'X-Api-Resource-Id':self.resource_id,
                   'X-Api-Request-Id':request_id, 'X-Api-Sequence':'-1'}
        async def execute():
            async with websockets.connect(self.ws_url, additional_headers=headers,
                                          max_size=1024*1024, open_timeout=5, close_timeout=2) as ws:
                body = gzip.compress(json.dumps(self.request_payload()).encode())
                await ws.send(self.generate_header()+len(body).to_bytes(4,'big')+body)
                sent_bytes = 0
                async def upload():
                    nonlocal sent_bytes
                    # Preserve PCM order, all samples and original 16kHz rate.
                    async for pcm, last in source:
                        if progress['first_upload_ms'] is None:
                            progress['first_upload_ms'] = (time.perf_counter()-started)*1000
                        sent_bytes += len(pcm)
                        chunk = gzip.compress(pcm)
                        header = self.generate_header(message_type=2,
                            message_type_specific_flags=2 if last else 0, serial_method=0)
                        await ws.send(header+len(chunk).to_bytes(4,'big')+chunk)
                async def receive():
                    nonlocal previous_text
                    async for packet in ws:
                        result = self.parse_response(packet)
                        body = result['payload_msg']
                        if result.get('code') or body.get('code', 0) not in (0,1000,20000000):
                            raise RuntimeError('seed_asr_provider_error')
                        snapshot = body.get('result', {})
                        text = snapshot.get('text', '')
                        if not isinstance(text, str) or len(text) > 4096:
                            raise ValueError('invalid_asr_snapshot')
                        if text and text != previous_text:
                            if progress['first_result_ms'] is None:
                                progress['first_result_ms'] = (time.perf_counter()-started)*1000
                            progress['updates'] += 1
                            if previous_text and not text.startswith(previous_text):
                                progress['revisions'] += 1
                            previous_text = text
                        for utterance in snapshot.get('utterances', []):
                            if not isinstance(utterance, dict):
                                raise ValueError('invalid_asr_utterance')
                            key = (utterance.get('start_time'), utterance.get('end_time'))
                            if all(type(stamp) is int and stamp >= 0 for stamp in key):
                                # Replace a revised interval instead of appending repeats.
                                segments[key] = utterance.get('definite') is True
                        if len(segments) > 256:
                            raise ValueError('too_many_asr_segments')
                        progress['definite_segments'] = sum(segments.values())
                        if on_update and text:
                            await on_update({'text': text, 'final': bool(result.get('last')),
                                             **progress})
                        if result.get('last'):
                            if not isinstance(text,str) or not text.strip():
                                raise ValueError('empty_asr')
                            return text
                    raise RuntimeError('seed_asr_closed_before_final')
                sender = asyncio.create_task(upload())
                receiver = asyncio.create_task(receive())
                try:
                    _, output = await asyncio.gather(sender,receiver)
                    return output, {'provider':'seed-asr-2.0', 'provider_request_id':request_id,
                                    'input_audio_seconds':sent_bytes/32000,
                                    'language_forced':False, 'ddc':False, 'itn':False,
                                    'input_streamed':streamed, **progress}
                finally:
                    for task in (sender,receiver):
                        if not task.done():task.cancel()
                    await asyncio.gather(sender,receiver,return_exceptions=True)
                    await source.aclose()
        try:
            async with asyncio.timeout(self.timeout_s):return await execute()
        except asyncio.CancelledError:raise
        except Exception as exc:
            # Do not leak headers, full URLs or provider response bodies to logs.
            raise RuntimeError('seed_asr_failed:'+type(exc).__name__) from None

    async def close(self):
        pass  # Each request owns and closes its upstream WS, no global turn state.


class SeedASRTurn:
    """One bounded recording owns its queue, socket task and explicit final marker."""
    def __init__(self, provider):
        self.queue = asyncio.Queue(maxsize=251)  # 250 maximum 60ms input frames + EOF.
        self.bytes = 0
        self.finished = False
        self.on_update = None
        self.latest = None
        async def update(snapshot):
            self.latest = snapshot
            if self.on_update:
                await self.on_update(snapshot)
        self.task = asyncio.create_task(provider._recognize(
            self.source(), streamed=True, on_update=update))
        self.task.add_done_callback(lambda task: None if task.cancelled() else task.exception())

    def feed(self, pcm):
        if self.finished or not isinstance(pcm, bytes) or not pcm or len(pcm)%2 or self.bytes+len(pcm)>480000:
            raise ValueError('invalid_stream_audio')
        if self.task.done():return
        self.bytes += len(pcm)
        self.queue.put_nowait(pcm)

    def finish(self):
        if not self.finished:
            self.finished = True
            if not self.task.done():self.queue.put_nowait(None)

    async def source(self):
        buffer = bytearray()
        while True:
            pcm = await self.queue.get()
            if pcm is None:break
            buffer.extend(pcm)
            # Keep a final nonempty block until the device explicitly submits.
            while len(buffer)>6400:
                yield bytes(buffer[:6400]),False
                del buffer[:6400]
        if not buffer:raise ValueError('empty_stream_audio')
        yield bytes(buffer),True

    def cancel(self):
        self.finished = True
        if not self.task.done():self.task.cancel()
        while not self.queue.empty():self.queue.get_nowait()

    async def close(self):
        self.cancel()
        await asyncio.gather(self.task,return_exceptions=True)
