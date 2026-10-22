"""Explicit, bounded local PCM capture for replaying the same hardware audio."""
import os
import re
import time
import wave
from pathlib import Path


class DiagnosticAudioCapture:
    def __init__(self, directory, limit=4, seconds=300):
        if not 1 <= limit <= 20 or not 1 <= seconds <= 900:
            raise ValueError('capture requires 1-20 requests and 1-900 seconds')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.remaining = limit
        self.deadline = time.monotonic() + seconds

    def save(self, request_id, frames):
        if self.remaining <= 0 or time.monotonic() >= self.deadline:
            return None
        if not re.fullmatch(r'[0-9a-f]{32}', request_id):
            raise ValueError('invalid capture request_id')
        pcm = b''.join(frames)
        if not pcm or len(pcm) % 2 or len(pcm) > 16000 * 2 * 15:
            raise ValueError('invalid capture PCM length')
        self.remaining -= 1
        path = self.directory / f'{request_id}.wav'
        # Exclusive creation avoids overwriting any earlier diagnostic recording.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            with wave.open(stream, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(pcm)
        return path
