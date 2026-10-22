"""Download pinned model snapshots. Runtime can then be fully offline."""
import argparse
import json
import os
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
from pathlib import Path
from huggingface_hub import snapshot_download, hf_hub_download
ROOT = Path(__file__).resolve().parents[1]
models = {
    'asr-mlx': ('mlx-community/whisper-small-mlx', ['*.json', '*.safetensors', '*.npz', 'README.md']),
    'english-marian': ('Helsinki-NLP/opus-mt-zh-en', ['*.json', '*.spm', 'pytorch_model.bin', 'README.md']),
    'english-qwen': ('mlx-community/Qwen2.5-1.5B-Instruct-4bit', ['*.json', '*.safetensors', 'tokenizer.model', 'README.md']),
    'tts-kokoro': ('hexgrad/Kokoro-82M', ['config.json', 'kokoro-v1_0.pth', 'voices/af_heart.pt', 'README.md', 'VOICES.md']),
}
if __name__ == '__main__':
    optional = {'asr-mlx-turbo': ('mlx-community/whisper-large-v3-turbo', ['*.json', '*.safetensors', 'README.md'])}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', choices=[*models, *optional, 'whisper-cpp'],
                        help='Download only this model; turbo is optional and excluded from the default download')
    args = parser.parse_args()
    selected = models if args.only is None else {k:v for k,v in {**models, **optional}.items() if k == args.only}
    for name, (repo, patterns) in selected.items():
        meta = json.loads((ROOT/'research'/f"{repo.replace('/', '--')}.json").read_text())
        print('Downloading', name, meta['sha'], flush=True)
        snapshot_download(repo, revision=meta['sha'], allow_patterns=patterns, local_dir=ROOT/'models'/name, max_workers=4)
    if args.only in (None, 'whisper-cpp'):
        meta = json.loads((ROOT/'research/ggerganov--whisper.cpp.json').read_text())
        hf_hub_download('ggerganov/whisper.cpp', 'ggml-small.bin', revision=meta['sha'], local_dir=ROOT/'models'/'whisper-cpp')
