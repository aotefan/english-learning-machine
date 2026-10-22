"""Run the upstream WebSocketServer, without upstream management HTTP endpoints."""
import asyncio
import os
import sys
import signal
import argparse
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
LAUNCH_DIR = Path.cwd()
SERVER = ROOT/'upstream/backend/main/xiaozhi-server'
sys.path.insert(0, str(SERVER))
os.environ['HF_HUB_OFFLINE']='1'
os.environ.setdefault('TOKENIZERS_PARALLELISM','false')

async def main(args):
    # Imports before asyncio.run: upstream logger loads configuration synchronously.
    from config.config_loader import load_config
    config = await load_config()
    if args.host is not None:
        config['server']['ip'] = args.host
    if args.port is not None:
        config['server']['port'] = args.port
    if args.event_log is not None:
        config['english_mvp']['log_path'] = str(args.event_log)
    from local_setup import apply_vad_pause
    apply_vad_pause(config, args.vad_silence_ms)
    selected_vad = config.get('selected_module', {}).get('VAD')
    if selected_vad:
        print(f"Automatic listening end pause: {config['VAD'][selected_vad].get('min_silence_duration_ms', 1000)} ms", flush=True)
    config['selected_module'] = {k:v for k,v in config['selected_module'].items() if v and k in ('ASR','LLM','TTS','VAD')}
    if args.capture_audio_dir is not None:
        from core.handle import englishMvp
        from core.utils.diagnostic_audio import DiagnosticAudioCapture
        englishMvp.diagnostic_capture = DiagnosticAudioCapture(
            args.capture_audio_dir, args.capture_audio_count, args.capture_audio_seconds)
        print(f'Local diagnostic WAV capture enabled: at most {args.capture_audio_count} requests '
              f'within {args.capture_audio_seconds}s; directory={args.capture_audio_dir}', flush=True)
    viewer = None
    if args.debug_port is not None:
        from debug_page import start_debug_page
        viewer = await start_debug_page(config['english_mvp']['log_path'], args.debug_port)
    try:
        server = WebSocketServer(config)
        task = asyncio.create_task(server.start())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if viewer is not None:
            await viewer.cleanup()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    parser.add_argument('--event-log', type=lambda p: (LAUNCH_DIR / Path(p).expanduser()).resolve())
    parser.add_argument('--debug-port', type=int, help='Optional read-only request page on 127.0.0.1, e.g. 8001')
    parser.add_argument('--vad-silence-ms', type=int, choices=(800, 1400),
                        help='Silero end pause: 800 standard / 1400 slow; manual mode unchanged')
    parser.add_argument('--capture-audio-dir', type=lambda p: (LAUNCH_DIR / Path(p).expanduser()).resolve(),
                        help='Opt-in local 16kHz WAV diagnostics; disabled by default')
    parser.add_argument('--capture-audio-count', type=int, default=4, choices=range(1, 21))
    parser.add_argument('--capture-audio-seconds', type=int, default=300, choices=range(1, 901))
    parser.add_argument('--check', action='store_true', help='Check local configuration, model files and audio libraries without listening or inference')
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    if args.debug_port is not None and not 1 <= args.debug_port <= 65535:
        parser.error('debug-port must be between 1 and 65535')
    try:
        if not (SERVER/'core/websocket_server.py').is_file():
            raise ValueError('Backend fork missing. Run python3 scripts/restore_forks.py first.')
        from local_setup import validate_local_config, check_runtime
        required_modules=validate_local_config(SERVER/'data/.config.yaml', args.port, args.vad_silence_ms)
        os.chdir(SERVER)
        checks=check_runtime(required_modules)
        if args.check:
            print('Local setup checks passed (model files checked; no model inference):', checks, flush=True)
            raise SystemExit(0)
        from core.websocket_server import WebSocketServer
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
    except (ValueError,OSError,ImportError,RuntimeError) as exc:
        print(f'Local MVP startup failed: {exc}',file=sys.stderr)
        raise SystemExit(1)
    finally:
        if 'core.providers.local_worker' in sys.modules:
            stopped=sys.modules['core.providers.local_worker'].worker.close()
            if stopped is not None:
                print(json.dumps({'event':'model_worker_stopped',**stopped}),flush=True)
