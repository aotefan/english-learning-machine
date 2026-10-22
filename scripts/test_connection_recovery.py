"""Real deadlines and local models over upstream WS/Opus, not physical Wi-Fi loss."""
import argparse
import asyncio
import json
import socket
import time

import websockets
from protocol_benchmark import OpusEncoderUtils, connect, one
from soak_backend import OwnedBackend, ROOT, read_events


async def main(args):
    folder = (ROOT/args.output).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    backend = OwnedBackend(folder, port)
    report = {'scope':'Software WS/Opus client, real local models and production deadlines; not ESP32 recovery acceptance',
              'hardware':False, 'passed':False, 'cases':[]}
    ws = None
    try:
        await backend.start()
        for reason, deadline in [('hello_timeout', 15), ('recording_submit_timeout', 20), ('heartbeat_timeout', 45)]:
            offset = len(read_events(backend.events))
            started = time.monotonic()
            if reason == 'hello_timeout':
                ws = await websockets.connect(backend.url, additional_headers={'Device-Id':'recovery-test'})
            else:
                ws = await connect(backend.url)
            if reason == 'recording_submit_timeout':
                await ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}))
                packets = []
                OpusEncoderUtils(16000,1,60).encode_pcm_to_opus_stream(bytes(1920),True,packets.append)
                await ws.send(packets[0])  # Never submit a stop; must not run partial ASR.
            await asyncio.wait_for(ws.wait_closed(), deadline+6)
            elapsed = time.monotonic()-started
            assert ws.close_reason == reason, ws.close_reason
            assert deadline-.1 <= elapsed <= deadline+6, elapsed
            # Let upstream connection finally finish its normal cleanup.
            await asyncio.sleep(.1)
            events = read_events(backend.events)[offset:]
            closures = [e for e in events if e['event']=='connection_timeout']
            assert len(closures)==1 and closures[0]['reason']==reason
            terminal = [e for e in events if e['event']=='request_complete']
            if reason == 'recording_submit_timeout':
                assert len(terminal)==1 and terminal[0]['success'] is False
                assert not any(e['event']=='asr_result' for e in events)
                assert [e['frames'] for e in events if e['event']=='audio_received']==[1]
            else:
                assert not terminal
            ws = await connect(backend.url)
            recovery = await one(ws, {'id':'01'}, ROOT/'artifacts/synthetic', False,
                                 returned_dir=folder/'returned')
            assert recovery['success'], recovery
            assert backend.process.returncode is None
            await ws.close(); ws = None
            case = {'fault':reason, 'closed_after_s':elapsed, 'timeout_event':closures[0],
                    'failed_request':terminal, 'next_request':recovery}
            report['cases'].append(case)
            print(json.dumps(case, ensure_ascii=False), flush=True)
        report.update(passed=True, server_restarts=backend.starts-1)
    finally:
        if ws is not None: await ws.close()
        await backend.stop()
        report['shutdowns'] = backend.shutdowns
        report['passed'] = report['passed'] and all(s['exit_code']==0 and not s['forced'] for s in backend.shutdowns)
        (folder/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    if not report['passed']: raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='artifacts/connection-recovery')
    asyncio.run(main(parser.parse_args()))
