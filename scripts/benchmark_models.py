"""Measured local candidate benchmarks on synthetic Chinese fixtures, not acceptance."""
import argparse, json, os, sys, time, wave, resource
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'upstream/backend/main/xiaozhi-server'))
from core.providers.local_models import LocalModels
parser=argparse.ArgumentParser();parser.add_argument('candidate',choices=['asr-mlx','english-marian','english-qwen','tts-kokoro']);args=parser.parse_args()
configs={'asr-mlx':('asr','mlx_whisper'),'english-marian':('english','marian'),'english-qwen':('english','mlx_qwen'),'tts-kokoro':('tts','kokoro')}
stage,engine=configs[args.candidate]
config={'engine':engine,'model_path':str(ROOT/'models'/args.candidate)}
models=LocalModels();rows=json.loads((ROOT/'benchmark/sentences.json').read_text())
output=ROOT/'benchmark'/f'{args.candidate}.jsonl'
with output.open('w') as out:
 for i,row in enumerate(rows):
  if stage=='asr':
   with wave.open(str(ROOT/'artifacts/synthetic'/f"{row['id']}.wav")) as wav:value=wav.readframes(wav.getnframes())
  else:value=row['zh'] if stage=='english' else row['reference']
  start=time.perf_counter(); record={'id':row['id'],'source':'synthetic_system_voice' if stage=='asr' else 'text_corpus','candidate':args.candidate,'cold':i==0,'input':row['zh']}
  try:
   result=models.run(stage,value,config)
   record.update(success=True,output=result if isinstance(result,str) else f'{len(result)} WAV bytes', **models.last_metrics)
   if stage=='tts':
    folder=ROOT/'artifacts/tts';folder.mkdir(exist_ok=True);(folder/f"{row['id']}.wav").write_bytes(result)
  except Exception as e:record.update(success=False,error=f'{type(e).__name__}: {e}')
  record['latency_ms']=(time.perf_counter()-start)*1000
  record['peak_rss_mb']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024/1024
  print(json.dumps(record,ensure_ascii=False),flush=True);out.write(json.dumps(record,ensure_ascii=False)+'\n');out.flush()
