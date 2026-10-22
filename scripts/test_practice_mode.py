"""Four fictional turns for the single practice mode; default is LOCAL_CHECK.

Generate installed macOS speech locally:
  python scripts/test_practice_mode.py --generate-fixtures
Check only, without network or credentials:
  python scripts/test_practice_mode.py
Explicitly run four paid, budget-gated turns through the existing cloud service:
  .venv-cloud/bin/python scripts/test_practice_mode.py --live \
      --output artifacts/practice-mode-20261009/run-RELEASE

No retry or hardware acceptance. Existing historical scene evidence is unchanged.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import wave

from test_scene_practice import wav_metadata, acknowledge_software_playback


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / 'artifacts/practice-mode-20261009/input'
DEFAULT_URL = 'wss://english.example.com/xiaozhi/v1/'
DEFAULT_VOICE = 'Tingting (Chinese (China mainland))'
TURNS = (
    ('practice_start', '陪练模式。'),
    ('noodles', '我想吃面条。'),
    ('book', '我想看一本书。'),
    ('practice_end', '结束陪练。'),
)
ANNOUNCEMENTS = {
    'practice_start': "Let's chat in English.",
    'practice_end': 'Back to everyday English.',
}


def generate_fixtures(folder, voice):
    if folder.exists():
        raise ValueError('fixture_directory_already_exists')
    if not shutil.which('say') or not shutil.which('ffmpeg'):
        raise ValueError('installed_macos_say_and_ffmpeg_required')
    folder.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.practice-fixtures-', dir=folder.parent) as tmp:
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
    for row, (sample_id, text) in zip(manifest['turns'], TURNS):
        if not isinstance(row, dict) or row.get('id') != sample_id or row.get('synthetic_text') != text:
            raise ValueError('manifest_must_match_fixed_fictional_turns')
        metadata = wav_metadata(folder / (sample_id + '.wav'))
        if any(row.get(key) != value for key, value in metadata.items()):
            raise ValueError('fixture_hash_or_duration_changed')
    return manifest['turns']


def response_checks(sample_id, result):
    english = result.get('english', '')
    checks = {'nonempty_audio_and_text': bool(result.get('success') and english)}
    if sample_id in ANNOUNCEMENTS:
        checks['exact_mode_announcement'] = english == ANNOUNCEMENTS[sample_id]
    else:
        pattern = r'\bnoodles?\b' if sample_id == 'noodles' else r'\bbook\b'
        checks['expected_topic_words'] = bool(re.search(pattern, english, re.I))
    return checks


async def live_batch(url, credential_file, rows, folder, output):
    # Imports and credential reads occur only after explicit --live.
    from cloud_replay import read_credential, validate_url
    from protocol_benchmark import connect, one

    validate_url(url)
    credential = read_credential(credential_file)
    if credential['device_id'] != 'cloud-smoke':
        raise ValueError('dedicated_cloud_smoke_credential_required')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    results, ws, failure = [], None, None
    try:
        ws = await asyncio.wait_for(connect(url, device_id=credential['device_id'],
            token=credential['token'], features={'mvp_playback_completed': True}), timeout=30)
        with (output / 'results.jsonl').open('x') as stream:
            for index, row in enumerate(rows, 1):
                result = {'turn': index, 'id': row['id'], 'success': False,
                          'hardware': False, 'software_playback_acknowledged': False}
                try:
                    raw = await asyncio.wait_for(one(ws, {'id': row['id']}, folder, True,
                        source='synthetic_practice_mode', save_audio=False), timeout=120)
                    result.update({key: raw[key] for key in (
                        'request_id', 'asr', 'english', 'success', 'total_ms',
                        'first_audio_after_stop_ms', 'output_frames') if key in raw})
                    result.update(synthetic_text=row['synthetic_text'], input_sha256=row['wav_sha256'])
                    if raw.get('error'):
                        result['error'] = 'server_rejected_or_provider_failed'
                    if result.get('success'):
                        await acknowledge_software_playback(ws, result['request_id'])
                        result['software_playback_acknowledged'] = True
                    result['checks'] = response_checks(row['id'], result)
                except Exception as exc:
                    result.update(success=False, error=type(exc).__name__)
                results.append(result)
                stream.write(json.dumps(result, ensure_ascii=False) + '\n')
                stream.flush()
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if not result.get('success'):
                    break
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
        'mode': 'LIVE', 'planned': len(rows), 'attempted': len(results),
        'pipeline_successes': sum(bool(row.get('success')) for row in results),
        'automated_checks_passed': len(results) == len(rows) and all(
            row.get('success') and all(row.get('checks', {}).values()) for row in results),
        'semantic_quality_review': 'pending_human_review_including_natural_topic_change',
        'hardware_acceptance': False, 'human_listening_verified': False,
        'input_source': 'fixed_fictional_macos_speech', 'audio_saved': False,
        'billing_source': 'existing_ECS_usage.sqlite_and_costs_page_by_request_id', 'paid_retries': 0,
    }
    if failure:
        summary['error'] = failure
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--fixture-dir', type=Path, default=FIXTURE_DIR)
    parser.add_argument('--generate-fixtures', action='store_true', help='Generate four fixed utterances using installed macOS say and FFmpeg')
    parser.add_argument('--voice', default=DEFAULT_VOICE)
    parser.add_argument('--live', action='store_true', help='Send four synthetic turns through the existing cloud budget')
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
    rows = validate_fixtures(folder)
    if not args.live:
        print(json.dumps({'mode': 'LOCAL_CHECK', 'validated_samples': len(rows),
                          'network_calls': 0, 'paid_calls': 0, 'credentials_read': False}))
        return 0
    if args.output is None:
        parser.error('--live requires a new --output directory')
    output = args.output.resolve()
    if output == folder or folder in output.parents:
        parser.error('Results must be separate from input fixtures')
    summary = asyncio.run(live_batch(args.url, args.credential_file.resolve(), rows, folder, output))
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary['automated_checks_passed'] and not summary.get('error') else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, wave.Error, EOFError, subprocess.SubprocessError) as exc:
        raise SystemExit('Practice check failed (' + type(exc).__name__ + '); no automatic retries.') from None
