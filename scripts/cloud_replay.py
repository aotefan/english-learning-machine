"""Replay authorized WAVs through the existing authenticated cloud voice server.
Default validates local inputs only. No vendor keys, direct calls or separate budget.
"""
import argparse
import asyncio
import json
import os
import wave
from pathlib import Path
from urllib.parse import urlsplit

from protocol_benchmark import connect, one, read_input


def validate_url(url):
    parts=urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment or parts.path!='/xiaozhi/v1/':
        raise ValueError('Use the voice endpoint without URL credentials or query parameters')
    if parts.scheme!='wss' and not (parts.scheme=='ws' and parts.hostname in ('127.0.0.1','localhost','::1')):
        raise ValueError('Use WSS, or loopback WS through an SSH tunnel')
    if not parts.hostname:raise ValueError('Missing voice server host')


def read_manifest(path,folder,count):
    rows=json.loads(path.read_text())
    if not isinstance(rows,list) or not 1<=len(rows)<=30 or not 1<=count<=len(rows):
        raise ValueError('Manifest requires 1–30 samples and an explicit valid count')
    selected=rows[:count];ids=set()
    for row in selected:
        if not isinstance(row,dict):raise ValueError('Invalid sample')
        read_input(folder,row.get('id'))
        if row['id'] in ids:raise ValueError('Duplicate sample ID')
        ids.add(row['id'])
        if row.get('transcript') is not None and not isinstance(row['transcript'],str):
            raise ValueError('Transcript must be actual human-checked words or null')
    return selected


def read_credential(path):
    if path.stat().st_mode & 0o077:raise ValueError('Credential file must have mode 600')
    value=json.loads(path.read_text())
    if not isinstance(value,dict):raise ValueError('Invalid device credential')
    if not all(isinstance(value.get(k),str) and value[k] and len(value[k])<=256
               and '\n' not in value[k] and '\r' not in value[k] for k in ('device_id','token')):
        raise ValueError('Invalid device credential')
    return value


async def replay(url,credential,rows,folder,output,*,save_audio=False,realtime=True):
    """One attempt per sample. A failure stops the batch; no paid auto-retries."""
    output.mkdir(parents=True,exist_ok=False,mode=0o700)
    count=0;successes=0
    with (output/'results.jsonl').open('x') as stream:
        for row in rows:
            ws=None
            try:
                ws=await connect(url,device_id=credential['device_id'],token=credential['token'])
                result=await asyncio.wait_for(one(ws,{'id':row['id']},folder,realtime,
                    source='authorized_file_replay',returned_dir=output/'generated-audio',save_audio=save_audio),120)
                # Keep error type only; never persist arbitrary server exception bodies.
                if result.get('error'):result['error']='server_rejected_or_provider_failed; see cloud diagnostics by request_id'
            except Exception as exc:
                result={'id':row['id'],'source':'authorized_file_replay','hardware':False,
                        'success':False,'error':type(exc).__name__,'request_id':None}
            finally:
                if ws is not None:await ws.close()
            result.pop('input_path',None)
            result['reference_transcript']=row.get('transcript')
            result['quality_review']='pending'  # Returned audio alone is not recognition accuracy.
            stream.write(json.dumps(result,ensure_ascii=False)+'\n');stream.flush()
            count+=1;successes+=int(result['success'])
            if not result['success']:break
            await asyncio.sleep(3)  # Existing server-side cooldown remains authoritative.
    summary={'attempted':count,'pipeline_successes':successes,'planned':len(rows),
             'hardware_acceptance':False,'recognition_quality_verified':False,
             'billing_source':'ECS usage.sqlite; costs page, same request_id',
             'raw_audio_copied':False,'generated_audio_saved':save_audio}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--input-dir',type=Path,required=True)
    p.add_argument('--count',type=int,default=1)
    p.add_argument('--url',default='ws://127.0.0.1:18000/xiaozhi/v1/')
    p.add_argument('--credential-file',type=Path)
    p.add_argument('--output',type=Path)
    p.add_argument('--live',action='store_true',help='Send authorized audio; requires approved API budget and LIVE server')
    p.add_argument('--save-generated-audio',action='store_true')
    a=p.parse_args();os.umask(0o077)
    validate_url(a.url)
    rows=read_manifest(a.manifest.resolve(),a.input_dir.resolve(),a.count)
    if not a.live:
        print(json.dumps({'validated_samples':len(rows),'network_calls':0,'paid_calls':0,'mode':'LOCAL_CHECK'}))
        return 0
    if not a.credential_file or not a.output:raise ValueError('--credential-file and a new --output directory required')
    result=asyncio.run(replay(a.url,read_credential(a.credential_file),rows,a.input_dir.resolve(),
                             a.output.resolve(),save_audio=a.save_generated_audio))
    print(json.dumps(result,ensure_ascii=False))  # Transcripts only in private result files.
    return 0 if result['pipeline_successes']==result['planned'] else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,wave.Error,EOFError):raise SystemExit('Replay configuration/input invalid; no automatic retries.') from None
