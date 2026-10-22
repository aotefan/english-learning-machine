from core.providers.llm.base import LLMProviderBase
from core.providers.local_worker import worker

class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.config = config

    def response(self, session_id, dialogue):
        # A translation provider uses only this utterance, never dialogue history.
        text = next(item['content'] for item in reversed(dialogue) if item['role']=='user')
        yield worker.call('english', text, self.config)
