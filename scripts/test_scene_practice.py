"""Synthetic scene-practice smoke test; LOCAL_CHECK is the network-free default.

Historical cloud-20261009-42/43 contract. Current unified practice mode uses
scripts/test_practice_mode.py; do not use these old scene openings to accept it.

Prepare local macOS speech fixtures (no paid API):
  python scripts/test_scene_practice.py --generate-fixtures
Check the fixtures without reading credentials or connecting:
  python scripts/test_scene_practice.py
Run an explicitly authorized batch through the existing LIVE cloud service:
  .venv-cloud/bin/python scripts/test_scene_practice.py --live \
      --output artifacts/scene-practice-20261009/run-RELEASE
Run only the two-turn tidying case after a targeted fix:
  .venv-cloud/bin/python scripts/test_scene_practice.py --case tidying --live \
      --output artifacts/scene-practice-20261009/tidying-RELEASE

The live batch uses one cloud-smoke WebSocket, the service's existing budget,
software playback acknowledgements, at most 10 turns, and no retries. It is not
hardware or human listening acceptance. Only fixed fictional utterances are sent.
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / 'artifacts/scene-practice-20261009/input'
DEFAULT_URL = 'wss://english.example.com/xiaozhi/v1/'
DEFAULT_VOICE = 'Tingting (Chinese (China mainland))'
TURNS = (
    ('meal_start', '开始吃饭陪练。'),
    ('noodles', '我想吃面条。'),
    ('meaning', '什么意思？'),
    ('repeat', '再说一遍。'),
    ('outing_start', '开始出门陪练。'),
    ('book', '我想带一本书。'),
    ('tidy_start', '开始收拾陪练。'),
    ('blocks', '我想收好积木。'),
    ('express_mode', '回到生活表达模式。'),
    ('park', '我想去公园。'),
)
CASES = {
    'full': tuple(sample_id for sample_id, _ in TURNS),
    'tidying': ('tidy_start', 'blocks'),
}
SCENE_OPENINGS = {
    'meal_start': "Let's pretend it's mealtime. What would you like to eat?",
    'outing_start': "Let's pretend we're getting ready to go out. What do you need to take?",
    'tidy_start': "Let's pretend we're tidying up. What would you like to put away?",
}


def wav_metadata(path):
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('fixture_too_large')
    with wave.open(str(path), 'rb') as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(),
                wav.getcomptype()) != (16000, 1, 2, 'NONE'):
            raise ValueError('fixture_requires_16khz_mono_pcm16')
        frames = wav.getnframes()
        if not 1600 <= frames <= 240000 or len(wav.readframes(frames)) != frames * 2:
            raise ValueError('fixture_duration_or_data_invalid')
    return {'wav_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'input_duration_ms': frames / 16}


def generate_fixtures(folder, voice):
    """Use installed system speech and FFmpeg; never overwrite prior evidence."""
    if folder.exists():
        raise ValueError('fixture_directory_already_exists')
    if not shutil.which('say') or not shutil.which('ffmpeg'):
        raise ValueError('installed_macos_say_and_ffmpeg_required')
    folder.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.scene-fixtures-', dir=folder.parent) as tmp:
        stage = Path(tmp) / 'input'
        stage.mkdir(mode=0o700)
        rows = []
        for sample_id, text in TURNS:
            aiff = stage / (sample_id + '.aiff')
            wav = stage / (sample_id + '.wav')
            subprocess.run(['say', '-v', voice, '-r', '155', '-o', str(aiff), text],
                           check=True, capture_output=True, timeout=30)
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-i', str(aiff),
                            '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(wav)],
                           check=True, capture_output=True, timeout=30)
            rows.append({'id': sample_id, 'synthetic_text': text, **wav_metadata(wav)})
        (stage / 'manifest.json').write_text(json.dumps({
            'version': 1, 'source': 'macos_system_synthetic_speech', 'voice': voice,
            'family_recordings': False, 'turns': rows,
        }, ensure_ascii=False, indent=2) + '\n')
        stage.rename(folder)


def validate_fixtures(folder):
    manifest = json.loads((folder / 'manifest.json').read_text())
    if (not isinstance(manifest, dict) or manifest.get('version') != 1 or
            manifest.get('source') != 'macos_system_synthetic_speech' or
            manifest.get('family_recordings') is not False or
            not isinstance(manifest.get('turns'), list) or len(manifest['turns']) != len(TURNS)):
        raise ValueError('invalid_synthetic_manifest')
    rows = []
    for row, (sample_id, text) in zip(manifest['turns'], TURNS):
        if not isinstance(row, dict) or row.get('id') != sample_id or row.get('synthetic_text') != text:
            raise ValueError('manifest_must_match_fixed_fictional_turns')
        metadata = wav_metadata(folder / (sample_id + '.wav'))
        if any(row.get(key) != value for key, value in metadata.items()):
            raise ValueError('fixture_hash_or_duration_changed')
        rows.append({'id': sample_id, 'synthetic_text': text, **metadata})
    return rows


def select_case(rows, case):
    if case not in CASES:
        raise ValueError('invalid_scene_case')
    by_id = {row['id']: row for row in rows}
    return [by_id[sample_id] for sample_id in CASES[case]]


def response_checks(sample_id, result, previous_results):
    """Small observable checks, not a claim of full model-quality acceptance."""
    english = result.get('english', '')
    checks = {'nonempty_audio_and_text': bool(result.get('success') and english)}
    vocabulary = {
        'noodles': r'\bnoodles?\b',
        'book': r'\bbook\b',
        'blocks': r'\bblocks?\b',
        'park': r'\bpark\b',
    }
    if sample_id in SCENE_OPENINGS:
        checks['exact_scene_opening'] = english == SCENE_OPENINGS[sample_id]
    if sample_id in vocabulary:
        checks['expected_topic_words'] = bool(re.search(vocabulary[sample_id], english, re.I))
    if sample_id == 'meaning':
        original = previous_results.get('noodles', {}).get('english')
        checks['chinese_help_present'] = bool(re.search(r'[\u3400-\u9fff]', english))
        checks['returns_to_original_english'] = bool(original and english.endswith(original))
    elif sample_id == 'repeat':
        original = previous_results.get('noodles', {}).get('english')
        checks['repeats_exact_pre_help_english'] = bool(original and english == original)
    elif sample_id == 'express_mode':
        checks['expression_mode_announcement'] = english == 'Back to everyday English.'
    elif sample_id == 'park':
        checks['expression_not_chat_question'] = '?' not in english and '？' not in english
    return checks


async def acknowledge_software_playback(ws, request_id):
    """Confirm decoded output in this synthetic client, never pretend it was heard."""
    for event, source in (('playback_started', 'codec_output_returned'),
                          ('playback_completed', 'codec_queue_drained')):
        await ws.send(json.dumps({'type': 'mvp_playback', 'request_id': request_id,
                                  'event': event, 'source': source}))
    await ws.send(json.dumps({'type': 'ping'}))
    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    if not isinstance(reply, dict) or reply.get('type') != 'pong':
        raise ValueError('playback_ack_barrier_failed')


async def live_batch(url, credential_file, rows, folder, output, *, case='full'):
    # Lazy imports: LOCAL_CHECK never imports a network client or reads credentials.
    from cloud_replay import read_credential, validate_url
    from protocol_benchmark import connect, one

    validate_url(url)
    credential = read_credential(credential_file)
    if credential['device_id'] != 'cloud-smoke':
        raise ValueError('dedicated_cloud_smoke_credential_required')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    ws = None
    results = []
    prior = {}
    failure = None
    try:
        ws = await asyncio.wait_for(connect(url, device_id=credential['device_id'],
            token=credential['token'], features={'mvp_playback_completed': True}), timeout=30)
        with (output / 'results.jsonl').open('x') as stream:
            for index, row in enumerate(rows, 1):
                result = {'turn': index, 'id': row['id'], 'success': False,
                          'hardware': False, 'software_playback_acknowledged': False}
                try:
                    raw = await asyncio.wait_for(one(ws, {'id': row['id']}, folder, True,
                        source='synthetic_scene_practice', save_audio=False), timeout=120)
                    result = {key: raw[key] for key in (
                        'id', 'request_id', 'asr', 'english', 'success', 'total_ms',
                        'first_audio_after_stop_ms', 'output_frames') if key in raw}
                    result.update(turn=index, synthetic_text=row['synthetic_text'],
                                  input_sha256=row['wav_sha256'], hardware=False,
                                  software_playback_acknowledged=False)
                    if raw.get('error'):
                        result['error'] = 'server_rejected_or_provider_failed'
                    if result.get('success'):
                        await acknowledge_software_playback(ws, result['request_id'])
                        result['software_playback_acknowledged'] = True
                    result['checks'] = response_checks(row['id'], result, prior)
                    prior[row['id']] = result
                except Exception as exc:
                    # Preserve a known request ID if only the acknowledgement
                    # failed, so the existing billing record can be inspected.
                    result.update(success=False, error=type(exc).__name__)
                results.append(result)
                stream.write(json.dumps(result, ensure_ascii=False) + '\n')
                stream.flush()
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if not result.get('success'):
                    break  # Never reconnect or retry a paid turn.
                if index < len(rows):
                    await asyncio.sleep(3.2)
    except Exception as exc:
        failure = type(exc).__name__
    finally:
        if ws is not None:
            try:
                await ws.close()
            except Exception as exc:
                failure = failure or type(exc).__name__
    summary = {
        'mode': 'LIVE', 'case': case, 'planned': len(rows), 'attempted': len(results),
        'pipeline_successes': sum(bool(row.get('success')) for row in results),
        'automated_checks_passed': len(results) == len(rows) and all(
            row.get('success') and all(row.get('checks', {}).values()) for row in results),
        'semantic_quality_review': 'pending_human_review_of_synthetic_results',
        'hardware_acceptance': False, 'human_listening_verified': False,
        'input_source': 'fixed_fictional_macos_speech', 'audio_saved': False,
        'billing_source': 'existing_ECS_usage.sqlite_and_costs_page_by_request_id',
        'paid_retries': 0,
    }
    if failure:
        summary['error'] = failure
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--fixture-dir', type=Path, default=FIXTURE_DIR)
    parser.add_argument('--generate-fixtures', action='store_true', help='Generate all 10 fixed fixtures using installed macOS say and FFmpeg only')
    parser.add_argument('--voice', default=DEFAULT_VOICE)
    parser.add_argument('--case', choices=tuple(CASES), default='full', help='full: 10 turns (default); tidying: opening + blocks, 2 turns')
    parser.add_argument('--live', action='store_true', help='Explicitly send the selected fixed case (at most 10 turns) through the authorized cloud budget')
    parser.add_argument('--url', default=DEFAULT_URL)
    parser.add_argument('--credential-file', type=Path, default=ROOT / '.secrets/cloud-client.json')
    parser.add_argument('--output', type=Path, help='Required new result directory for --live')
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.generate_fixtures and args.live:
        parser.error('Generate and inspect fixtures locally before running --live')
    folder = args.fixture_dir.resolve()
    if args.generate_fixtures:
        generate_fixtures(folder, args.voice)
    all_rows = validate_fixtures(folder)
    rows = select_case(all_rows, args.case)
    if not args.live:
        print(json.dumps({'mode': 'LOCAL_CHECK', 'case': args.case,
                          'validated_samples': len(all_rows), 'selected_samples': len(rows),
                          'network_calls': 0, 'paid_calls': 0,
                          'credentials_read': False, 'input_source': 'fixed_fictional_macos_speech'}))
        return 0
    if args.output is None:
        parser.error('--live requires a new --output directory')
    output = args.output.resolve()
    if output == folder or folder in output.parents:
        parser.error('Results must be separate from the input fixtures')
    summary = asyncio.run(live_batch(args.url, args.credential_file.resolve(), rows, folder, output,
                                    case=args.case))
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary['automated_checks_passed'] and not summary.get('error') else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, wave.Error, EOFError, subprocess.SubprocessError) as exc:
        raise SystemExit('Scene check failed (' + type(exc).__name__ + '); no automatic retries.') from None
