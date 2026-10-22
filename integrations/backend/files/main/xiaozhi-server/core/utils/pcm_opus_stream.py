"""One utterance's PCM16 byte stream, with a single final Opus padding frame."""
import opuslib_next


class PCMOpusStream:
    def __init__(self, sample_rate=24000):
        self.frame_samples = sample_rate * 60 // 1000
        self.frame_bytes = self.frame_samples * 2
        self.encoder = opuslib_next.Encoder(sample_rate, 1, opuslib_next.APPLICATION_AUDIO)
        self.pending = bytearray()
        self.finished = False

    def feed(self, data, *, final=False):
        if self.finished:
            raise ValueError('opus_stream_already_finished')
        self.pending.extend(data)
        if final and len(self.pending) % 2:
            raise ValueError('invalid_pcm_alignment')
        count = len(self.pending) // self.frame_bytes
        for index in range(count):
            start = index * self.frame_bytes
            yield self.encoder.encode(
                bytes(self.pending[start:start + self.frame_bytes]), self.frame_samples)
        del self.pending[:count * self.frame_bytes]
        if final:
            if self.pending:
                frame = bytes(self.pending).ljust(self.frame_bytes, b'\0')
                yield self.encoder.encode(frame, self.frame_samples)
                self.pending.clear()
            self.finished = True
