"""Profile installed local providers; excludes transport and never counts as hardware acceptance."""
import argparse
import cProfile
import datetime
import json
import os
from pathlib import Path
import platform
import pstats
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'upstream/backend/main/xiaozhi-server'))
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
from core.providers.local_models import LocalModels


def timed_profile(operation, path):
    profiler = cProfile.Profile()
    start = time.perf_counter()
    try:
        result = profiler.runcall(operation)
    finally:
        elapsed = (time.perf_counter() - start) * 1000
        profiler.dump_stats(str(path))
        with path.with_suffix('.txt').open('w') as out:
            pstats.Stats(profiler, stream=out).sort_stats('cumulative').print_stats(35)
    return result, elapsed


def imports(stage):
    # Same imports/order as LocalModels; subsequent run reuses Python's module cache.
    if stage == 'asr':
        import numpy
        import mlx_whisper
    elif stage == 'english':
        import torch
        from transformers import MarianMTModel, MarianTokenizer
    else:
        import numpy
        import torch
        import soundfile
        from kokoro import KModel, KPipeline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    models = LocalModels()
    with wave.open(str(ROOT / 'artifacts/synthetic/01.wav')) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        pcm = wav.readframes(wav.getnframes())
    report = {'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'python': sys.version, 'prefix': sys.prefix, 'machine': platform.machine(),
              'hardware': False, 'profiled': True,
              'scope': 'Default providers, fixed independent inputs; excludes worker spawn, WS, Opus and playback. cProfile adds overhead.',
              'stages': [], 'success': False}
    stages = [('asr', 'mlx_whisper', 'asr-mlx', pcm),
              ('english', 'marian', 'english-marian', '妈妈，我想喝水。'),
              ('tts', 'kokoro', 'tts-kokoro', 'Mom, I want some water.')]
    try:
        for stage, engine, folder, value in stages:
            config = {'engine': engine, 'model_path': str(ROOT / 'models' / folder)}
            _, import_ms = timed_profile(lambda: imports(stage), args.output / f'{stage}-import.pstats')
            result, first_ms = timed_profile(lambda: models.run(stage, value, config),
                                            args.output / f'{stage}-first.pstats')
            row = {'stage': stage, 'import_ms': import_ms, 'first_call_ms': first_ms,
                   'output': result if isinstance(result, str) else f'{len(result)} WAV bytes'}
            report['stages'].append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        # Finish all first calls before checking cached operation, like the server.
        for row, (stage, engine, folder, value) in zip(report['stages'], stages):
            _, row['warm_call_ms'] = timed_profile(
                lambda: models.run(stage, value, {'engine': engine, 'model_path': str(ROOT/'models'/folder)}),
                args.output / f'{stage}-warm.pstats')
        report['success'] = True
    finally:
        (args.output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
