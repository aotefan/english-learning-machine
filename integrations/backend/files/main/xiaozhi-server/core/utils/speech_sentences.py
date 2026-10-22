"""Keep incomplete model wording out of TTS; retain every sentence in order."""
import re


class SpeechSentences:
    def __init__(self):
        self.pending = ''

    def feed(self, delta):
        self.pending += delta
        ready = []
        # Wait for the following space, or finish(), before committing a dot.
        # This also leaves token-split decimal numbers and abbreviations intact.
        for match in re.finditer(r'''[.!?]["')\]]*(?=\s)\s*''', self.pending):
            end = match.end()
            prefix = self.pending[:match.start()+1]
            if match.group()[0] == '.' and re.search(
                    r'\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|[A-Z])\.$', prefix):
                continue
            ready.append(end)
        chunks = []
        start = 0
        for end in ready:
            chunks.append(self.pending[start:end])
            start = end
        self.pending = self.pending[start:]
        return chunks

    def finish(self):
        tail, self.pending = self.pending, ''
        return [tail] if tail else []
