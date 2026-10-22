"""No paid calls: health, rejected auth, accepted hello, reconnect, empty audio."""
import argparse
import asyncio
import json
import urllib.request
from pathlib import Path
import websockets


async def smoke(url, credential):
    rejected = 0
    for suffix, headers in (
            ('', {}), ('', {'Device-Id':credential['device_id'],'Authorization':'Bearer invalid'}),
            ('?authorization=invalid', {'Device-Id':credential['device_id']})):
        try:
            async with websockets.connect(url+suffix, additional_headers=headers):
                raise AssertionError('Authentication unexpectedly accepted')
        except websockets.exceptions.InvalidStatus as exc:
            assert exc.response.status_code == 401
            rejected += 1
    for _ in range(2):
        async with websockets.connect(url,additional_headers={
                'Device-Id':credential['device_id'],
                'Authorization':'Bearer '+credential['token']}) as ws:
            await ws.send(json.dumps({'type':'hello','version':1,'audio_params':{
                'format':'opus','sample_rate':16000,'channels':1,'frame_duration':60}}))
            hello=json.loads(await asyncio.wait_for(ws.recv(),10))
            assert hello['type']=='hello' and hello['audio_params']['sample_rate']==24000
            await ws.send('{"type":"ping"}')
            assert json.loads(await ws.recv())['type']=='pong'
            await ws.send('{"type":"listen","state":"start","mode":"manual"}')
            await ws.send('{"type":"listen","state":"stop"}')
            messages=[]
            for _ in range(4):
                msg=json.loads(await asyncio.wait_for(ws.recv(),5));messages.append(msg)
                if msg.get('type')=='tts' and msg.get('state')=='stop':break
            assert any(m.get('type')=='error' and 'empty_audio' in m.get('error','') for m in messages)
        await asyncio.sleep(.1)
    return {'unauthorized_rejected':rejected,'hello_ping_empty_audio_reconnect':2,
            'paid_api_calls':0,'hardware_acceptance':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',default='ws://127.0.0.1:18000/xiaozhi/v1/')
    p.add_argument('--credential-file',required=True,type=Path)
    a=p.parse_args()
    print(json.dumps(asyncio.run(smoke(a.url,json.loads(a.credential_file.read_text())))))
