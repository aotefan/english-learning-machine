"""Compare existing Silero pause profiles with real local models on isolated ports."""
import argparse
import asyncio
import json
from pathlib import Path
import wave
from types import SimpleNamespace

from mac_service import MacService, ROOT
from protocol_benchmark import connect, one, read_input
from vad_benchmark import one_auto
from soak_backend import wait_for_event, read_events


async def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / 'input'
    inputs.mkdir()
    pcm, source = read_input(ROOT / 'artifacts/synthetic', '01')
    cut = int(16000 * .72) * 2
    for name, audio in [('normal', pcm), ('pause', pcm[:cut] + bytes(33600) + pcm[cut:])]:
        with wave.open(str(inputs / f'{name}.wav'), 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(audio)
    (output / 'fixture.json').write_text(json.dumps({
        'hardware': False, 'source': source, 'insert_at_ms':720, 'inserted_silence_ms':1050,
        'note':'Artificial pause inside a synthetic adult/system voice sentence; not child audio.'}, indent=2)+'\n')
    service = MacService(state_dir=output / 'launchd')
    events_path = output / 'events.jsonl'
    results = []
    try:
        service.start(host='127.0.0.1', port=args.port, debug_port=args.debug_port,
                      event_log=str(events_path), vad_profile='standard')
        with (output / 'results.jsonl').open('w') as stream:
            for profile in ('standard', 'slow'):
                if profile == 'slow':
                    service.set_pause_profile(profile)
                ws = await connect(f'ws://127.0.0.1:{args.port}/xiaozhi/v1/')
                try:
                    warmup = await one(ws, {'id':'normal'}, inputs, False, returned_dir=output/profile/'warmup')
                    (output/f'{profile}-warmup.json').write_text(json.dumps(warmup, ensure_ascii=False, indent=2)+'\n')
                    if not warmup['success']:raise RuntimeError('Warmup failed')
                    for sample in ('normal', 'pause'):
                        result = await one_auto(ws, {'id':sample}, inputs, output/profile/'audio')
                        await wait_for_event(SimpleNamespace(events=events_path), 0,
                                             'request_complete', result['request_id'])
                        events = read_events(events_path)
                        related = {e['event']:e for e in events if e.get('request_id')==result.get('request_id')}
                        result.update(profile=profile,
                            silence_ms=related.get('vad_speech_stopped', {}).get('silence_ms'),
                            captured_frames=related.get('audio_received', {}).get('frames'),
                            server_timings=related.get('request_complete'))
                        results.append(result)
                        stream.write(json.dumps(result, ensure_ascii=False)+'\n');stream.flush()
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        if not result['success']:raise RuntimeError('Automatic request failed')
                    if profile == 'slow':
                        manual = await one(ws, {'id':'normal'}, inputs, False, returned_dir=output/'manual-fallback')
                        (output/'manual-fallback.json').write_text(json.dumps(manual, ensure_ascii=False, indent=2)+'\n')
                        if not manual['success']:raise RuntimeError('Manual fallback failed')
                finally:
                    await ws.close()
    finally:
        service.stop()
    normal = {r['profile']:r for r in results if r['id']=='normal'}
    (output/'comparison.json').write_text(json.dumps({
        'hardware':False, 'auto_requests':len(results), 'manual_fallback_success':manual['success'],
        'normal_sentence_endpoint_extra_ms':normal['slow']['auto_stop_after_start_ms']-normal['standard']['auto_stop_after_start_ms'],
        'limitations':'One synthetic sentence per condition. No hardware accuracy or child/noise generalization.'}, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New directory only')
    parser.add_argument('--port', type=int, default=18018)
    parser.add_argument('--debug-port', type=int, default=18019)
    asyncio.run(run(parser.parse_args()))
