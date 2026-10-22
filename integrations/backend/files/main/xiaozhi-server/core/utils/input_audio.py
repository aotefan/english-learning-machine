"""The existing local ASR input gate, shared with transport preflight.

Input is mono signed 16-bit PCM at the MVP protocol's fixed 16 kHz.
This is a near-silence/length check, not speech or noise classification.
"""
import numpy as np


def rejection_reason(samples):
    if samples.size < 1600:  # Existing 100 ms minimum; do not extend for short words.
        return 'too_short'
    # Cast before abs: abs(int16(-32768)) would overflow.
    peak = float(np.max(np.abs(samples.astype(np.float32)))) / 32768
    if peak < 0.002:  # Preserve the existing local ASR peak threshold exactly.
        return 'silent_or_too_quiet'
    return None
