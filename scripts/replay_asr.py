"""Replay local WAVs through the existing ASR provider without changing the server."""
import argparse
import json
import os
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'upstream/backend/main/xiaozhi-server'))
os.environ['HF_HUB_OFFLINE'] = '1'
from core.providers.local_models import LocalModels
from protocol_benchmark import read_input


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--input-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=2)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('rounds must be positive')
    files = sorted(args.input_dir.glob('*.wav'))
    if not files:
        parser.error('no WAV files')
    model = args.model.resolve()
    if not (model / 'config.json').is_file():
        parser.error('local model config missing')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    models = LocalModels()
    failed = False
    with args.output.open('x') as out:
        for repeat in range(args.rounds):
            for wav_path in files:
                row = {'input_id': wav_path.stem,
                       'model': model.name, 'round': repeat + 1,
                       'first_invocation': repeat == 0 and wav_path == files[0],
                       'scope': 'offline replay; not a new physical acceptance attempt'}
                try:
                    pcm, metadata = read_input(args.input_dir, wav_path.stem)
                    row['wav_sha256'] = metadata['input_sha256']
                    row['duration_s'] = metadata['input_duration_ms'] / 1000
                    start = time.perf_counter()
                    row['text'] = models.run('asr', pcm, {'engine': 'mlx_whisper', 'model_path': str(model)})
                    row['latency_ms'] = (time.perf_counter() - start) * 1000
                    row['success'] = True
                except Exception as exc:
                    row.update(success=False, error=f'{type(exc).__name__}: {exc}')
                    failed = True
                row['process_peak_rss_mib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
                # macOS process RSS is not the complete Metal memory footprint.
                line = json.dumps(row, ensure_ascii=False)
                out.write(line + '\n'); out.flush()
                print(line, flush=True)
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
