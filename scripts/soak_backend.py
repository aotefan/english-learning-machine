"""Real local models over upstream WS/Opus; synthetic input, never hardware acceptance."""
import argparse
import asyncio
import contextlib
import datetime
import json
import os
import signal
import socket
import statistics
import sys
import time
from pathlib import Path

import psutil

from protocol_benchmark import connect, one

ROOT = Path(__file__).resolve().parents[1]


class OwnedBackend:
    def __init__(self, folder, port):
        self.folder, self.port = folder, port
        self.events = folder / 'request-events.jsonl'
        self.process = None
        self.log = None
        self.starts = 0
        self.shutdowns = []

    @property
    def url(self):
        return f'ws://127.0.0.1:{self.port}/xiaozhi/v1/'

    async def start(self):
        self.starts += 1
        self.log = (self.folder / f'server-{self.starts}.log').open('wb')
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / 'scripts/run_backend.py'), '--host', '127.0.0.1',
            '--port', str(self.port), '--event-log', str(self.events), cwd=ROOT,
            stdout=self.log, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic()+45
        while time.monotonic() < deadline:
            if self.process.returncode is not None:
                raise RuntimeError(f'Owned backend exited: {self.process.returncode}')
            try:
                ws = await connect(self.url)
                await ws.close()
                return
            except (OSError, TimeoutError):
                await asyncio.sleep(.2)
        raise TimeoutError('backend_start_timeout')

    async def stop(self):
        if self.process is None:
            return
        started = time.monotonic()
        forced = False
        descendants=[]
        with contextlib.suppress(psutil.NoSuchProcess):
            descendants=psutil.Process(self.process.pid).children(recursive=True)
        if self.process.returncode is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 20)
            except TimeoutError:
                forced = True
                os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        # Clean only this harness's process group, including a native child
        # potentially orphaned by an interrupted server. Never kill other servers.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        _, remaining=await asyncio.to_thread(psutil.wait_procs,descendants,timeout=2)
        self.log.close()
        log=(self.folder/f'server-{self.starts}.log').read_text(errors='replace')
        worker_stop=None
        for line in log.splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                row=json.loads(line)
                if isinstance(row,dict) and row.get('event')=='model_worker_stopped':
                    worker_stop=row
        self.shutdowns.append({'exit_code':self.process.returncode, 'forced':forced,
                               'model_worker':worker_stop,
                               'remaining_children':len(remaining),
                               'resource_cleanup_warning':'leaked semaphore' in log,
                               'shutdown_ms':(time.monotonic()-started)*1000})
        self.process = None

    def memory(self):
        process = psutil.Process(self.process.pid)
        children = process.children(recursive=True)
        return {'server_rss_mib':process.memory_info().rss/2**20,
                'children_rss_mib':sum(p.memory_info().rss for p in children if p.is_running())/2**20,
                'child_count':len(children), 'server_threads':process.num_threads()}


def read_events(path):
    if not path.exists():
        return []
    rows=[]
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # A writer may be appending its final line.
    return rows


async def wait_for_event(backend, offset, name, request_id=None):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        matches=[r for r in read_events(backend.events)[offset:] if r['event']==name
                 and (request_id is None or r.get('request_id')==request_id)]
        if matches:
            return matches[-1]
        await asyncio.sleep(.01)
    raise TimeoutError(f'no {name} event')


async def main(args):
    folder=ROOT/args.output
    folder.mkdir(parents=True, exist_ok=False)  # Do not overwrite evidence.
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', args.port))
        port=probe.getsockname()[1]
    backend=OwnedBackend(folder, port)
    sentences=json.loads((ROOT/'benchmark/sentences.json').read_text())
    report={'source':'synthetic_loopback_real_models', 'hardware':False,
            'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'requests':[], 'fault_checks':[], 'memory':[]}
    ws=None
    started=time.monotonic()
    try:
        await backend.start()
        ws=await connect(backend.url)
        for index in range(args.count):
            if index and index%20==0:
                offset=len(read_events(backend.events))
                await ws.send(json.dumps({'type':'listen','state':'start','mode':'manual'}))
                recording=await wait_for_event(backend, offset, 'recording_started')
                await ws.close()
                terminal=await wait_for_event(backend, offset, 'request_complete', recording['request_id'])
                report['fault_checks'].append({'case':'disconnect_during_capture',
                                               'request_id':recording['request_id'],
                                               'terminal_failure_logged':terminal['success'] is False})
                ws=await connect(backend.url)
            if index==args.count//2:
                offset=len(read_events(backend.events))
                in_flight=asyncio.create_task(one(ws, sentences[0], ROOT/'artifacts/synthetic', False))
                received=await wait_for_event(backend, offset, 'audio_received')
                await backend.stop()
                with contextlib.suppress(Exception):
                    await in_flight
                await ws.close()
                terminal=await wait_for_event(backend, offset, 'request_complete', received['request_id'])
                await backend.start()
                ws=await connect(backend.url)
                report['fault_checks'].append({'case':'server_restart_during_request',
                                               'request_id':received['request_id'],
                                               'terminal_failure_logged':terminal['success'] is False})
            row=sentences[index%len(sentences)]
            try:
                offset=len(read_events(backend.events))
                result=await one(ws, row, ROOT/'artifacts/synthetic', not args.burst)
                if result.get('request_id'):
                    await wait_for_event(backend, offset, 'request_complete', result['request_id'])
            except Exception as exc:
                result={'id':row['id'],'success':False,'error':f'{type(exc).__name__}: {exc}'}
                await ws.close()
                ws=await connect(backend.url)
            result['iteration']=index+1
            report['requests'].append(result)
            with (folder/'requests.jsonl').open('a') as out:
                out.write(json.dumps(result, ensure_ascii=False)+'\n')
            if index%10==0 or index==args.count-1:
                report['memory'].append({'iteration':index+1,'server_start':backend.starts,**backend.memory()})
            if backend.process.returncode is not None:
                raise RuntimeError('backend_crashed')
            print(json.dumps({'iteration':index+1,'success':result['success'],
                              'total_ms':result.get('total_ms')},ensure_ascii=False),flush=True)
        events=read_events(backend.events)
        terminals={r['request_id']:r for r in events if r['event']=='request_complete'}
        ids=[r.get('request_id') for r in report['requests']]
        report['unique_request_ids']=len(ids)==len(set(ids)) and all(ids)
        report['complete_stage_logs']=all(
            rid in terminals and terminals[rid]['success'] and
            all(terminals[rid].get(key) is not None for key in
                ('audio_upload_ms','asr_ms','english_ms','tts_ms','return_ms','total_ms')) for rid in ids)
        report['stage_means_ms']={key:statistics.mean(terminals[rid][key] for rid in ids
                                                       if rid in terminals and terminals[rid].get(key) is not None)
                                  for key in ('asr_ms','english_ms','tts_ms','total_ms')}
    except Exception as exc:
        report['harness_error']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if ws is not None:
            await ws.close()
        await backend.stop()
        report['shutdowns']=backend.shutdowns
        report['elapsed_s']=time.monotonic()-started
        report['success_count']=sum(r['success'] for r in report['requests'])
        report['failure_count']=len(report['requests'])-report['success_count']
        report['passed']=(len(report['requests'])==args.count and report['failure_count']==0 and
                          report.get('unique_request_ids',False) and report.get('complete_stage_logs',False) and
                          all(r['terminal_failure_logged'] for r in report['fault_checks']) and
                          all(r['exit_code']==0 and not r['forced'] for r in backend.shutdowns) and
                          'harness_error' not in report)
        (folder/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    if not report['passed']:
        raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count',type=int,default=100)
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--output',default='artifacts/soak-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    parser.add_argument('--burst',action='store_true')
    args=parser.parse_args()
    if args.count<2:
        parser.error('count must be at least 2 to exercise restart')
    asyncio.run(main(args))
