import argparse,json,statistics,platform,subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser(description='Join software-client results to server timings by request_id; never hardware acceptance.')
p.add_argument('--input',type=Path,help='Custom protocol JSONL; omit to regenerate the original synthetic baseline summary')
p.add_argument('--event-log',type=Path,default=Path('logs/requests.jsonl'))
p.add_argument('--output',type=Path,help='New summary JSON path for custom input')
args=p.parse_args()
if bool(args.input)!=bool(args.output):p.error('--input and --output must be supplied together')
output=root/(args.output or Path('benchmark/summary.json'))
event_output=output.with_suffix('.events.jsonl') if args.input else root/'benchmark/request-events.jsonl'
if args.input and (output.exists() or event_output.exists()):p.error('Custom outputs must be new files')
rows=[json.loads(x) for x in (root/(args.input or Path('benchmark/protocol.jsonl'))).read_text().splitlines() if x.strip()]
ids={x['request_id'] for x in rows if x.get('request_id')}
events=[json.loads(x) for x in (root/args.event_log).read_text().splitlines() if x.strip()]
events=[x for x in events if x['request_id'] in ids]
terminals=[x for x in events if x['event']=='request_complete']
successful_ids={x['request_id'] for x in rows if x['success'] and x.get('request_id')}
complete=[x for x in terminals if x.get('success') and x['request_id'] in successful_ids]
mean=lambda values: statistics.mean(values) if values else None
summary={'environment':{'cpu':subprocess.check_output(['sysctl','-n','machdep.cpu.brand_string'],text=True).strip(),'ram_bytes':int(subprocess.check_output(['sysctl','-n','hw.memsize'])),'platform':platform.platform()},'hardware_acceptance':{'attempts':0,'success':None,'failure':None,'rate':None},'software_loopback':{'attempts':len(rows),'success':sum(x['success'] for x in rows),'failure':sum(not x['success'] for x in rows),'timing_definition':'successful server requests only; total includes client upload pacing and paced return, no physical speaker confirmation','means_ms':{k:mean([x[k] for x in complete if x.get(k) is not None]) for k in ['audio_upload_ms','asr_ms','english_ms','tts_ms','return_ms','total_ms']},'first_audio_after_stop_ms':mean([x['first_audio_after_stop_ms'] for x in rows if x['success'] and 'first_audio_after_stop_ms' in x])},'candidates':{}}
summary['software_loopback'].update(sources=sorted({x.get('source','unknown') for x in rows}),
    requests_without_server_id=sum(not x.get('request_id') for x in rows),
    missing_server_terminals=sorted(ids-{x['request_id'] for x in terminals}),timed_success_count=len(complete))
for name in ([] if args.input else ['asr-mlx','english-marian','english-qwen','tts-kokoro','tts-piper']):
 r=[json.loads(x) for x in (root/f'benchmark/{name}.jsonl').read_text().splitlines()];warm=[x for x in r if not x['cold'] and x['success']]
 summary['candidates'][name]={'invocations':len(r),'runtime_success':sum(x['success'] for x in r),'first_invocation_ms':r[0]['latency_ms'],'warm_mean_ms':statistics.mean(x['latency_ms'] for x in warm),'peak_rss_mb':max(x['peak_rss_mb'] for x in r),'note':'runtime_success is not a semantic quality judgment'}
 if 'first_chunk_ms' in r[0]:summary['candidates'][name]['warm_first_chunk_ms']=statistics.mean(x['first_chunk_ms'] for x in warm)
 if name=='tts-piper':summary['candidates'][name]['load_ms']=r[0]['load_ms']
output.parent.mkdir(parents=True,exist_ok=True)
with event_output.open('x' if args.input else 'w') as out:out.write(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in events))
with output.open('x' if args.input else 'w') as out:out.write(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(summary,ensure_ascii=False,indent=2))
