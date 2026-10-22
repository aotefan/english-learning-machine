import asyncio
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType
from core.providers.local_worker import worker

class ASRProvider(ASRProviderBase):
    def __init__(self, config, delete_audio_file=True):
        super().__init__()
        self.config = config
        self.interface_type = InterfaceType.LOCAL
        self.delete_audio_file = delete_audio_file

    async def speech_to_text(self, opus_data, session_id, audio_format='pcm'):
        text = await asyncio.to_thread(worker.call, 'asr', b''.join(opus_data), self.config)
        return text, None
