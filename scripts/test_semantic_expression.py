"""Fixed fictional semantic smoke cases; no provider calls without --live.

Generate local macOS speech: python scripts/test_semantic_expression.py --generate
Inspect hashes only: python scripts/test_semantic_expression.py
Run once through the existing cloud budget: python scripts/test_semantic_expression.py --live RELEASE
Replies require a separate semantic review; pipeline success is not translation accuracy.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from test_scene_practice import wav_metadata, acknowledge_software_playback
from cloud_replay import read_credential, validate_url
from protocol_benchmark import connect, one

BASE = ROOT / 'artifacts/semantics-20261010'
INPUT = BASE / 'input'
URL = 'wss://english.example.com/xiaozhi/v1/'
SAMPLES = (
    ('demonstrate', '你先折一只纸船给我看，我再试着折。'),
    ('wish', '我还想再玩一会儿。'),
    ('correction', '我们明天下午三点，不对，两点半出发。'),
    ('mixed', 'Can I 再喝一点果汁？'),
    ('short', '踢球呀？'),
    ('complete_question', '红色的纸和蓝色的纸是一样厚的吗？'),
    ('incomplete_question', '红色的纸和蓝色的纸是不是'),
    ('clarification_answer', '我想比较这两张纸的厚度，不是比较颜色，也不是问纸张大小，它们一样厚吗？'),
)

def generate():
    INPUT.mkdir(parents=True, exist_ok=False, mode=0o700)
    rows = []
    for sample_id, text in SAMPLES:
        aiff, wav = INPUT / (sample_id + '.aiff'), INPUT / (sample_id + '.wav')
        subprocess.run(['say', '-v', 'Tingting (Chinese (China mainland))', '-r', '155',
                        '-o', str(aiff), text], check=True, capture_output=True, timeout=30)
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-i', str(aiff), '-ar', '16000',
                        '-ac', '1', '-c:a', 'pcm_s16le', str(wav)], check=True, capture_output=True, timeout=30)
        rows.append({'id': sample_id, 'synthetic_text': text, **wav_metadata(wav)})
    (INPUT / 'manifest.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')

def validate():
    rows = json.loads((INPUT / 'manifest.json').read_text())
    assert [(r['id'], r['synthetic_text']) for r in rows] == list(SAMPLES)
    for row in rows:
        assert all(row.get(k) == v for k, v in wav_metadata(INPUT / (row['id'] + '.wav')).items())
    return rows

async def run(rows, label):
    if not label or not all(c.isalnum() or c in '-_' for c in label):
        raise ValueError('invalid_result_label')
    output = BASE / label
    output.mkdir(exist_ok=False, mode=0o700)
    validate_url(URL)
    credential = read_credential(ROOT / '.secrets/cloud-client.json')
    assert credential['device_id'] == 'cloud-smoke'
    ws = await asyncio.wait_for(connect(URL, device_id=credential['device_id'], token=credential['token'],
                                       features={'mvp_playback_completed': True}), 30)
    results = []
    try:
        with (output / 'results.jsonl').open('x') as stream:
            for index, row in enumerate(rows):
                if row['id'] == 'incomplete_question':
                    await ws.close()
                    ws = await asyncio.wait_for(connect(URL, device_id=credential['device_id'], token=credential['token'], features={'mvp_playback_completed': True}), 30)
                raw = await asyncio.wait_for(one(ws, {'id': row['id']}, INPUT, True,
                    source='synthetic_semantic_check', save_audio=False), 120)
                result = {k: raw[k] for k in ('request_id', 'asr', 'english', 'success',
                    'first_audio_after_stop_ms', 'total_ms', 'output_frames') if k in raw}
                result.update(id=row['id'], round=1,
                              synthetic_text=row['synthetic_text'], hardware=False)
                if raw.get('error'):
                    result['error'] = 'server_or_provider_failure'
                if result.get('success'):
                    await acknowledge_software_playback(ws, result['request_id'])
                results.append(result)
                stream.write(json.dumps(result, ensure_ascii=False) + '\n'); stream.flush()
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if not result.get('success'):
                    break
                if index < len(rows) - 1:
                    await asyncio.sleep(3.2)
    finally:
        await ws.close()
        summary = {'attempted': len(results), 'planned': len(rows),
            'successes': sum(bool(r.get('success')) for r in results), 'hardware_acceptance': False,
            'audio_saved': False, 'automatic_retries': 0,
            'metric': 'client_first_decoded_audio_after_listen_stop', 'semantic_review': 'pending'}
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary

if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--generate', action='store_true')
    parser.add_argument('--live', metavar='RELEASE', help='Run selected paid, budget-gated synthetic turns once')
    parser.add_argument('--case-id', action='append', choices=[item[0] for item in SAMPLES], help='Select a fixed case; repeat to test a subset in original order')
    args = parser.parse_args()
    if args.generate and args.live:
        parser.error('Generate and inspect fixtures before --live')
    if args.generate:
        generate()
    rows = validate()
    if args.case_id:
        rows = [row for row in rows if row['id'] in args.case_id]
    if args.live:
        result = asyncio.run(run(rows, args.live)); print(json.dumps(result))
        raise SystemExit(0 if result['successes'] == result['planned'] else 1)
    print(json.dumps({'mode': 'LOCAL_CHECK', 'samples': len(rows), 'paid_calls': 0}))
