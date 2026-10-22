"""Reject bad input; preserve recording across duplicate start and busy controls."""
import argparse
import asyncio
import json
import socket
import wave
import numpy as np

from protocol_benchmark import OpusEncoderUtils, connect, one, opuslib_next
from soak_backend import OwnedBackend, ROOT, read_events, wait_for_event


async def main(args):
    folder=(ROOT/args.output).resolve()
    folder.mkdir(parents=True,exist_ok=False)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        port=probe.getsockname()[1]
    backend=OwnedBackend(folder,port)
    with wave.open(str(ROOT/'artifacts/synthetic/01.wav')) as wav:
        pcm=wav.readframes(wav.getnframes())
    speech=[];silent=[];quiet=[];short=[]
    OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(pcm,True,speech.append)
    OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(bytes(16000*2*3),True,silent.append)
    OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(np.full(16000,20,np.int16).tobytes(),True,quiet.append)
    OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(np.full(960,1000,np.int16).tobytes(),True,short.append)
    cases=[('empty',[],'empty_audio'),
           ('undecodable',[b''],'audio_decode_failed'),
           ('oversized_packet',[bytes(1276)],'audio_too_long_or_packet_too_large'),
           ('too_many_frames',[speech[0]]*251,'audio_too_long_or_packet_too_large'),
           ('silent',silent,'empty_or_silent_audio'),
           ('quiet',quiet,'empty_or_silent_audio'),
           ('too_short',short,'empty_or_silent_audio')]
    report={'source':'synthetic_loopback_real_models','hardware':False,'cases':[],'passed':False}
    ws=None
    try:
        await backend.start()
        ws=await connect(backend.url)
        for name,packets,expected in cases:
            offset=len(read_events(backend.events))
            await ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}))
            for packet in packets:await ws.send(packet)
            for _ in range(2):
                await ws.send(json.dumps({'type':'listen','state':'stop'}))
            errors=[]
            while True:
                message=await asyncio.wait_for(ws.recv(),30)
                assert isinstance(message,str),'Invalid input must not produce audio'
                row=json.loads(message)
                if row['type']=='error':errors.append(row)
                if row.get('type')=='tts' and row.get('state')=='stop':break
            assert len(errors)==1 and expected in errors[0]['error'],errors
            await ws.send(json.dumps({'type':'ping'}))
            assert json.loads(await asyncio.wait_for(ws.recv(),3))=={'type':'pong'},'Stale response after stop'
            terminal=await wait_for_event(backend,offset,'request_complete',errors[0]['request_id'])
            assert terminal['success'] is False,terminal
            rejected=[e for e in read_events(backend.events)[offset:]
                      if e.get('request_id')==errors[0]['request_id'] and e['event']=='audio_rejected']
            if name in ('silent','quiet','too_short'):
                assert len(rejected)==1 and rejected[0]['model_called'] is False,rejected
                assert all(terminal[stage] is None for stage in ('asr_ms','english_ms','tts_ms')),terminal
            recovery=await one(ws,{'id':'01'},ROOT/'artifacts/synthetic',False)
            assert recovery['success'],recovery
            await wait_for_event(backend,offset,'request_complete',recovery['request_id'])
            report['cases'].append({'case':name,'rejection':errors[0],
                                    'audio_rejected':rejected,'terminal':terminal,'recovery':recovery})
        # Same socket and codecs: repeated start during capture must preserve it;
        # a new start during audio return is busy, without replacing the request.
        offset=len(read_events(backend.events))
        start=json.dumps({'type':'listen','state':'start','mode':'manual'})
        await ws.send(start)
        for index,packet in enumerate(speech):
            await ws.send(packet)
            if index in (0,10,len(speech)-1):await ws.send(start)
        for _ in range(2):await ws.send(json.dumps({'type':'listen','state':'stop'}))
        decoder=opuslib_next.Decoder(24000,1);frames=0;errors=[];texts={};request_id=None
        while True:
            message=await asyncio.wait_for(ws.recv(),90)
            if isinstance(message,bytes):
                assert len(decoder.decode(message,1440))==2880
                frames+=1
                if frames==1:await ws.send(start)
                continue
            row=json.loads(message);request_id=row.get('request_id',request_id)
            if row['type']=='error':errors.append(row)
            if row['type']=='stt':texts['asr']=row['text']
            if row.get('state')=='sentence_start':texts['english']=row['text']
            if row.get('type')=='tts' and row.get('state')=='stop':break
        assert frames>0 and len(errors)==1 and errors[0]['error']=='busy',errors
        terminal=await wait_for_event(backend,offset,'request_complete',request_id)
        assert terminal['success'] is True and errors[0]['request_id']==request_id,terminal
        events=read_events(backend.events)[offset:]
        assert len({event['request_id'] for event in events})==1,events
        assert sum(event['event']=='recording_started' for event in events)==1
        assert sum(event['event']=='control_ignored' for event in events)==3
        assert [event['frames'] for event in events if event['event']=='audio_received']==[len(speech)]
        await ws.send(json.dumps({'type':'ping'}))
        assert json.loads(await asyncio.wait_for(ws.recv(),3))=={'type':'pong'}
        following=await one(ws,{'id':'01'},ROOT/'artifacts/synthetic',False)
        assert following['success'],following
        await wait_for_event(backend,offset,'request_complete',following['request_id'])
        report['button_controls']={'duplicate_starts':3,'preserved_frames':len(speech),
                                   'output_frames':frames,'busy_response':errors[0],
                                   'texts':texts,'terminal':terminal,'following':following}
        report.update(passed=True,same_websocket=True,server_restarts=backend.starts-1)
    finally:
        if ws is not None:await ws.close()
        await backend.stop()
        report['shutdowns']=backend.shutdowns
        report['passed']=report['passed'] and all(s['exit_code']==0 and not s['forced'] for s in backend.shutdowns)
        (folder/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='artifacts/input-recovery')
    asyncio.run(main(parser.parse_args()))
