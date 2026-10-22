import asyncio
from pathlib import Path
from core.providers.tts.base import TTSProviderBase
from core.providers.local_worker import worker

class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file=True):
        super().__init__(config, delete_audio_file)
        self.config = config

    async def text_to_speak(self, text, output_file):
        audio = await asyncio.to_thread(worker.call, 'tts', text, self.config)
        if output_file:
            Path(output_file).write_bytes(audio)
        else:
            return audio
