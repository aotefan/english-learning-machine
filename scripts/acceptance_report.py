"""Require 20 distinct physical attempts, correlated logs, and all recovery checks."""
import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_KEYS = {
    'server_restart_device_reconnect', 'wifi_loss_device_reconnect',
    'provider_failure_no_manual_restart', 'server_no_crash', 'button_mode_retained',
}
TIMINGS = ('audio_upload_ms', 'asr_ms', 'english_ms', 'tts_ms', 'return_ms', 'total_ms')
EVENTS = {'recording_started', 'audio_received', 'asr_result', 'english_result',
          'tts_generated', 'audio_sent', 'request_complete'}


def finite_nonnegative(value):
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and float(value) >= 0
    except (TypeError, ValueError):
        return False


def evaluate(rows, recovery, expected, server_events, playback_events):
    errors = []
    ids = [r.get('id') for r in rows]
    if len(rows) != 20 or len(set(ids)) != 20 or set(ids) != set(expected):
        errors.append('Require exactly the 20 distinct sentence IDs in benchmark/sentences.json')
    if any(r.get('zh') != expected.get(r.get('id')) for r in rows):
        errors.append('Sentence text must match its benchmark ID')
    if any(r.get('status') not in ('pass', 'fail', 'unverified', 'not_run') for r in rows):
        errors.append('Invalid attempt status')
    attempted = [r for r in rows if r.get('status') in ('pass', 'fail', 'unverified')]
    unverified = sum(r.get('status') == 'unverified' for r in attempted)
    request_ids = [(r.get('request_id') or '').strip() for r in attempted]
    if any(not re.fullmatch(r'[0-9a-f]{32}', rid) for rid in request_ids):
        errors.append('Every attempted sentence needs its server request_id')
    if len(set(request_ids)) != len(request_ids):
        errors.append('Each attempt must use a distinct request_id')
    passed = 0
    for row in attempted:
        if row['status'] != 'pass':
            continue
        rid = row.get('request_id')
        records = [r for r in server_events if r.get('request_id') == rid]
        terminal = [r for r in records if r.get('event') == 'request_complete']
        playback = [r for r in playback_events.get(row.get('playback_log'), [])
                    if r.get('request_id') == rid and r.get('event') == 'playback_started'
                    and r.get('source') == 'codec_output_returned']
        text_matches = all(any(r.get('event') == event and r.get('text') == row.get(field)
                               for r in records) for event, field in
                           [('asr_result', 'asr'), ('english_result', 'english')])
        complete = len(terminal) == 1 and terminal[0].get('success') is True
        valid_times = complete and all(finite_nonnegative(terminal[0].get(k)) for k in TIMINGS)
        csv_times_match = valid_times and all(
            finite_nonnegative(row.get(k)) and abs(float(row[k])-float(terminal[0][k])) <= 1
            for k in ('asr_ms', 'english_ms', 'tts_ms', 'total_ms'))
        valid = (row.get('speaker_heard') == 'yes' and row.get('server_no_crash') == 'yes'
                 and bool(playback) and text_matches and csv_times_match
                 and EVENTS.issubset({r.get('event') for r in records})
                 and not any(r.get('event') == 'error' for r in records))
        if valid:
            passed += 1
        else:
            errors.append(f"Sentence {row.get('id')}: pass lacks matching text, timings, playback or complete server evidence")
    recovery_ok = isinstance(recovery, dict) and set(recovery) == RECOVERY_KEYS and all(
        recovery.get(key) is True for key in RECOVERY_KEYS)
    sentence_ok = not errors and len(attempted) == 20 and not unverified and passed >= 18
    return {'attempted': len(attempted), 'passed': passed,
            'failed': len(attempted)-passed-unverified, 'unverified': unverified,
            'rate': passed/len(attempted) if attempted else None,
            'sentence_gate_passed': sentence_ok, 'recovery': recovery,
            'recovery_gate_passed': recovery_ok, 'errors': errors,
            'mvp_passed': sentence_ok and recovery_ok,
            'note': 'speaker_heard and recovery are human observations; logs alone do not prove acoustic playback. Phase 2 remains gated.'}


def read_events(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f'Non-object log record in {path}')
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, default=ROOT/'benchmark/hardware_acceptance.csv')
    parser.add_argument('--recovery', type=Path, default=ROOT/'benchmark/hardware_recovery.json')
    parser.add_argument('--server-log', type=Path, default=ROOT/'logs/requests.jsonl')
    args = parser.parse_args()
    try:
        with args.results.open() as stream:
            rows = list(csv.DictReader(stream))
        recovery = json.loads(args.recovery.read_text())
        expected = {r['id']: r['zh'] for r in json.loads((ROOT/'benchmark/sentences.json').read_text())}
        paths = {r['playback_log'] for r in rows if r.get('status') == 'pass' and r.get('playback_log')}
        playback = {p: read_events(ROOT/p) for p in paths}
        server = read_events(args.server_log) if any(r.get('status') == 'pass' for r in rows) else []
        report = evaluate(rows, recovery, expected, server, playback)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report = {'mvp_passed': False, 'errors': [f'{type(exc).__name__}: {exc}']}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['mvp_passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
