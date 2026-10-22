import json, subprocess, time, re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
rows=json.loads((ROOT/'benchmark/sentences.json').read_text())
# One official CLI process, repeated -f inputs: model loads once.
command=['whisper-cli','-m',str(ROOT/'models/whisper-cpp/ggml-small.bin'),'-l','zh','-t','4','-otxt','-oj']
for row in rows:command+=['-f',str(ROOT/'artifacts/synthetic'/f"{row['id']}.wav")]
start=time.perf_counter(); result=subprocess.run(command,capture_output=True,text=True,timeout=600)
(ROOT/'artifacts/whisper-cpp-stdout.log').write_text(result.stdout)
(ROOT/'artifacts/whisper-cpp-stderr.log').write_text(result.stderr)
summary={'command':command,'exit_code':result.returncode,'batch_wall_ms':(time.perf_counter()-start)*1000,'source':'synthetic_system_voice','note':'20 files in one CLI process; per-file wall timing unavailable from this batch invocation'}
records=[]
for row in rows:
 file=ROOT/'artifacts/synthetic'/f"{row['id']}.wav.txt"
 records.append({'id':row['id'],'success':file.exists(),'output':file.read_text().strip() if file.exists() else None})
summary['results']=records
(ROOT/'benchmark/whisper-cpp.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False,indent=2))
