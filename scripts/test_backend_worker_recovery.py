"""Kill only our test server's native model child; recover on the same WebSocket."""
import argparse
import asyncio
import json
import signal
import socket
import time
from pathlib import Path

import psutil

from protocol_benchmark import connect, one
from soak_backend import OwnedBackend, ROOT, read_events, wait_for_event


def inference_child(server_pid):
    candidates=[p for p in psutil.Process(server_pid).children()
                if any('multiprocessing.spawn' in arg for arg in p.cmdline())]
    if len(candidates)!=1:
        raise RuntimeError(f'Expected exactly one owned inference child, got {len(candidates)}')
    return candidates[0]


async def main(args):
    folder=(ROOT/args.output).resolve()
    folder.mkdir(parents=True,exist_ok=False)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    backend=OwnedBackend(folder,port)
    report={'source':'synthetic_loopback_real_models','hardware':False,'passed':False}
    ws=None
    try:
        await backend.start()
        ws=await connect(backend.url)
        report['warmup']=await one(ws,{'id':'01'},ROOT/'artifacts/synthetic',False)
        assert report['warmup']['success'],report['warmup']
        child=inference_child(backend.process.pid)
        report['terminated_pid']=child.pid
        offset=len(read_events(backend.events))
        request=asyncio.create_task(one(ws,{'id':'01'},ROOT/'artifacts/synthetic',False))
        await wait_for_event(backend,offset,'audio_received')
        # psutil verifies process identity, avoiding PID-reuse mistakes.
        child.send_signal(signal.SIGKILL)
        report['injected_failure']=await asyncio.wait_for(request,15)
        assert not report['injected_failure']['success'],report['injected_failure']
        assert 'error' in report['injected_failure'],report['injected_failure']
        assert backend.process.returncode is None,'Server exited with its model child'
        start=time.monotonic()
        report['recovery']=await one(ws,{'id':'01'},ROOT/'artifacts/synthetic',False)
        report['recovery_ms']=(time.monotonic()-start)*1000
        report['replacement_pid']=inference_child(backend.process.pid).pid
        report['same_websocket']=True
        report['server_restarts']=backend.starts-1
        report['passed']=(report['recovery']['success'] and
                          report['replacement_pid']!=report['terminated_pid'])
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
    parser.add_argument('--output',default='artifacts/native-child-recovery')
    asyncio.run(main(parser.parse_args()))
