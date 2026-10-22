"""Xiaozhi v1 client; real Opus over loopback, not ESP32 hardware acceptance."""
import argparse, asyncio, hashlib, io, json, re, sys, time, wave
from pathlib import Path
import opuslib_next
import websockets
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'upstream/backend/main/xiaozhi-server'))
from core.utils.opus_encoder_utils import OpusEncoderUtils

async def connect(url, *, device_id='software-benchmark', token=None, features=None):
    headers={'Device-Id':device_id,'Client-Id':'software-benchmark'}
    if token is not None:headers['Authorization']='Bearer '+token
    ws=await websockets.connect(url,additional_headers=headers,max_size=2**20)
    try:
        await ws.send(json.dumps({'type':'hello','version':1,'transport':'websocket',
                                  'features':features or {},
                                  'audio_params':{'format':'opus','sample_rate':16000,'channels':1,'frame_duration':60}}))
        hello=json.loads(await asyncio.wait_for(ws.recv(), timeout=15));assert hello['type']=='hello';assert hello['audio_params']['sample_rate']==24000
        return ws
    except BaseException:
        await ws.close()
        raise

def read_input(folder, sample_id):
    if not isinstance(sample_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',sample_id):
        raise ValueError('Invalid sample ID')
    path=folder/(sample_id+'.wav')
    # Bound the read even for an accidentally huge or malformed user recording.
    with path.open('rb') as stream:raw=stream.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('WAV exceeds 1 MiB; convert/trim to at most 15 seconds')
    with wave.open(io.BytesIO(raw)) as wav:
        if (wav.getframerate(),wav.getnchannels(),wav.getsampwidth())!=(16000,1,2):
            raise ValueError('Expected 16000 Hz, mono, 16-bit PCM WAV; convert with FFmpeg first')
        if not 1600<=wav.getnframes()<=240000:
            raise ValueError('WAV duration must be between 0.1 and 15 seconds')
        pcm=wav.readframes(wav.getnframes())
        if len(pcm)!=wav.getnframes()*2:raise ValueError('Truncated PCM data')
    return pcm,{'input_path':str(path.resolve()),'input_sha256':hashlib.sha256(raw).hexdigest(),
                'input_duration_ms':len(pcm)/32}

async def one(ws,row,folder,realtime=True,on_first_audio=None,*,source='synthetic_loopback',returned_dir=None,save_audio=True):
    pcm,metadata=read_input(folder,row['id'])
    packets=[];OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(pcm,True,packets.append)
    start=time.perf_counter()
    await ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}))
    for packet in packets:
        await ws.send(packet)
        if realtime:await asyncio.sleep(.06)
    stopped=time.perf_counter()
    await ws.send(json.dumps({'type':'listen','state':'stop'}))
    decoder=opuslib_next.Decoder(24000,1);audio=[]
    data={'id':row['id'],'source':source,'hardware':False,'success':False,**metadata}
    for name,field in (('zh','reference_zh'),('reference','reference_english')):
        if name in row:data[field]=row[name]
    rid=None
    while True:
        msg=await asyncio.wait_for(ws.recv(),timeout=180)
        if isinstance(msg,bytes):
            audio.append(decoder.decode(msg,1440))
            if len(audio)==1:
                data['first_audio_after_stop_ms']=(time.perf_counter()-stopped)*1000
                if on_first_audio is not None:await on_first_audio(ws)
        else:
            msg=json.loads(msg);rid=msg.get('request_id',rid)
            if msg['type']=='stt':data['asr']=msg['text']
            if msg['type']=='error':data['error']=msg['error']
            if msg['type']=='mvp_control':data.setdefault('controls',[]).append(msg)
            if msg['type']=='tts' and msg['state']=='sentence_start':data['english']=msg['text']
            if msg['type']=='tts' and msg['state']=='stop':
                if msg.get('no_audio') is True and not audio:
                    data['no_audio']=True
                    data.pop('english',None)  # Display notice is not generated speech.
                break
    data.update(request_id=rid,success=(bool(audio) or data.get('no_audio') is True) and 'error' not in data,total_ms=(time.perf_counter()-start)*1000,output_frames=len(audio))
    if audio and save_audio:
        dest=returned_dir if returned_dir is not None else ROOT/'artifacts/returned'
        dest.mkdir(parents=True,exist_ok=True)
        output=dest/(row['id']+'.wav')
        with wave.open(str(output),'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(b''.join(audio))
        data['returned_audio']=str(output.resolve())
    return data

async def main(args):
    rows=json.loads((ROOT/'benchmark/sentences.json').read_text())[:args.count]
    folder=(ROOT/args.input_dir).resolve() if args.input_dir else ROOT/'artifacts/synthetic'
    source=args.source or ('file_wav' if args.input_dir else 'synthetic_loopback')
    output=ROOT/args.output;output.parent.mkdir(parents=True,exist_ok=True)
    returned=ROOT/args.returned_dir if args.returned_dir else (output.parent/(output.stem+'-audio') if args.input_dir else ROOT/'artifacts/returned')
    if folder.resolve()==returned.resolve():raise ValueError('Returned audio directory must differ from input directory')
    if args.input_dir and returned.exists():raise FileExistsError(f'Use a new returned audio directory: {returned}')
    ws=None;failed=False
    try:
        # Preserve historical defaults; custom recordings must never overwrite evidence.
        with output.open('x' if args.input_dir else 'w') as out:
            for row in rows:
                try:
                    if ws is None:ws=await connect(args.url)
                    result=await one(ws,row,folder,not args.burst,source=source,returned_dir=returned)
                except Exception as exc:
                    result={'id':row['id'],'source':source,'hardware':False,'success':False,
                            'error':f'{type(exc).__name__}: {exc}'}
                    if ws is not None:await ws.close()
                    ws=None  # Do not reuse a socket that may contain an unfinished response.
                failed=failed or not result['success']
                print(json.dumps(result,ensure_ascii=False),flush=True);out.write(json.dumps(result,ensure_ascii=False)+'\n');out.flush()
    finally:
        if ws is not None:await ws.close()
    return 1 if failed else 0

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--url',default='ws://127.0.0.1:8000/xiaozhi/v1/');p.add_argument('--count',type=int,default=20);p.add_argument('--burst',action='store_true');p.add_argument('--output',default='benchmark/protocol.jsonl')
    p.add_argument('--input-dir',help='Directory containing 01.wav .. 20.wav matching benchmark/sentences.json')
    p.add_argument('--source',choices=['synthetic_loopback','file_wav','recorded_wav'],help='Caller-declared origin; custom directories default to file_wav, never hardware acceptance')
    p.add_argument('--returned-dir',help='Output WAV directory; custom inputs default to <output stem>-audio')
    args=p.parse_args()
    if not 1<=args.count<=20:p.error('count must be between 1 and 20')
    if args.input_dir and args.output=='benchmark/protocol.jsonl':p.error('Custom input requires --output pointing to a new results file')
    if args.source and not args.input_dir:p.error('--source requires an explicit --input-dir')
    raise SystemExit(asyncio.run(main(args)))
