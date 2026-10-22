"""Small launchctl wrapper for the existing backend; no custom process supervisor."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import time
import urllib.request

import psutil
import yaml
from local_setup import VAD_PAUSES, validate_local_config

ROOT = Path(__file__).resolve().parents[1]


class MacService:
    def __init__(self, root=ROOT, state_dir=None):
        self.root = Path(root).resolve()
        self.state = Path(state_dir or self.root / 'artifacts/mac-service').resolve()
        suffix = hashlib.sha256(str(self.state).encode()).hexdigest()[:12]
        self.label = 'local.english-edge.' + suffix
        self.domain = f'gui/{os.getuid()}'
        self.target = f'{self.domain}/{self.label}'
        self.plist = self.state / 'backend.plist'

    def ctl(self, *args, check=True):
        result = subprocess.run(['/bin/launchctl', *args], capture_output=True,
                                text=True, timeout=40)
        if check and result.returncode:
            raise RuntimeError(f'launchctl {args[0]}失败：{result.stderr.strip()}')
        return result

    def loaded(self):
        return self.ctl('print', self.target, check=False).returncode == 0

    def processes(self):
        if not self.plist.exists():
            return []
        args = plistlib.loads(self.plist.read_bytes())['ProgramArguments']
        found = []
        for proc in psutil.process_iter(['pid', 'cmdline', 'uids']):
            try:
                if proc.info['uids'].real == os.getuid() and proc.info['cmdline'][1:] == args[1:]:
                    found.append(proc)
            except (psutil.Error, TypeError):
                continue
        return found

    def info(self):
        data = {'loaded': self.loaded(), 'pids': [], 'voice_ready': False, 'page_ready': False}
        if not self.plist.exists():
            return data
        plist = plistlib.loads(self.plist.read_bytes())
        args = plist['ProgramArguments']
        value = lambda key: args[args.index(key) + 1]
        data.update(host=value('--host'), port=int(value('--port')),
                    debug_port=int(value('--debug-port')), event_log=value('--event-log'),
                    output_log=plist['StandardOutPath'])
        data['vad_silence_ms'] = int(value('--vad-silence-ms')) if '--vad-silence-ms' in args else None
        procs = self.processes()
        data['pids'] = [p.pid for p in procs]
        for proc in procs:
            try:
                ports = {c.laddr.port for c in proc.net_connections(kind='inet') if c.status == 'LISTEN'}
                data['voice_ready'] |= data['port'] in ports
                data['page_ready'] |= data['debug_port'] in ports
            except psutil.Error:
                continue
        return data

    def prepare(self, host=None, port=None, debug_port=None, event_log=None, vad_profile=None):
        config_path = self.root / 'upstream/backend/main/xiaozhi-server/data/.config.yaml'
        config = yaml.safe_load(config_path.read_text())
        # Remember the last launch's ports/log across stop/start; never change providers.
        previous = plistlib.loads(self.plist.read_bytes()) if self.plist.exists() else None
        old = previous['ProgramArguments'] if previous else []
        def setting(key, explicit, default):
            return explicit if explicit is not None else old[old.index(key)+1] if key in old else default
        host = setting('--host', host, config['server']['ip'])
        port = int(setting('--port', port, config['server']['port']))
        debug_port = int(setting('--debug-port', debug_port, 8001))
        if vad_profile is not None and vad_profile not in VAD_PAUSES:
            raise ValueError('Unknown VAD pause profile.')
        pause = setting('--vad-silence-ms', VAD_PAUSES.get(vad_profile), None)
        if not all(1 <= p <= 65535 for p in (port, debug_port)) or port == debug_port:
            raise ValueError('语音与调试页端口必须不同，且在1–65535之间。')
        event_log = Path(setting('--event-log', event_log, config['english_mvp']['log_path'])).expanduser()
        if not event_log.is_absolute():
            event_log = self.root / event_log
        # Check by bind, not connect: this also catches an unavailable LAN address.
        for address, number in [(host, port), ('127.0.0.1', debug_port)]:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind((address, number))
                except OSError as exc:
                    raise RuntimeError(f'{address}:{number}不可用：端口已占用或本机IP已变化。不会停止其他进程。') from exc
        python = self.root / '.venv/bin/python'
        if not python.is_file():
            raise ValueError('缺少.venv，请先按README安装。')
        output = self.root / 'logs/mac-backend.log'
        output.parent.mkdir(exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        plist = {
            'Label': self.label,
            'ProgramArguments': [str(python), '-u', str(self.root / 'scripts/run_backend.py'),
                                 '--host', host, '--port', str(port), '--debug-port', str(debug_port),
                                 '--event-log', str(event_log.resolve())],
            'WorkingDirectory': str(self.root),
            'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
                                     'HF_HUB_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false'},
            'StandardOutPath': str(output), 'StandardErrorPath': str(output),
            'RunAtLoad': False, 'KeepAlive': False, 'ExitTimeOut': 30,
        }
        if pause is not None:
            plist['ProgramArguments'].extend(['--vad-silence-ms', str(pause)])
        self.plist.write_bytes(plistlib.dumps(plist))
        return plist

    def set_pause_profile(self, profile):
        # Validate before stopping a working backend. Do not regenerate config/models.
        if profile not in VAD_PAUSES:
            raise ValueError('Unknown VAD pause profile.')
        validate_local_config(self.root / 'upstream/backend/main/xiaozhi-server/data/.config.yaml',
                              vad_pause_override=VAD_PAUSES[profile])
        before = self.info()
        if before['voice_ready'] and before['page_ready'] and before.get('vad_silence_ms') == VAD_PAUSES[profile]:
            return before
        if not before['loaded']:
            raise ValueError('请先双击启动英语终端，再切换说话档位。')
        previous = self.plist.read_bytes()
        print('正在切换说话档位，后端将正常重启，设备会自动重连…', flush=True)
        self.stop()
        try:
            return self.start(vad_profile=profile)
        except Exception as exc:
            self.plist.write_bytes(previous)
            try:
                self.start()
            except Exception as rollback:
                raise RuntimeError(f'切换失败，原档位恢复也失败：{rollback}') from exc
            raise RuntimeError('切换失败，已恢复原启动参数和服务。') from exc

    def start(self, **options):
        if self.loaded():
            info = self.info()
            if any(value is not None for value in options.values()):
                raise ValueError('服务已加载；要更改启动参数，请先停止。')
            if info['voice_ready'] and info['page_ready']:
                return info  # Repeated clicks never replace a running backend.
            if info['pids']:
                raise RuntimeError('服务仍在启动或状态异常，请查看状态和运行日志。')
            self.ctl('bootout', self.target)
        self.prepare(**options)
        self.ctl('bootstrap', self.domain, str(self.plist))
        try:
            self.ctl('kickstart', self.target)
            # A fresh venv has measured native-import cold starts above 60s.
            deadline = time.monotonic() + 120
            next_notice = time.monotonic() + 10
            while time.monotonic() < deadline:
                info = self.info()
                if info['voice_ready'] and info['page_ready']:
                    # The page must actually answer, not merely have a listening socket.
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(f"http://127.0.0.1:{info['debug_port']}/api/requests", timeout=2) as response:
                        json.load(response)
                    return info
                if time.monotonic() >= next_notice:
                    print('仍在启动；首次加载本地依赖可能较慢…', flush=True)
                    next_notice = time.monotonic() + 10
                time.sleep(.4)
            raise RuntimeError('120秒内未就绪；请查看logs/mac-backend.log。')
        except BaseException:
            self.ctl('bootout', self.target, check=False)
            raise

    def stop(self):
        procs = self.processes() if self.loaded() else []
        if self.loaded():
            self.ctl('bootout', self.target)
        _, alive = psutil.wait_procs(procs, timeout=10)
        if alive:
            raise RuntimeError('后台尚未退出，请查看状态；没有按进程名强制杀进程。')
        return self.info()


def describe(info):
    if info['voice_ready'] and info['page_ready']:
        print(f"英语后端和调试页已运行，PID：{info['pids']}。")
        print(f"调试页：http://127.0.0.1:{info['debug_port']}/")
        print('模型在首句时加载，第一次可能较慢；设备能否连接请看实际语音测试。')
    elif info['loaded']:
        print('服务已加载，但尚未就绪或已退出。请查看运行日志。')
    else:
        print('一键启动的服务已停止。')
    if 'output_log' in info:
        pause = info.get('vad_silence_ms')
        print('自动断句等待：', f'{pause / 1000:g}秒' if pause is not None else '使用原配置')
        print('运行日志：', info['output_log'])
        print('请求日志：', info['event_log'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'stop', 'status', 'standard', 'slow'])
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    parser.add_argument('--debug-port', type=int)
    parser.add_argument('--event-log')
    parser.add_argument('--open', action='store_true', help='Open the local debug page after start')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('此启动器只用于macOS。')
    service = MacService()
    service.state.mkdir(parents=True, exist_ok=True)
    try:
        with (service.state / 'operation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.action == 'start':
                print('正在检查并启动，请稍候…', flush=True)
                info = service.start(host=args.host, port=args.port, debug_port=args.debug_port, event_log=args.event_log)
            elif args.action == 'stop':
                info = service.stop()
            elif args.action in VAD_PAUSES:
                info = service.set_pause_profile(args.action)
            else:
                info = service.info()
            describe(info)
            if args.open and info['page_ready']:
                subprocess.run(['/usr/bin/open', f"http://127.0.0.1:{info['debug_port']}/"], check=True)
    except BlockingIOError:
        print('另一个启动/停止操作正在进行，请稍后查看状态。', file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'操作未完成：{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
