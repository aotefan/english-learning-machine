"""Decode only the committed intent and English JSON string prefix.

The complete source utterance has already been recognized. This never translates
partial ASR, never executes control intents, and never emits JSON punctuation.
"""
import json
import re


PREFIX = re.compile(r'^\s*\{\s*"intent"\s*:\s*"(express|chat)"\s*,\s*"english"\s*:\s*"')


def english_prefix(raw, mode):
    match = PREFIX.match(raw)
    if not match or match[1] != mode:
        return ''
    value = raw[match.end():]
    cursor = 0
    while cursor < len(value):
        char = value[cursor]
        if char == '"':
            break
        if char == '\\':
            if cursor + 1 >= len(value):
                break
            size = 6 if value[cursor+1] == 'u' else 2
            if cursor + size > len(value):
                break
            if size == 6:
                number = int(value[cursor+2:cursor+6], 16)
                if 0xd800 <= number <= 0xdbff:
                    if cursor + 12 > len(value):
                        break
                    if value[cursor+6:cursor+8] != '\\u':
                        raise ValueError('invalid_stream_surrogate')
                    size = 12
            cursor += size
        else:
            cursor += 1
    decoded = json.loads('"' + value[:cursor] + '"')
    if len(decoded) > 400 or any('\u3400' <= char <= '\u9fff' for char in decoded):
        raise ValueError('invalid_stream_english')
    if any(0xd800 <= ord(char) <= 0xdfff for char in decoded):
        raise ValueError('invalid_stream_surrogate')
    return decoded
