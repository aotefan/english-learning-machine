"""Official TTS2 HTTP Chunked API -> in-memory WAV for the existing Opus path."""
import asyncio
import base64
import io
import json
import time
import uuid
import wave
import httpx
import websockets
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.huoshan_double_stream import (
    Header, Optional, TTSProvider as UpstreamProtocol,
    FULL_CLIENT_REQUEST, FULL_SERVER_RESPONSE, AUDIO_ONLY_RESPONSE,
    ERROR_INFORMATION, MsgTypeFlagWithEvent, JSON,
    EVENT_Start_Connection, EVENT_ConnectionStarted, EVENT_StartSession,
    EVENT_SessionStarted, EVENT_TaskRequest, EVENT_FinishSession,
    EVENT_SessionFinished, EVENT_SessionFailed, EVENT_TTSResponse,
)


class TTSProvider(TTSProviderBase):
    supports_pcm_streaming = True

    def __init__(self, config, delete_audio_file=True):
        super().__init__(config, delete_audio_file)
        self.api_key = config['api_key']
        self.speaker = config['speaker']
        self.help_speaker = config.get('help_speaker', 'zh_female_vv_uranus_bigtts')
        self.timeout_s = float(config.get('timeout_s',25))
        self.client = None
        self.last_metrics = {}
        self.supports_incremental_input = config.get('bidirectional', False) is True
        self._stream_ws = None
        self._stream_lock = asyncio.Lock()
        self._stream_used_at = 0
        # Reuse the checked-in MIT upstream binary protocol, without starting
        # its unrelated queue threads or requiring old AppID/token credentials.
        self._protocol = object.__new__(UpstreamProtocol)

    async def _send_stream_event(self, event, payload, session_id=None):
        header = Header(message_type=FULL_CLIENT_REQUEST,
                        message_type_specific_flags=MsgTypeFlagWithEvent,
                        serial_method=JSON).as_bytes()
        await self._protocol.send_event(self._stream_ws, header,
            Optional(event=event, sessionId=session_id).as_bytes(),
            json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode())

    async def _receive_stream_event(self, session_id=None):
        packet = await self._stream_ws.recv()
        if (not isinstance(packet, bytes) or not 8 <= len(packet) <= 2*1024*1024
                or packet[0] != 0x11 or packet[1] >> 4 not in
                    (FULL_SERVER_RESPONSE, AUDIO_ONLY_RESPONSE, ERROR_INFORMATION)):
            raise ValueError('invalid_tts_stream_packet')
        response = self._protocol.parser_response(packet)
        if response.header.message_type == ERROR_INFORMATION:
            raise RuntimeError('tts_stream_provider_error')
        if session_id and response.optional.sessionId != session_id:
            raise ValueError('tts_stream_session_mismatch')
        if response.optional.event == EVENT_SessionFailed:
            raise RuntimeError('tts_stream_session_failed')
        return response

    async def speak_incremental(self, text_source, on_pcm):
        """One ordered session; input and output overlap; no paid retries."""
        async with self._stream_lock:
            session_id = str(uuid.uuid4())
            start = time.perf_counter()
            pcm = bytearray()
            first_ms = usage = None
            text_length = 0
            sender = None
            try:
                async with asyncio.timeout(self.timeout_s):
                    if self._stream_ws is not None and (
                            time.perf_counter()-self._stream_used_at >= 45 or
                            self._stream_ws.state.name != 'OPEN'):
                        await self._stream_ws.close()
                        self._stream_ws = None
                    if self._stream_ws is None:
                        self._stream_ws = await websockets.connect(
                            'wss://openspeech.bytedance.com/api/v3/tts/bidirection',
                            additional_headers={'X-Api-Key': self.api_key,
                                'X-Api-Resource-Id': 'seed-tts-2.0',
                                'X-Api-Request-Id': session_id,
                                'X-Control-Require-Usage-Tokens-Return': '*'},
                            open_timeout=5, close_timeout=2, max_size=2*1024*1024)
                        await self._send_stream_event(EVENT_Start_Connection, {})
                        if (await self._receive_stream_event()).optional.event != EVENT_ConnectionStarted:
                            raise ValueError('tts_connection_not_started')
                    metadata = {'user': {'uid': 'english-edge'}, 'event': EVENT_StartSession,
                        'namespace': 'BidirectionalTTS', 'req_params': {'speaker': self.speaker,
                        'audio_params': {'format': 'pcm', 'sample_rate': 24000}}}
                    await self._send_stream_event(EVENT_StartSession, metadata, session_id)
                    if (await self._receive_stream_event(session_id)).optional.event != EVENT_SessionStarted:
                        raise ValueError('tts_session_not_started')

                    async def upload():
                        nonlocal text_length
                        async for text in text_source:
                            if not isinstance(text, str) or not text or text_length+len(text)>400:
                                raise ValueError('invalid_incremental_tts_text')
                            text_length += len(text)
                            await self._send_stream_event(EVENT_TaskRequest,
                                {'event': EVENT_TaskRequest, 'namespace': 'BidirectionalTTS',
                                 'req_params': {'text': text}}, session_id)
                        if not text_length:
                            raise ValueError('empty_incremental_tts_text')
                        # Finish immediately, including for an unpunctuated tail.
                        await self._send_stream_event(EVENT_FinishSession, {}, session_id)

                    sender = asyncio.create_task(upload())
                    async def receive():
                        nonlocal first_ms, usage
                        while True:
                            response = await self._receive_stream_event(session_id)
                            if response.optional.event == EVENT_TTSResponse:
                                chunk = response.payload
                                if (not isinstance(chunk, bytes) or not chunk or
                                        len(pcm)+len(chunk)>24000*2*30):
                                    raise ValueError('invalid_tts_stream_audio')
                                pcm.extend(chunk)
                                if first_ms is None:
                                    first_ms = (time.perf_counter()-start)*1000
                                await on_pcm(chunk)
                            elif response.optional.event == EVENT_SessionFinished:
                                info = json.loads(response.optional.response_meta_json or '{}')
                                usage = info.get('usage', {}).get('text_words')
                                return
                    receiver = asyncio.create_task(receive())
                    try:
                        await asyncio.gather(sender, receiver)
                    finally:
                        for task in (sender, receiver):
                            if not task.done():task.cancel()
                        await asyncio.gather(sender, receiver, return_exceptions=True)
                    if not pcm or len(pcm)%2:
                        raise ValueError('invalid_tts_stream_audio')
                    self._stream_used_at = time.perf_counter()
                    self.last_metrics = {'provider':'seed-tts-2.0-bidirectional',
                        'provider_request_id':session_id, 'speaker':self.speaker,
                        'first_chunk_ms':first_ms, 'input_characters':text_length,
                        'billed_text_words':usage, 'output_audio_seconds':len(pcm)/48000}
                    # Cache synthesized PCM in memory only after forwarding it.
                    out = io.BytesIO()
                    with wave.open(out, 'wb') as wav:
                        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
                        wav.writeframes(pcm)
                    return out.getvalue()
            except BaseException as exc:
                if self._stream_ws is not None:
                    await self._stream_ws.close()
                    self._stream_ws = None
                if isinstance(exc, asyncio.CancelledError):raise
                raise RuntimeError('seed_tts_failed:'+type(exc).__name__) from None

    async def text_to_speak(self, text, _, *, speech_rate=0, speaker=None, on_pcm=None):
        if not isinstance(text,str) or not text.strip() or len(text)>400:
            raise ValueError('invalid_tts_text')
        if not isinstance(speech_rate, int) or isinstance(speech_rate, bool) or not -50 <= speech_rate <= 100:
            raise ValueError('invalid_speech_rate')
        if speaker is not None and (not isinstance(speaker, str) or not speaker or len(speaker) > 128):
            raise ValueError('invalid_speaker')
        if on_pcm is not None and not callable(on_pcm):
            raise ValueError('invalid_pcm_callback')
        selected_speaker = speaker or self.speaker
        # Created on the serving event loop; reuse its keep-alive connection.
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=False,
                                           limits=httpx.Limits(max_connections=100,
                                               max_keepalive_connections=20, keepalive_expiry=60))
        rid = str(uuid.uuid4()); start = time.perf_counter(); first_ms = None
        pcm = bytearray(); usage = None
        self.last_metrics = {}
        headers = {'X-Api-Key':self.api_key,'X-Api-Resource-Id':'seed-tts-2.0',
                   'X-Api-Request-Id':rid,'X-Control-Require-Usage-Tokens-Return':'*'}
        payload = {'user':{'uid':'english-edge'},'req_params':{'text':text,'speaker':selected_speaker,
                   'audio_params':{'format':'pcm','sample_rate':24000,'speech_rate':speech_rate}}}
        try:
            async with asyncio.timeout(self.timeout_s):
                async with self.client.stream('POST','https://openspeech.bytedance.com/api/v3/tts/unidirectional',
                                              headers=headers,json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:continue
                        if len(line)>4*1024*1024:raise ValueError('tts_response_too_large')
                        item = json.loads(line)
                        if item.get('code') not in (0,20000000):raise RuntimeError('tts_provider_error')
                        if item.get('data'):
                            chunk = base64.b64decode(item['data'],validate=True)
                            if len(pcm) + len(chunk)>24000*2*30:
                                raise ValueError('tts_audio_too_long')
                            pcm.extend(chunk)
                            if first_ms is None:first_ms=(time.perf_counter()-start)*1000
                            if chunk and on_pcm is not None:
                                await on_pcm(chunk)
                        if item.get('usage') is not None:usage=item['usage'].get('text_words')
                        if item.get('code')==20000000:
                            break  # Legacy explicit terminal success marker.
                    # Current HTTP contract: code=0 records + complete HTTP body.
                    # HTTPX raises on a truncated chunked transfer; don't invent a
                    # mandatory marker that the current endpoint does not promise.
            if not pcm or len(pcm)%2:raise ValueError('empty_or_invalid_tts_pcm')
            out = io.BytesIO()
            with wave.open(out,'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(pcm)
            self.last_metrics={'provider':'seed-tts-2.0','provider_request_id':rid,'speaker':selected_speaker,
                               'first_chunk_ms':first_ms,'input_characters':len(text),
                               'billed_text_words':usage,'output_audio_seconds':len(pcm)/48000}
            return out.getvalue()
        except asyncio.CancelledError:raise
        except Exception as exc:
            raise RuntimeError('seed_tts_failed:'+type(exc).__name__) from None

    async def close(self):
        if self._stream_ws is not None:
            await self._stream_ws.close(); self._stream_ws=None
        if self.client is not None:
            await self.client.aclose(); self.client=None
