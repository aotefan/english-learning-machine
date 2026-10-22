"""Bounded bridge from the existing Ark worker to bidirectional TTS/Opus."""
import asyncio
import time
from core.handle.sendAudioHandle import sendAudio, send_tts_message
from core.utils.pcm_opus_stream import PCMOpusStream
from core.utils.speech_sentences import SpeechSentences


class IncrementalSpeech:
    def __init__(self, conn, record, english_started_at):
        self.conn = conn
        self.record = record
        self.english_started_at = english_started_at
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=32)
        self.task = None
        self.text = ''
        self.started_at = self.first_packet_at = None
        self.packet_count = 0
        self.encoder = PCMOpusStream(conn.sample_rate)

    def on_text(self, delta):
        # SDK remains on its worker; backpressure never blocks the serving loop.
        asyncio.run_coroutine_threadsafe(self.push(delta), self.loop).result(timeout=25)

    async def push(self, delta):
        if self.conn.client_abort or self.conn.stop_event.is_set():
            raise ValueError('request_aborted')
        if self.task is not None and self.task.done():
            self.task.result()
        if self.task is None:
            self.started_at = time.perf_counter()
            self.record(self.conn, 'english_first_output',
                llm_first_english_ms=(self.started_at-self.english_started_at)*1000)
            self.record(self.conn, 'tts_started', incremental_input=True)
            self.conn.mvp_streaming_tts = True
            self.task = asyncio.create_task(self.conn.tts.speak_incremental(
                self.source(), self.forward_pcm))
        self.text += delta
        await self.queue.put(delta)

    async def source(self):
        sentences = SpeechSentences()
        while True:
            item = await self.queue.get()
            if item is None:
                for sentence in sentences.finish():
                    yield sentence
                return
            for sentence in sentences.feed(item):
                yield sentence

    async def forward_pcm(self, chunk, *, final=False):
        if self.conn.client_abort or self.conn.stop_event.is_set():
            raise ValueError('request_aborted')
        for packet in self.encoder.feed(chunk, final=final):
            if self.first_packet_at is None:
                self.first_packet_at = time.perf_counter()
                await send_tts_message(self.conn, 'start')
                self.conn.client_is_speaking = True
                await send_tts_message(self.conn, 'sentence_start', self.text)
                self.conn.mvp_audio_returning = True
            await sendAudio(self.conn, packet)
            self.packet_count += 1
            if self.packet_count == 1:
                self.record(self.conn, 'audio_first_packet',
                    tts_first_packet_ms=(time.perf_counter()-self.started_at)*1000,
                    stop_to_first_audio_ms=(time.perf_counter()-self.conn.mvp_submitted_at)*1000)
            await asyncio.sleep(0)

    async def finish(self, english):
        if self.text.strip() != english.strip():
            await self.cancel()
            raise ValueError('streamed_english_mismatch')
        await self.queue.put(None)
        wav = await self.task
        await self.forward_pcm(b'', final=True)
        self.conn.mvp_streaming_tts = False
        return wav

    async def cancel(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.conn.mvp_streaming_tts = False
