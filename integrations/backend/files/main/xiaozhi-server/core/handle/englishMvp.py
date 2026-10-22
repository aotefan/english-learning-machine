"""Manual and opt-in Silero turns over the upstream connection/audio protocol."""
import asyncio
import io
import json
import re
import time
import uuid
import wave
from collections import deque
from pathlib import Path
import numpy as np
from core.handle.sendAudioHandle import sendAudio, send_display_message, send_tts_message
from core.utils.util import audio_bytes_to_data_stream
from core.utils.input_audio import rejection_reason
from core.utils.teaching import direct_help_request, is_silent_expression_candidate, is_unchanged_english_expression
from core.utils.pcm_opus_stream import PCMOpusStream
from core.utils.incremental_speech import IncrementalSpeech


# Set only by the local runner when explicitly enabling bounded diagnostics.
diagnostic_capture = None
# Firmware stops capture at 13 s; server accepts up to 15 s of PCM.
# Allow transport/drain headroom while bounding abandoned captures.
RECORDING_TIMEOUT_S = 20
HELLO_TIMEOUT_S = 15
HEARTBEAT_TIMEOUT_S = 45  # Firmware sends application ping every 15 s.
WATCHDOG_INTERVAL_S = 1
REPEAT_WITHOUT_CONTEXT = "I don't have a sentence to repeat yet."
SLOW_WITHOUT_CONTEXT = "I don't have a sentence to slow down yet."
EXACT_REPEAT_REQUESTS = {
    '你刚才说什么', '你刚刚说什么', '刚才说什么', '再说一遍', '再说一次',
    '请再说一遍', '能再说一遍吗', '可以再说一遍吗', '用英语再说一遍',
    '我没听清', '没听清',
    'say again', 'please say again', 'can you say that again',
    'could you say that again', 'what did you say', 'what did you just say',
    'pardon',
}
EXACT_SLOW_REQUESTS = {'慢一点', '说慢一点', '请说慢一点', '太快了', 'too fast', 'speak slower', 'please speak slower'}
EXACT_CHAT_REQUESTS = {'陪练模式', '进入陪练模式', '开始陪练', '开始陪练模式',
                       '进入对话陪练模式', '切换到对话模式', '开始对话陪练',
                       '开始陪我聊天', 'let us chat in english', "let's chat in english",
                       # Older entry phrases all enter the same conversation.
                       '开始吃饭陪练', '进入吃饭场景', '练习吃饭英语',
                       '开始出门陪练', '进入出门场景', '练习出门英语',
                       '开始收拾陪练', '开始收拾物品陪练', '进入收拾场景',
                       '进入收拾物品场景', '练习收拾东西的英语'}
EXACT_EXPRESS_REQUESTS = {'回到生活表达模式', '切换到表达模式', '退出对话陪练',
                          '退出对话模式', '结束场景陪练', '退出场景陪练', 'back to expression mode',
                          '结束陪练', '退出陪练', '退出陪练模式', '表达模式', '生活表达模式',
                          '关闭陪练模式', '关掉陪练模式',
                          '只告诉我英文', '只给我英文表达'}
EXACT_CORRECTION_REQUESTS = {'不是这个意思', '我不是这个意思', '不是 我不是这个意思',
                             '你理解错了', '你说错了', "no that's not what i meant"}
CORRECTION_QUESTION = '你原本想表达什么？'
CLARIFICATION_TTL_S = 120
HELP_CONTEXT_TTL_S = 120


async def send_teaching_mode(conn):
    """Optional authoritative state for displays; no inference or speech call."""
    if getattr(conn, 'mvp_reports_teaching_mode', False):
        await conn.websocket.send(json.dumps({
            'type': 'teaching_mode', 'mode': conn.mvp_teaching_mode,
        }))


def get_clarification_context(conn, now=None):
    """Only a recently confirmed question can give meaning to a short answer."""
    context = getattr(conn, 'mvp_clarification_context', None)
    if context is not None:
        elapsed = (time.monotonic() if now is None else now) - conn.mvp_clarification_at
        if elapsed < 0 or elapsed >= CLARIFICATION_TTL_S:
            conn.mvp_clarification_context = None
            return None
    return context


def get_help_context(conn, now=None):
    """Remember only the latest explanation that the device confirmed playing."""
    context = getattr(conn, 'mvp_help_context', None)
    if context is not None:
        elapsed = (time.monotonic() if now is None else now) - conn.mvp_help_at
        if elapsed < 0 or elapsed >= HELP_CONTEXT_TTL_S:
            conn.mvp_help_context = None
            return None
    return context


def is_exact_repeat_request(transcript):
    normalized = re.sub(r'[\s\u3000，。！？、,.!?;:；：]+', ' ', transcript.casefold()).strip()
    return normalized in EXACT_REPEAT_REQUESTS


def is_exact_request(transcript, phrases):
    normalized = re.sub(r'[\s\u3000，。！？、,.!?;:；：]+', ' ', transcript.casefold()).strip()
    return normalized in phrases


def append_played_wav(explanation_wav, previous_wav):
    """Play the help, a short pause, then the exact previously heard audio."""
    with wave.open(io.BytesIO(explanation_wav), 'rb') as help_audio, \
         wave.open(io.BytesIO(previous_wav), 'rb') as original_audio:
        parameters = (help_audio.getnchannels(), help_audio.getsampwidth(),
                      help_audio.getframerate(), help_audio.getcomptype())
        original_parameters = (original_audio.getnchannels(), original_audio.getsampwidth(),
                               original_audio.getframerate(), original_audio.getcomptype())
        if parameters != (1, 2, 24000, 'NONE') or original_parameters != parameters:
            raise ValueError('incompatible_recap_audio')
        if help_audio.getnframes() + original_audio.getnframes() > 24000 * 45:
            raise ValueError('recap_audio_too_long')
        pcm = (help_audio.readframes(help_audio.getnframes()) +
               b'\0\0' * 4800 + original_audio.readframes(original_audio.getnframes()))
    output = io.BytesIO()
    with wave.open(output, 'wb') as joined:
        joined.setnchannels(1)
        joined.setsampwidth(2)
        joined.setframerate(24000)
        joined.writeframes(pcm)
    return output.getvalue()


def confirm_playback(conn, source):
    event(conn, 'playback_confirmed', source=source)
    intent = getattr(conn, 'mvp_pending_intent', None)
    spoken = getattr(conn, 'mvp_pending_english', None)
    if intent in ('repeat', 'slow', 'partial_repeat', 'hint'):
        # A confirmed return to English changes what a short meaning question
        # refers to; an older, unrelated explanation must not take precedence.
        conn.mvp_help_context = None
    used_context = getattr(conn, 'mvp_pending_used_clarification', None)
    if used_context is not None and getattr(conn, 'mvp_clarification_context', None) is used_context:
        conn.mvp_clarification_context = None
    if intent in ('express', 'chat', 'mode'):
        conn.mvp_last_english = spoken
        conn.mvp_last_wav = conn.mvp_pending_wav
        conn.mvp_last_transcript = conn.mvp_pending_transcript
        conn.mvp_correction_context = None
        conn.mvp_clarification_context = None
        conn.mvp_help_context = None
        conn.mvp_repeat_streak = 0
        if intent == 'chat' and conn.mvp_pending_transcript:
            history = getattr(conn, 'mvp_dialogue_context', [])
            conn.mvp_dialogue_context = (history + [{
                'child': conn.mvp_pending_transcript, 'assistant': spoken,
            }])[-4:]
    elif intent == 'clarify':
        conn.mvp_clarification_context = getattr(conn, 'mvp_pending_clarification_context', None)
        conn.mvp_clarification_at = time.monotonic()
        conn.mvp_repeat_streak = 0
    elif intent in ('explain', 'explain_only'):
        conn.mvp_help_context = getattr(conn, 'mvp_pending_help_context', None)
        conn.mvp_help_at = time.monotonic()
        conn.mvp_repeat_streak = 0
    elif spoken and spoken == getattr(conn, 'mvp_last_english', None):
        if intent == 'repeat':
            conn.mvp_repeat_streak = min(getattr(conn, 'mvp_repeat_streak', 0) + 1, 2)
        elif intent == 'slow':
            conn.mvp_repeat_streak = 2
        else:
            conn.mvp_repeat_streak = 0
    else:
        conn.mvp_repeat_streak = 0

def event(conn, name, **fields):
    if name == 'request_complete':
        conn.mvp_request_finished = True
    row = {'request_id': getattr(conn, 'mvp_request_id', None), 'event': name, 'time': time.time(), **fields}
    policy = getattr(getattr(conn, 'server', None), 'cloud_policy', None)
    if policy is not None:
        policy.record(conn, row)
        # Raw text lives only in the protected, retained SQLite learning record.
        public_row = {k: v for k, v in row.items() if k != 'text'}
        conn.logger.bind(tag='english_mvp').info(json.dumps(public_row, ensure_ascii=False))
        return
    conn.logger.bind(tag='english_mvp').info(json.dumps(row, ensure_ascii=False))
    path = Path(conn.config['english_mvp']['log_path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps(row, ensure_ascii=False)+'\n')


def cancel_unprocessed_request(conn, reason):
    duplex = getattr(conn, 'mvp_duplex_session', None)
    if duplex is not None:
        asyncio.create_task(duplex.close())
    stream = getattr(conn, 'mvp_asr_stream', None)
    if stream is not None:stream.cancel()
    pending = getattr(conn, 'mvp_processing_pending', False)
    if getattr(conn, 'mvp_recording', False) or pending:
        frame_count = conn.mvp_pending_frames if pending else len(conn.asr_audio)
        conn.mvp_recording = False
        conn.mvp_processing_pending = False
        conn.asr_audio.clear()
        event(conn, 'error', error=reason)
        # Decode/length errors happen before process() owns the request. Give
        # those requests the same terminal timing record as provider failures.
        elapsed = (time.perf_counter()-conn.mvp_started)*1000
        timings = dict(audio_upload_ms=elapsed, asr_ms=None, english_ms=None,
                       tts_ms=None, return_ms=None, total_ms=elapsed)
        event(conn, 'audio_received', frames=frame_count, **timings)
        event(conn, 'request_complete', success=False, **timings)


async def check_timeouts(conn):
    """Reuse the connection watchdog; never auto-submit partial speech."""
    now = time.perf_counter()
    reason = None
    if not getattr(conn, 'mvp_hello', False):
        if now - conn.mvp_connected_at >= HELLO_TIMEOUT_S:
            reason = 'hello_timeout'
    elif getattr(conn, 'mvp_recording', False):
        if now - conn.mvp_started >= RECORDING_TIMEOUT_S:
            reason = 'recording_submit_timeout'
    else:
        task = getattr(conn, 'mvp_task', None)
        # Providers already have bounded worker deadlines. Do not cut a valid
        # cold inference off just because no new microphone packets arrive.
        if not (task and not task.done()):
            if now - conn.mvp_last_activity >= HEARTBEAT_TIMEOUT_S:
                reason = 'heartbeat_timeout'
    if reason is None:
        return False
    conn.mvp_closing = True
    cancel_unprocessed_request(conn, reason)
    event(conn, 'connection_timeout', request_id=None,
          connection_id=conn.mvp_connection_id, reason=reason)
    # The upstream connection finally block owns cleanup and cancels this
    # watchdog. Calling conn.close() here would make the task await itself.
    await conn.websocket.close(code=1001, reason=reason)
    return True


async def fail(conn, reason):
    conn.mvp_auto_listening = False
    if conn.mvp_recording or getattr(conn, 'mvp_processing_pending', False):
        cancel_unprocessed_request(conn, reason)
    else:
        event(conn, 'error', error=reason)
    # A stopped/failed background sender can leave queue_empty_event unset.
    # Reuse the upstream reset before its stop helper waits for queue drain.
    if getattr(conn, 'audio_rate_controller', None):
        conn.audio_rate_controller.reset()
    conn.mvp_audio_returning = False
    conn.mvp_streaming_tts = False
    policy = getattr(getattr(conn, 'server', None), 'cloud_policy', None)
    recovery = (policy.error_response(reason) if callable(getattr(policy, 'error_response', None))
                else {'error_code': 'other_error', 'retry_after_ms': 3000})
    message = recovery['error_code'] if callable(getattr(policy, 'error_response', None)) else reason
    await conn.websocket.send(json.dumps({'type':'error', 'request_id':conn.mvp_request_id,
        'error':message, 'terminal':True, **recovery}))
    await send_tts_message(conn, 'stop')


def begin_request(conn):
    conn.mvp_request_id = uuid.uuid4().hex
    conn.sentence_id = conn.mvp_request_id
    conn.mvp_started = time.perf_counter()
    conn.mvp_request_finished = False
    conn.mvp_recording = True
    conn.mvp_playback_reported = False
    conn.mvp_playback_completed = False
    conn.mvp_pending_english = None
    conn.mvp_pending_intent = None
    conn.mvp_pending_wav = None
    conn.mvp_pending_transcript = None
    conn.mvp_pending_clarification_context = None
    conn.mvp_pending_used_clarification = None
    conn.mvp_pending_help_context = None
    conn.mvp_audio_returning = False
    conn.mvp_streaming_tts = False
    conn.mvp_cancel_output = False
    conn.mvp_processing_pending = False
    conn.client_abort = False
    conn.mvp_asr_stream = None
    conn.mvp_stream_error = None
    conn.mvp_reservation = None
    event(conn, 'recording_started', mode=conn.client_listen_mode)


def feed_asr_stream(conn):
    """Reuse the current provider after the same signal gate and budget admission."""
    if not getattr(conn.asr, 'supports_input_streaming', False) or conn.mvp_stream_error is not None:
        return
    stream = conn.mvp_asr_stream
    if stream is not None:
        stream.feed(conn.asr_audio[-1])
        return
    pcm = b''.join(conn.asr_audio)
    if rejection_reason(np.frombuffer(pcm,dtype='<i2')):return
    policy = getattr(getattr(conn,'server',None),'cloud_policy',None)
    try:
        if policy is not None:
            conn.mvp_reservation = policy.before_turn(conn,15)
            event(conn,'cost_reserved',**conn.mvp_reservation)
        event(conn,'asr_started',input_streamed=True)
        conn.mvp_asr_stream = conn.asr.start_stream(conn.mvp_request_id)
        if hasattr(conn.mvp_asr_stream, 'on_update'):
            async def progress(snapshot):
                # Partial snapshots are diagnostic metadata, never translated
                # or appended to learning text before the whole turn ends.
                if not snapshot['final'] and snapshot['updates'] != 1:
                    return
                event(conn, 'asr_progress', updates=snapshot['updates'],
                    revisions=snapshot['revisions'], definite_segments=snapshot['definite_segments'],
                    final=snapshot['final'], asr_first_result_ms=snapshot['first_result_ms'])
            conn.mvp_asr_stream.on_update = progress
        for frame in conn.asr_audio:conn.mvp_asr_stream.feed(frame)
    except Exception as exc:
        # Final-turn error handling reports admission failures without a retry.
        conn.mvp_stream_error = exc


async def submit(conn):
    if getattr(conn,'mvp_asr_stream',None) is not None:conn.mvp_asr_stream.finish()
    conn.mvp_submitted_at = time.perf_counter()
    conn.mvp_recording = False
    conn.mvp_auto_listening = False
    frames, conn.asr_audio = conn.asr_audio, []
    conn.mvp_processing_pending = True
    conn.mvp_pending_frames = len(frames)
    conn.mvp_task = asyncio.create_task(process(conn, frames))


async def auto_audio(conn, pcm):
    # Reuse the upstream Silero provider's recurrent state, hysteresis and
    # silence detector. Only bounded pre-roll and turn ownership live here.
    start = time.perf_counter()
    voice = conn.vad.is_vad(conn, pcm)
    conn.mvp_vad_ms += (time.perf_counter() - start) * 1000
    conn.mvp_vad_frames += 1
    if voice is None:
        await fail(conn, 'vad_failed')
        return
    if not conn.mvp_recording:
        conn.mvp_preroll.append(pcm)
        if not voice:
            return
        begin_request(conn)
        conn.asr_audio = list(conn.mvp_preroll)
        conn.mvp_preroll.clear()
        event(conn, 'vad_speech_started', pre_roll_frames=len(conn.asr_audio))
    else:
        conn.asr_audio.append(pcm)
    feed_asr_stream(conn)
    if len(conn.asr_audio) >= 250:
        await fail(conn, 'audio_too_long_or_packet_too_large')
    elif conn.client_voice_stop:
        event(conn, 'vad_speech_stopped', vad_ms=conn.mvp_vad_ms,
              evaluated_frames=conn.mvp_vad_frames,
              silence_ms=conn.vad.silence_threshold_ms)
        await submit(conn)


async def handle_message(conn, message):
    if getattr(conn, 'mvp_closing', False):
        return
    if isinstance(message, str):
        try:control = json.loads(message)
        except ValueError:control = None  # Existing validation owns malformed messages.
        if isinstance(control, dict) and control.get('type') == 'mcp':
            conn.mvp_last_activity = time.perf_counter()
            policy = getattr(getattr(conn, 'server', None), 'cloud_policy', None)
            if policy is not None and hasattr(policy, 'device_control'):
                policy.device_control.receive(conn.device_id, conn.websocket, control.get('payload'))
            return
    duplex = getattr(conn, 'mvp_duplex_session', None)
    if duplex is not None:
        await duplex.handle(message)
        return
    if not hasattr(conn, 'mvp_request_id'):
        conn.mvp_request_id = None
        conn.mvp_recording = False
        conn.mvp_task = None
        conn.mvp_hello = False
    if isinstance(message, bytes):
        if not conn.mvp_recording and not getattr(conn, 'mvp_auto_listening', False):
            return
        # Fixed upstream v1: 16 kHz, mono, 60 ms. Limit to 15 s.
        if len(conn.asr_audio) >= 250 or len(message) > 1275:
            await fail(conn, 'audio_too_long_or_packet_too_large')
            return
        pcm = conn._decode_opus_packet(message)
        if pcm is None or len(pcm) != 1920:
            await fail(conn, 'audio_decode_failed')
        elif getattr(conn, 'mvp_auto_listening', False):
            await auto_audio(conn, pcm)
        else:
            conn.asr_audio.append(pcm)
            feed_asr_stream(conn)
        return
    try:
        data = json.loads(message)
        if not isinstance(data, dict):
            raise ValueError('expected_object')
        kind = data.get('type')
        if kind == 'hello':
            expected = {'format':'opus', 'sample_rate':16000, 'channels':1, 'frame_duration':60}
            if data.get('version', 1) != 1 or data.get('audio_params') != expected:
                raise ValueError('unsupported_audio_parameters')
            if getattr(conn, 'mvp_hello', False):
                # Re-handshaking ends the old teaching session. Let bounded
                # inference finish under abort; cancel only network output.
                conn.client_abort = True
                cancel_unprocessed_request(conn, 'session_restarted')
                task = getattr(conn, 'mvp_task', None)
                if (task and not task.done() and getattr(conn, 'mvp_audio_returning', False)
                        and not getattr(conn, 'mvp_cancel_output', False)):
                    conn.mvp_cancel_output = True
                    task.cancel()
            conn.mvp_hello = True
            features = data.get('features')
            conn.mvp_requires_completion = (
                isinstance(features, dict) and features.get('mvp_playback_completed') is True)
            conn.mvp_reports_teaching_mode = (
                isinstance(features, dict) and features.get('mvp_teaching_mode') is True)
            conn.mvp_teaching_mode = 'express'
            policy = getattr(getattr(conn, 'server', None), 'cloud_policy', None)
            if policy is not None and hasattr(policy, 'device_control'):
                policy.device_control.attach(conn, features)
            conn.mvp_audio_returning = False
            conn.mvp_dialogue_context = []
            conn.mvp_clarification_context = None
            conn.mvp_help_context = None
            conn.mvp_correction_context = None
            # Keep server's downstream 24 kHz; upstream hello otherwise echoes input params.
            await conn.websocket.send(json.dumps(conn.welcome_msg))
            await send_teaching_mode(conn)
            if isinstance(features, dict) and features.get('mvp_duplex') is True:
                accepted = (features.get('mvp_aec_reference') is True
                    and conn.mvp_requires_completion and conn.config['english_mvp'].get('cloud') is True)
                await conn.websocket.send(json.dumps({'type':'duplex_ready','enabled':accepted,
                    'reason':'aec_reference_required' if not accepted else 'environment_english',
                    'queue_limit':3}))
                if accepted:
                    from core.handle.englishDuplex import DuplexSession
                    conn.mvp_duplex_session = DuplexSession(conn)
        elif kind == 'listen':
            if not conn.mvp_hello:
                raise ValueError('hello_required')
            if (data.get('state') == 'stop' and not conn.mvp_recording
                    and (conn.mvp_task is not None
                         or getattr(conn, 'mvp_request_finished', False))):
                # A stop can already be in flight when invalid audio is rejected,
                # or repeat after processing began. Do not emit another reply.
                return
            if conn.mvp_task and not conn.mvp_task.done():
                await conn.websocket.send(json.dumps({'type':'error','error':'busy',
                    'request_id':conn.mvp_request_id, 'terminal':False}))
                return
            if data.get('state') == 'start':
                mode = data.get('mode')
                if mode not in ('manual', 'auto') or (mode == 'auto' and conn.vad is None):
                    raise ValueError('listen_mode_unavailable')
                if conn.mvp_recording or getattr(conn, 'mvp_auto_listening', False):
                    # Repeated button start must not discard the current capture.
                    # Keep its request ID, decoder state, frames and start time.
                    event(conn, 'control_ignored', control='listen:start', reason='already_recording')
                    return
                conn.client_listen_mode = mode
                conn.reset_audio_states()
                conn._init_connection_state(conn)
                conn._connection_opus_decoder.reset_state()
                conn.mvp_auto_listening = mode == 'auto'
                if mode == 'auto':
                    conn.mvp_request_id = None
                    conn.mvp_request_finished = False
                    conn.vad.release_conn_resources(conn)
                    conn.mvp_preroll = deque(maxlen=6)  # 360 ms at 60 ms/frame
                    conn.mvp_vad_ms = 0.0
                    conn.mvp_vad_frames = 0
                    event(conn, 'vad_listening', request_id=None)
                else:
                    begin_request(conn)
            elif data.get('state') == 'stop':
                if not conn.mvp_recording:
                    if getattr(conn, 'mvp_auto_listening', False):
                        conn.mvp_auto_listening = False
                        await send_tts_message(conn, 'stop')
                        return
                    raise ValueError('not_recording')
                await submit(conn)
        elif kind == 'mvp_playback':
            if (data.get('request_id') == conn.mvp_request_id
                    and data.get('event') == 'playback_started'
                    and data.get('source') == 'codec_output_returned'
                    and getattr(conn, 'mvp_audio_returning', False)
                    and not getattr(conn, 'client_abort', False)
                    and not getattr(conn, 'mvp_playback_reported', False)):
                conn.mvp_playback_reported = True
                event(conn, 'playback_started', source='codec_output_returned',
                      stop_to_playback_ack_ms=(time.perf_counter()-conn.mvp_submitted_at)*1000
                          if hasattr(conn, 'mvp_submitted_at') else None,
                      received_after_start_ms=(time.perf_counter()-conn.mvp_started)*1000)
                if not getattr(conn, 'mvp_requires_completion', False):
                    confirm_playback(conn, 'legacy_playback_started')
            elif (data.get('request_id') == conn.mvp_request_id
                  and data.get('event') == 'playback_completed'
                  and data.get('source') == 'codec_queue_drained'
                  and getattr(conn, 'mvp_requires_completion', False)
                  and getattr(conn, 'mvp_audio_returning', False)
                  and not getattr(conn, 'mvp_streaming_tts', False)
                  and not getattr(conn, 'client_abort', False)
                  and getattr(conn, 'mvp_playback_reported', False)
                  and not getattr(conn, 'mvp_playback_completed', False)):
                conn.mvp_playback_completed = True
                event(conn, 'playback_completed', source='codec_queue_drained')
                confirm_playback(conn, 'codec_queue_drained')
        elif kind == 'abort':
            conn.mvp_auto_listening = False
            conn.client_abort = True
            # Let bounded inference finish; suppress its audio. No unsafe thread cancellation.
            if conn.mvp_recording:
                await fail(conn, 'request_aborted')
            elif (conn.mvp_task and not conn.mvp_task.done()
                  and conn.mvp_audio_returning and not conn.mvp_cancel_output):
                # Only the network-output phase is cancellable: native model
                # calls still finish within their existing worker deadline.
                conn.mvp_cancel_output = True
                conn.mvp_task.cancel()
        elif kind == 'ping':
            await conn.websocket.send(json.dumps({'type':'pong'}))
    except (ValueError, KeyError, TypeError) as exc:
        if not conn.mvp_request_id:
            conn.mvp_request_id = uuid.uuid4().hex
        if conn.mvp_task and not conn.mvp_task.done():
            # A malformed control frame must not send a premature TTS stop while
            # the valid request is still producing audio on this connection.
            event(conn, 'control_error', error=str(exc))
            await conn.websocket.send(json.dumps({'type': 'error', 'error': str(exc),
                                                  'request_id': conn.mvp_request_id, 'terminal': False}))
            return
        await fail(conn, str(exc))


async def process(conn, frames):
    conn.mvp_processing_pending = False
    timings = dict(audio_upload_ms=(time.perf_counter()-conn.mvp_started)*1000,
                   asr_ms=None, english_ms=None, tts_ms=None, return_ms=None, total_ms=None)
    event(conn, 'audio_received', frames=len(frames), **timings)
    success = False
    incremental = None
    try:
        if conn.client_abort or conn.stop_event.is_set():
            raise ValueError('request_aborted')
        if not frames:
            raise ValueError('empty_audio')
        if conn.client_listen_mode == 'auto':
            # Existing TTS start pauses microphone capture before inference.
            # Firmware resumes only after stop and physical playback drain.
            await send_tts_message(conn, 'start')
        # Diagnose real microphone levels without retaining raw speech or adding VAD.
        samples = np.frombuffer(b''.join(frames), dtype='<i2').astype(np.float64)
        if samples.size:
            rms = float(np.sqrt(np.mean(samples * samples)))
            event(conn, 'audio_signal', samples=int(samples.size),
                  peak=float(np.max(np.abs(samples))), rms=rms,
                  rms_dbfs=float(20 * np.log10(rms / 32768)) if rms else None,
                  nonzero_ratio=float(np.count_nonzero(samples) / samples.size),
                  clipped_ratio=float(np.mean(np.abs(samples) >= 32767)))
        if diagnostic_capture is not None:
            try:
                saved = diagnostic_capture.save(conn.mvp_request_id, frames)
                if saved is not None:
                    event(conn, 'diagnostic_audio_saved', filename=saved.name)
            except (OSError, ValueError) as exc:
                # Diagnostic disk failure must not interrupt speech processing.
                event(conn, 'diagnostic_audio_error', error=type(exc).__name__)
        rejected = rejection_reason(samples)
        if rejected:
            # Reject before the shared model worker/queue, using its same gate.
            # Keep the existing error + tts:stop recovery protocol in both modes.
            event(conn, 'audio_rejected', reason=rejected,
                  duration_ms=samples.size / 16, model_called=False)
            raise ValueError('empty_or_silent_audio')
        event(conn, 'recording_duration', audio_duration_ms=samples.size / 16,
              vad_stop_ms=None, network_upload_ms=None)
        # Cloud adapter performs persistent reservation before any paid provider call.
        policy = getattr(getattr(conn, 'server', None), 'cloud_policy', None)
        if getattr(conn,'mvp_stream_error',None) is not None:
            raise conn.mvp_stream_error
        if policy is not None and getattr(conn,'mvp_reservation',None) is None:
            reservation = policy.before_turn(conn, samples.size / 16000)
            event(conn, 'cost_reserved', **reservation)
        start = time.perf_counter()
        if getattr(conn,'mvp_asr_stream',None) is not None:
            chinese, asr_usage = await conn.mvp_asr_stream.task
        else:
            event(conn, 'asr_started')
            chinese, asr_usage = await conn.asr.speech_to_text(frames, conn.mvp_request_id, 'pcm')
        timings['asr_ms'] = (time.perf_counter()-start)*1000
        if not chinese or not chinese.strip():
            raise ValueError('empty_asr')
        event(conn, 'asr_result', text=chinese, asr_ms=timings['asr_ms'])
        # Show the recognized source while English is generated. This existing
        # display-only message must not start playback or confirm a spoken turn.
        await send_display_message(conn, chinese)
        if isinstance(asr_usage, dict):
            event(conn, 'asr_usage', **asr_usage)
        if conn.client_abort or conn.stop_event.is_set():
            raise ValueError('request_aborted')
        previous_english = (await asyncio.to_thread(policy.last_played_english, conn.device_id)
                            if policy is not None else getattr(conn, 'mvp_last_english', None))
        if conn.client_abort or conn.stop_event.is_set():
            raise ValueError('request_aborted')
        start = time.perf_counter()
        event(conn, 'english_started')
        clarification_context = get_clarification_context(conn)
        used_clarification = False
        intent = 'express'
        if is_exact_repeat_request(chinese):
            intent = 'repeat'
            english = previous_english or REPEAT_WITHOUT_CONTEXT
            event(conn, 'llm_usage', provider='local-repeat', input_tokens=0, output_tokens=0)
        elif is_exact_request(chinese, EXACT_SLOW_REQUESTS):
            intent = 'slow'
            english = previous_english or SLOW_WITHOUT_CONTEXT
            event(conn, 'llm_usage', provider='local-slow', input_tokens=0, output_tokens=0)
        elif is_exact_request(chinese, EXACT_CHAT_REQUESTS):
            conn.mvp_teaching_mode = 'chat'
            conn.mvp_dialogue_context = []
            conn.mvp_clarification_context = None
            conn.mvp_help_context = None
            conn.mvp_correction_context = None
            intent = 'mode'
            english = "Let's chat in English."
            event(conn, 'llm_usage', provider='local-mode', input_tokens=0, output_tokens=0)
            event(conn, 'teaching_mode_changed', mode='chat')
            await send_teaching_mode(conn)
        elif is_exact_request(chinese, EXACT_EXPRESS_REQUESTS):
            conn.mvp_teaching_mode = 'express'
            conn.mvp_dialogue_context = []
            conn.mvp_clarification_context = None
            conn.mvp_help_context = None
            conn.mvp_correction_context = None
            intent = 'mode'
            english = 'Back to everyday English.'
            event(conn, 'llm_usage', provider='local-mode', input_tokens=0, output_tokens=0)
            event(conn, 'teaching_mode_changed', mode='express')
            await send_teaching_mode(conn)
        elif is_exact_request(chinese, EXACT_CORRECTION_REQUESTS):
            conn.mvp_clarification_context = None
            intent = 'correct'
            english = CORRECTION_QUESTION
            event(conn, 'llm_usage', provider='local-correction', input_tokens=0, output_tokens=0)
        elif hasattr(conn.llm, 'teaching_response'):
            correction_context = getattr(conn, 'mvp_correction_context', None)
            arguments = (chinese, conn.mvp_request_id, previous_english)
            if correction_context:
                arguments += (correction_context,)
            options = {}
            help_context = get_help_context(conn)
            translation_help = getattr(conn.llm, 'meaning_requests_as_translation', False)
            if not translation_help and help_context and direct_help_request(chinese, previous_english, help_context):
                options['help_context'] = help_context
                event(conn, 'help_context_used')
            if clarification_context:
                options['clarification_context'] = clarification_context
                used_clarification = True
                event(conn, 'clarification_context_used', turns=len(clarification_context['turns']))
            event(conn, 'teaching_mode_used', mode=getattr(conn, 'mvp_teaching_mode', 'express'))
            if getattr(conn, 'mvp_teaching_mode', 'express') == 'chat':
                options.update(mode='chat', dialogue_context=getattr(conn, 'mvp_dialogue_context', []))
            if (getattr(conn.llm, 'supports_text_streaming', False)
                    and getattr(conn.tts, 'supports_incremental_input', False)
                    and conn.sample_rate == 24000
                    and getattr(conn, 'mvp_requires_completion', False)
                    and not correction_context and not clarification_context
                    and (translation_help or not direct_help_request(chinese, previous_english, help_context))
                    and not is_silent_expression_candidate(chinese,
                        getattr(conn, 'mvp_teaching_mode', 'express'))
                    and not re.search(r'(?:是不是|是否|是)[\s。.!！?？…]*$', chinese)):
                incremental = IncrementalSpeech(conn, event, start)
                options['on_text'] = incremental.on_text
            intent, generated, llm_usage = await asyncio.to_thread(
                conn.llm.teaching_response, *arguments, **options)
            english = ((previous_english or REPEAT_WITHOUT_CONTEXT) if intent == 'repeat' else
                       (previous_english or SLOW_WITHOUT_CONTEXT) if intent == 'slow' else generated)
            event(conn, 'llm_usage', **llm_usage)
            if incremental and incremental.task is not None and intent not in ('express', 'chat'):
                raise ValueError('streamed_intent_changed')
        elif hasattr(conn.llm, 'response_result'):
            english, llm_usage = await asyncio.to_thread(conn.llm.response_result, chinese, conn.mvp_request_id)
            event(conn, 'llm_usage', **llm_usage)
        else:
            english = await asyncio.to_thread(lambda: ''.join(conn.llm.response(conn.session_id, [{'role':'user','content':chinese}])))
        if conn.client_abort or conn.stop_event.is_set():
            raise ValueError('request_aborted')
        if (intent == 'repeat' and previous_english and
                getattr(conn, 'mvp_repeat_streak', 0) >= 1):
            intent = 'slow'
            english = previous_english
            event(conn, 'help_adjusted', from_intent='repeat', to_intent='slow',
                  reason='repeat_after_confirmed_replay')
        timings['english_ms'] = (time.perf_counter()-start)*1000
        if (intent == 'express' and isinstance(english, str) and len(english) <= 400 and
                is_unchanged_english_expression(chinese, english,
                    getattr(conn, 'mvp_teaching_mode', 'express'),
                    getattr(conn, 'mvp_correction_context', None))):
            intent, english = 'no_change', ''
        if intent == 'no_change':
            if english != '' or not is_silent_expression_candidate(chinese,
                    getattr(conn, 'mvp_teaching_mode', 'express'),
                    getattr(conn, 'mvp_correction_context', None)):
                raise ValueError('invalid_no_change')
            event(conn, 'intent_classified', intent=intent)
            event(conn, 'speech_not_needed', reason='natural_english_expression')
            # Known zero synthesis usage, without starting a provider/billing stage.
            event(conn, 'tts_usage', provider='not-needed', input_characters=0,
                  billed_text_words=0)
            conn.mvp_clarification_context = None
            conn.mvp_help_context = None
            # This existing caption message updates the display without TTS start.
            await send_tts_message(conn, 'sentence_start', '无需重复')
            conn.clearSpeakStatus()
            await conn.websocket.send(json.dumps({'type': 'tts', 'state': 'stop',
                'session_id': conn.session_id, 'request_id': conn.mvp_request_id,
                'no_audio': True, 'reason': 'natural_english_expression'}))
            # Do not promote playback, overwrite heard English, or claim mastery.
            success = True
            return
        if intent not in ('express', 'chat', 'mode', 'repeat', 'slow', 'partial_repeat', 'hint',
                          'explain', 'explain_only', 'clarify', 'correct', 'repair'):
            raise ValueError('invalid_intent')
        if intent == 'partial_repeat' and (
            not previous_english or not english.strip() or english not in previous_english):
            raise ValueError('invalid_partial_repeat')
        if intent == 'hint' and (
            not previous_english or not english.strip() or
            not previous_english.startswith(english) or len(english) >= len(previous_english)
            or len(english) > 100 or
            (english[-1].isalpha() and previous_english[len(english)].isalpha())):
            raise ValueError('invalid_hint')
        if not english or len(english) > 400 or (
                intent not in ('explain', 'explain_only', 'clarify', 'correct') and
                re.search(r'[\u3400-\u9fff]', english)):
            raise ValueError('invalid_english')
        if intent in ('correct', 'repair'):
            conn.mvp_help_context = None
            if policy is not None:
                rejected = await asyncio.to_thread(policy.reject_last_expression, conn.device_id)
            elif getattr(conn, 'mvp_last_english', None):
                rejected = {'request_id': None, 'transcript': getattr(conn, 'mvp_last_transcript', None),
                            'english': conn.mvp_last_english}
            else:
                rejected = None
            if conn.client_abort or conn.stop_event.is_set():
                raise ValueError('request_aborted')
            if rejected is not None:
                source = rejected['transcript']
                if not isinstance(source, str) or len(source) > 400:
                    source = None
                conn.mvp_correction_context = {'transcript': source,
                                               'rejected_english': rejected['english']}
                event(conn, 'expression_rejected', rejected_request_id=rejected['request_id'])
            conn.mvp_last_english = None
            conn.mvp_last_wav = None
            conn.mvp_repeat_streak = 0
            if intent == 'repair':
                intent = 'express'
                event(conn, 'correction_applied')
        event(conn, 'intent_classified', intent=intent)
        recap = previous_english if intent == 'explain' and previous_english else None
        spoken_text = english + (' ' + recap if recap else '')
        if len(spoken_text) > 800:
            raise ValueError('explanation_too_long')
        event(conn, 'english_result', text=spoken_text, english_ms=timings['english_ms'])
        if conn.client_abort or conn.stop_event.is_set():
            raise ValueError('request_aborted')
        start = time.perf_counter()
        delivered = incremental is not None and incremental.task is not None
        if delivered:
            start = incremental.started_at
        else:
            event(conn, 'tts_started')
        stream_audio = (intent in ('express', 'chat') and conn.sample_rate == 24000
                        and getattr(conn, 'mvp_requires_completion', False)
                        and getattr(conn.tts, 'supports_pcm_streaming', False))
        packet_count = 0
        stream_started_at = None
        if stream_audio and not delivered:
            encoder = PCMOpusStream(conn.sample_rate)
            # A first-sample ACK is useful for latency, but cannot confirm the
            # utterance until synthesis succeeds and the final queue drains.
            conn.mvp_streaming_tts = True

            async def forward_pcm(chunk, *, final=False):
                nonlocal packet_count, stream_started_at
                if conn.client_abort or conn.stop_event.is_set():
                    raise ValueError('request_aborted')
                for packet in encoder.feed(chunk, final=final):
                    if stream_started_at is None:
                        stream_started_at = time.perf_counter()
                        await send_tts_message(conn, 'start')
                        conn.client_is_speaking = True
                        await send_tts_message(conn, 'sentence_start', spoken_text)
                        conn.mvp_audio_returning = True
                    if conn.client_abort or conn.stop_event.is_set():
                        raise ValueError('request_aborted')
                    await sendAudio(conn, packet)
                    packet_count += 1
                    if packet_count == 1:
                        event(conn, 'audio_first_packet',
                              tts_first_packet_ms=(time.perf_counter()-start)*1000,
                              stop_to_first_audio_ms=(time.perf_counter()-conn.mvp_submitted_at)*1000
                                  if hasattr(conn, 'mvp_submitted_at') else None)
                    # Native encoding stays on its owning loop; yield between
                    # frames so cancellation/heartbeat never wait for a chunk.
                    await asyncio.sleep(0)
        cached_wav = None
        if intent == 'repeat' and previous_english:
            cached_wav = (policy.last_played_audio(conn.device_id, previous_english)
                          if policy is not None else
                          getattr(conn, 'mvp_last_wav', None))
        if delivered:
            wav = await incremental.finish(english)
            packet_count = incremental.packet_count
            stream_started_at = incremental.first_packet_at
            await send_tts_message(conn, 'sentence_start', spoken_text)
        elif cached_wav:
            wav = cached_wav
            event(conn, 'tts_usage', provider='memory-replay', input_characters=0,
                  billed_text_words=0)
        elif intent == 'slow' and previous_english:
            wav = await conn.tts.text_to_speak(english, None, speech_rate=-25)
        elif intent == 'explain' and recap:
            recap_wav = (policy.last_played_audio(conn.device_id, recap)
                         if policy is not None else
                         getattr(conn, 'mvp_last_wav', None))
            if recap_wav:
                help_wav = await conn.tts.text_to_speak(english, None,
                                                        speaker=conn.tts.help_speaker)
                wav = append_played_wav(help_wav, recap_wav)
                event(conn, 'explanation_recap', source='cached_played_audio')
            else:
                wav = await conn.tts.text_to_speak(spoken_text, None,
                                                   speaker=conn.tts.help_speaker)
                event(conn, 'explanation_recap', source='spoken_text_fallback')
        elif intent in ('explain', 'explain_only', 'clarify', 'correct'):
            wav = await conn.tts.text_to_speak(english, None, speaker=conn.tts.help_speaker)
        elif stream_audio:
            wav = await conn.tts.text_to_speak(english, None, on_pcm=forward_pcm)
            # Flush only after the provider accepted the complete HTTP body.
            await forward_pcm(b'', final=True)
        else:
            wav = await conn.tts.text_to_speak(english, None)
        if not cached_wav and getattr(conn.tts, 'last_metrics', None):
            event(conn, 'tts_usage', **conn.tts.last_metrics)
        if not wav:
            raise ValueError('empty_tts')
        packets = []
        if not stream_audio:
            await asyncio.to_thread(audio_bytes_to_data_stream, wav, 'wav', True, packets.append, conn.sample_rate)
            packet_count = len(packets)
        if not packet_count:
            raise ValueError('empty_opus')
        if intent in ('explain', 'explain_only') and len(chinese) <= 400:
            conn.mvp_pending_help_context = {
                'child': chinese, 'assistant': english, 'replay_previous': bool(recap)}
        if intent == 'clarify' and len(chinese) <= 400:
            earlier = clarification_context['turns'] if used_clarification else []
            conn.mvp_pending_clarification_context = {
                'turns': (earlier + [{'child': chinese, 'assistant': english}])[-2:]}
        if intent in ('express', 'chat', 'mode'):
            conn.mvp_pending_wav = wav
            conn.mvp_pending_transcript = chinese
            if policy is not None:
                policy.cache_pending_audio(conn.device_id, conn.mvp_request_id, english, wav)
        timings['tts_ms'] = (time.perf_counter()-start)*1000
        event(conn, 'tts_generated', packets=packet_count, tts_ms=timings['tts_ms'])
        if conn.client_abort or conn.stop_event.is_set():
            raise ValueError('request_aborted')
        start = stream_started_at if stream_audio else time.perf_counter()
        if not stream_audio:
            await send_tts_message(conn, 'start')
            conn.client_is_speaking = True
            await send_tts_message(conn, 'sentence_start', spoken_text)
        if conn.client_abort or conn.stop_event.is_set():
            raise ValueError('request_aborted')
        conn.mvp_audio_returning = True
        conn.mvp_pending_english = english
        conn.mvp_pending_intent = intent
        conn.mvp_pending_used_clarification = clarification_context if used_clarification else None
        if not stream_audio:
            await asyncio.wait_for(sendAudio(conn, packets), timeout=60 if recap else 30)
        await asyncio.wait_for(send_tts_message(conn, 'stop'), timeout=30)
        timings['return_ms'] = (time.perf_counter()-start)*1000
        event(conn, 'audio_sent', return_ms=timings['return_ms'])
        success = True
    except asyncio.CancelledError:
        if getattr(conn, 'mvp_cancel_output', False):
            try:
                await fail(conn, 'request_aborted')
            except Exception:
                event(conn, 'error', error='connection_closed_during_error_response')
        else:
            event(conn, 'error', error='connection_closed_during_request')
            raise
    except Exception as exc:
        try:
            await fail(conn, f'{type(exc).__name__}: {exc}')
        except Exception:
            event(conn, 'error', error='connection_closed_during_error_response')
    finally:
        if incremental is not None:
            await incremental.cancel()
        timings['total_ms'] = (time.perf_counter()-conn.mvp_started)*1000
        if getattr(conn,'mvp_asr_stream',None) is not None:
            await conn.mvp_asr_stream.close()
            conn.mvp_asr_stream = None
        event(conn, 'request_complete', success=success, **timings)
        conn.mvp_last_activity = time.perf_counter()
