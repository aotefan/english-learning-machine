"""Cancel actual local TTS output after its first Opus frame; reuse the same WS."""
import argparse
import asyncio
import json
import socket
import time

from protocol_benchmark import connect, one
from soak_backend import OwnedBackend, ROOT, read_events, wait_for_event


async def main(args):
    folder=(ROOT/args.output).resolve()
    folder.mkdir(parents=True,exist_ok=False)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        port=probe.getsockname()[1]
    backend=OwnedBackend(folder,port)
    report={'source':'synthetic_loopback_real_models','hardware':False,
            'cycles':[],'passed':False}
    ws=None
    try:
        await backend.start()
        ws=await connect(backend.url)
        for _ in range(3):
            offset=len(read_events(backend.events))
            aborted_at=None
            async def abort(socket):
                nonlocal aborted_at
                aborted_at=time.monotonic()
                # Repeated control messages must not cancel the error response
                # a second time or poison the following request.
                for _ in range(2):
                    await socket.send(json.dumps({'type':'abort'}))
            interrupted=await asyncio.wait_for(
                one(ws,{'id':'01'},ROOT/'artifacts/synthetic',False,on_first_audio=abort),120)
            abort_ms=(time.monotonic()-aborted_at)*1000 if aborted_at is not None else None
            assert interrupted['output_frames']>0,interrupted
            assert not interrupted['success'] and 'request_aborted' in interrupted.get('error',''),interrupted
            assert abort_ms is not None and abort_ms<3000,abort_ms
            terminal=await wait_for_event(backend,offset,'request_complete',interrupted['request_id'])
            assert terminal['success'] is False,terminal
            recovered=await one(ws,{'id':'01'},ROOT/'artifacts/synthetic',False)
            assert recovered['success'],recovered
            await wait_for_event(backend,offset,'request_complete',recovered['request_id'])
            report['cycles'].append({'interrupted':interrupted,'abort_to_stop_ms':abort_ms,
                                     'recovery':recovered})
        report['same_websocket']=True
        report['server_restarts']=backend.starts-1
        report['passed']=len(report['cycles'])==3 and backend.starts==1
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
    parser.add_argument('--output',default='artifacts/output-recovery')
    asyncio.run(main(parser.parse_args()))
