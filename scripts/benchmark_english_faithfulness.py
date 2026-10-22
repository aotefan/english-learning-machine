"""Compare installed English providers on punctuation and meaning preservation."""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'upstream/backend/main/xiaozhi-server'))
os.environ['HF_HUB_OFFLINE'] = '1'
from core.providers.local_models import LocalModels

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
rows = json.loads((ROOT/'benchmark/english_faithfulness_cases.json').read_text())
models = LocalModels()
args.output.parent.mkdir(parents=True, exist_ok=True)
with args.output.open('x') as out:
    for engine, folder in [('marian', 'english-marian'), ('mlx_qwen', 'english-qwen')]:
        for row in rows:
            start = time.perf_counter()
            result = models.run('english', row['zh'], {'engine':engine, 'model_path':str(ROOT/'models'/folder)})
            record = {**row, 'engine':engine, 'output':result,
                      'latency_ms':(time.perf_counter()-start)*1000,
                      'contains_chinese':bool(re.search(r'[\u3400-\u9fff]', result)),
                      'scope':'Text model diagnostic; completion does not establish semantic correctness.'}
            line = json.dumps(record, ensure_ascii=False)
            out.write(line+'\n');out.flush();print(line, flush=True)
