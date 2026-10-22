"""Arm one bounded native-worker failure for a physical button test.

Reuses the existing owned-child selector. Never restarts the server or device.
An acoustic pass still requires the user's subsequent listening confirmation.
"""
import argparse
import json
import signal
import time
from pathlib import Path

import psutil

from test_backend_worker_recovery import inference_child

ROOT = Path(__file__).resolve().parents[1]


def main(args):
    owner = json.loads(args.owner.read_text())
    server = psutil.Process(owner['pid'])
    command = server.cmdline()
    if (str(ROOT / 'scripts/run_backend.py') not in command
            or abs(server.create_time() - owner['started_epoch']) > 2
            or '--event-log' not in command
            or Path(command[command.index('--event-log') + 1]).resolve() != args.events.resolve()):
        raise RuntimeError('Ownership/event-log mismatch; refusing fault injection')
    child = inference_child(server.pid)
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'armed', 'server_pid': server.pid,
              'worker_pid': child.pid, 'armed_epoch': time.time(),
              'wait_seconds': args.wait_seconds, 'injected': False,
              'pipeline_recovered': False, 'audible_confirmed': None,
              'device_reset_observed': None,
              'scope': 'Only next submitted request; no automatic physical acceptance.'}

    def save():
        (args.output / 'summary.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    seen = []
    failed_id = None
    failure_observed = False
    deadline = time.monotonic() + args.wait_seconds
    with args.events.open() as stream:
        stream.seek(0, 2)
        save()
        print(json.dumps(report), flush=True)
        try:
            while time.monotonic() < deadline:
                if not server.is_running() or server.status() == psutil.STATUS_ZOMBIE:
                    raise RuntimeError('Original server exited')
                position = stream.tell()
                line = stream.readline()
                if not line.endswith('\n'):
                    stream.seek(position)
                    time.sleep(0.01)
                    continue
                event = json.loads(line)
                seen.append(event)
                rid = event.get('request_id')
                if failed_id is None and event['event'] == 'audio_received':
                    # Allow the native ASR call to begin. If inference wins the
                    # race, the test remains inconclusive instead of retrying.
                    time.sleep(0.1)
                    current = inference_child(server.pid)
                    if current.pid != child.pid or current.create_time() != child.create_time():
                        raise RuntimeError('Worker changed before injection')
                    child.send_signal(signal.SIGKILL)
                    failed_id = rid
                    report.update(injected=True, injected_epoch=time.time(),
                                  failed_request_id=rid, status='awaiting_failed_request')
                    save()
                if rid == failed_id and event['event'] == 'request_complete':
                    failure_observed = event.get('success') is False
                    report['failure_observed'] = failure_observed
                    if not failure_observed:
                        report['status'] = 'inconclusive_failure_not_observed'
                        break
                    report['status'] = 'awaiting_next_request'
                    save()
                elif failure_observed and rid and event['event'] == 'request_complete':
                    replacement = inference_child(server.pid)
                    playback = any(e.get('request_id') == rid
                                   and e['event'] == 'playback_started'
                                   and e.get('source') == 'codec_output_returned' for e in seen)
                    recovered = (event.get('success') is True and playback
                                 and replacement.pid != child.pid)
                    report.update(recovery_request_id=rid,
                                  replacement_pid=replacement.pid,
                                  original_server_alive=server.is_running(),
                                  playback_ack=playback, pipeline_recovered=recovered,
                                  status='awaiting_human_confirmation' if recovered else 'recovery_failed')
                    break
            else:
                report['status'] = 'expired'
        except Exception as exc:
            report.update(status='test_error', error=f'{type(exc).__name__}: {exc}')
        finally:
            report['finished_epoch'] = time.time()
            (args.output / 'request-events.jsonl').write_text(''.join(
                json.dumps(e, ensure_ascii=False) + '\n' for e in seen))
            save()
            print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['pipeline_recovered'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', type=Path, required=True)
    parser.add_argument('--events', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--wait-seconds', type=int, default=600, choices=range(1, 601))
    raise SystemExit(main(parser.parse_args()))
