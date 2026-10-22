"""Run the existing voice server with cloud auth and persistent budget gates."""
import argparse
import asyncio
import os
import sqlite3
from contextlib import suppress
from pathlib import Path
from cloud_setup import load_cloud_config, initialize_config
from cloud_service import CloudPolicy, attach_policy


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env-file', type=Path, required=True)
    p.add_argument('--devices-file', type=Path, required=True)
    p.add_argument('--state-dir', type=Path, default=Path('/state'))
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=18000)
    p.add_argument('--live', action='store_true')
    p.add_argument('--admin-file',type=Path)
    p.add_argument('--capture-audio-dir', type=Path,
                   help='Opt-in diagnostic WAV capture in /state; disabled by default')
    p.add_argument('--capture-audio-count', type=int, default=1)
    p.add_argument('--capture-audio-seconds', type=int, default=900)
    args = p.parse_args()
    os.umask(0o077)
    state = args.state_dir.resolve()
    devices = args.devices_file.resolve()
    admin = args.admin_file.resolve() if args.admin_file else None
    if admin and admin.stat().st_mode & 0o077:
        raise ValueError('Admin credential permissions invalid')
    if args.capture_audio_dir is not None:
        capture_dir = args.capture_audio_dir.resolve()
        if not capture_dir.is_relative_to(state):
            raise ValueError('Diagnostic audio must stay under the state directory')
        if not 1 <= args.capture_audio_count <= 4 or not 1 <= args.capture_audio_seconds <= 900:
            raise ValueError('Cloud diagnostic capture exceeds one to four requests or 15 minutes')
    config = load_cloud_config(args.env_file.resolve(), require_credentials=args.live)
    if args.live and os.getenv('DOUBAO_LIVE_ENABLED') != '1':
        raise ValueError('Live calls disabled in private configuration')
    policy = CloudPolicy(state, devices, live=args.live)
    config['server'].update(ip=args.host, port=args.port, max_size=4096, max_queue=8)
    config['english_mvp'].update(cloud=True, log_path=str(state/'requests.jsonl'))
    config['log'].update(log_dir=str(state/'logs'), data_dir=str(state/'data'),
                         retention='3 days', diagnose=False)
    initialize_config(config)
    if args.capture_audio_dir is not None:
        from core.handle import englishMvp
        from core.utils.diagnostic_audio import DiagnosticAudioCapture
        englishMvp.diagnostic_capture = DiagnosticAudioCapture(
            capture_dir, args.capture_audio_count, args.capture_audio_seconds)
        print(f'Cloud diagnostic audio: max {args.capture_audio_count} request(s) '
              f'within {args.capture_audio_seconds}s', flush=True)
    from core.websocket_server import WebSocketServer
    server = attach_policy(WebSocketServer(config), policy)
    print('Cloud voice server: '+('LIVE' if args.live else 'STANDBY; paid calls disabled'), flush=True)
    async def retention_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.to_thread(policy.prune_expired)
            except (OSError, sqlite3.Error) as exc:
                print('Speech-text retention cleanup deferred: '+type(exc).__name__, flush=True)
    async def serve():
        viewer=None
        retention=asyncio.create_task(retention_loop())
        try:
            if admin:
                from debug_page import start_debug_page
                viewer=await start_debug_page(state/'requests.jsonl',18001,
                    billing_db=policy.db_path,admin_file=admin,host=args.host,runtime_status=policy.runtime_status,
                    update_budget=policy.set_budget, set_device_volume=policy.device_control.set_volume,
                    device_status=policy.device_control.status, set_device_mode=policy.device_control.set_mode)
            await server.start()
        finally:
            retention.cancel()
            with suppress(asyncio.CancelledError):
                await retention
            if viewer:await viewer.cleanup()
    try:
        asyncio.run(serve())
    finally:
        if hasattr(server._llm, 'close'):
            server._llm.close()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError):
        raise SystemExit('Cloud startup configuration invalid; check private configuration and registry.') from None
