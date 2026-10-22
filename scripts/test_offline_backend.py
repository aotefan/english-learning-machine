"""macOS-only: reuse the voice soak under OS network restrictions and fresh caches."""
import argparse
import asyncio
import errno
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
PROFILE=ROOT/'tests/loopback-only.sb'


def network_probe():
    denied=[]
    # Numeric documentation addresses avoid DNS and third-party HTTP requests.
    for family,address in [(socket.AF_INET,('192.0.2.1',9)),
                           (socket.AF_INET6,('2001:db8::1',9))]:
        for kind in (socket.SOCK_STREAM,socket.SOCK_DGRAM):
            with socket.socket(family,kind) as sock:
                sock.settimeout(2)
                try:
                    if kind==socket.SOCK_STREAM:sock.connect(address)
                    else:sock.sendto(b'x',address)
                except OSError as exc:
                    if exc.errno not in (errno.EPERM,errno.EACCES):raise
                    denied.append({'address':address[0],'transport':kind.name,'errno':exc.errno})
                else:raise AssertionError('Non-loopback traffic was allowed; not an offline test')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen()
        with socket.create_connection(listener.getsockname(),timeout=2):
            peer,_=listener.accept();peer.close()
    return {'non_loopback_denied':denied,'loopback_tcp_allowed':True}


def inside(args):
    folder=args.output
    report={'hardware':False,'passed':False,'scope':'OS network restrictions on test process descendants; loopback allowed. Not physical Wi-Fi loss or proof of zero attempted network calls.'}
    try:
        report['probe']=network_probe()
        child=subprocess.run([sys.executable,__file__,'--probe-only'],capture_output=True,text=True,check=True)
        report['exec_child_probe']=json.loads(child.stdout)
        report['initial_cache_files']=[str(p.relative_to(folder)) for p in (folder/'cache').rglob('*') if p.is_file()]
        assert not report['initial_cache_files'],'Offline run requires fresh cache directories'
        from soak_backend import main as run_soak
        asyncio.run(run_soak(SimpleNamespace(output=str(folder/'voice'),port=0,count=args.count,burst=False)))
        voice=json.loads((folder/'voice/summary.json').read_text())
        report.update(passed=voice['passed'],success=voice['success_count'],failure=voice['failure_count'],
                      fault_checks=voice['fault_checks'],shutdowns=voice['shutdowns'])
    finally:
        report['final_cache_files']=[str(p.relative_to(folder)) for p in (folder/'cache').rglob('*') if p.is_file()]
        (folder/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('artifacts/offline-backend'))
    parser.add_argument('--count',type=int,default=20)
    parser.add_argument('--inside',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--probe-only',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.probe_only:
        print(json.dumps(network_probe()));return
    if args.count<2:parser.error('count must be at least 2 to verify restart')
    args.output=(ROOT/args.output).resolve()
    if args.inside:
        inside(args);return
    sandbox=shutil.which('sandbox-exec')
    if platform.system()!='Darwin' or not sandbox:
        parser.error('This test requires macOS sandbox-exec; there is no unrestricted fallback')
    args.output.mkdir(parents=True,exist_ok=False)
    env={key:value for key,value in os.environ.items()
         if key.lower() not in ('http_proxy','https_proxy','all_proxy','no_proxy')
         and key not in ('TRANSFORMERS_CACHE','HUGGINGFACE_HUB_CACHE')}
    cache=args.output/'cache'
    for name,path in {'HF_HOME':cache/'hf','HF_HUB_CACHE':cache/'hf/hub',
                      'XDG_CACHE_HOME':cache/'xdg','TORCH_HOME':cache/'torch',
                      'TIKTOKEN_CACHE_DIR':cache/'tiktoken'}.items():
        path.mkdir(parents=True,exist_ok=True);env[name]=str(path)
    env['HF_HUB_OFFLINE']='1'
    env['NO_PROXY']='127.0.0.1,localhost,::1'
    command=[sandbox,'-f',str(PROFILE),sys.executable,str(Path(__file__).resolve()),
             '--inside','--output',str(args.output),'--count',str(args.count)]
    with (args.output/'driver.log').open('w') as out:
        result=subprocess.run(command,env=env,cwd=ROOT,stdout=out,stderr=subprocess.STDOUT)
    if result.returncode:raise SystemExit(result.returncode)
    print((args.output/'summary.json').read_text())


if __name__=='__main__':main()
