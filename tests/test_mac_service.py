"""Service ownership and idempotence checks; real launchd exercise is separate."""
import plistlib
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from mac_service import MacService


class MacServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='English MVP space ')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        config = self.root / 'upstream/backend/main/xiaozhi-server/data/.config.yaml'
        config.parent.mkdir(parents=True)
        config.write_text(yaml.safe_dump({'server': {'ip':'127.0.0.1','port':18014},
                                         'english_mvp': {'log_path':'logs/events.jsonl'}}))
        python = self.root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.touch()
        self.service = MacService(self.root)

    def test_busy_port_is_not_taken_over(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0));occupied.listen()
            with self.assertRaisesRegex(RuntimeError, '不会停止其他进程'):
                self.service.prepare(port=occupied.getsockname()[1], debug_port=18015)
            with socket.create_connection(occupied.getsockname(), timeout=1):
                pass
        self.assertFalse(self.service.plist.exists())

    def test_plist_preserves_spaces_and_has_no_login_or_restart_loop(self):
        with patch('mac_service.socket.socket'):
            first = self.service.prepare(port=18014, debug_port=18015, event_log='logs/chosen.jsonl')
            second = self.service.prepare()
        self.assertEqual(first, second)
        self.assertEqual(first['ProgramArguments'][2], str(self.root / 'scripts/run_backend.py'))
        self.assertFalse(first['RunAtLoad'])
        self.assertFalse(first['KeepAlive'])
        self.assertNotIn('LaunchAgents', str(self.service.plist))
        self.assertEqual(plistlib.loads(self.service.plist.read_bytes()), first)
        self.assertNotEqual(self.service.label, MacService(self.root, self.root/'other').label)

    def test_duplicate_start_does_not_restart_ready_service(self):
        ready = dict(voice_ready=True, page_ready=True, pids=[123])
        with patch.object(self.service, 'loaded', return_value=True), \
             patch.object(self.service, 'info', return_value=ready), \
             patch.object(self.service, 'ctl') as ctl:
            self.assertEqual(self.service.start(), ready)
            ctl.assert_not_called()
            with self.assertRaisesRegex(ValueError, '先停止'):
                self.service.start(port=12345)

    def test_failed_kickstart_unloads_only_own_job(self):
        with patch.object(self.service, 'loaded', return_value=False), \
             patch.object(self.service, 'prepare'), \
             patch.object(self.service, 'ctl', side_effect=[None, RuntimeError('failed'), None]) as ctl:
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                self.service.start()
            self.assertEqual(ctl.call_args_list[-1].args, ('bootout', self.service.target))

    def test_stop_not_loaded_does_not_signal_processes(self):
        with patch.object(self.service, 'loaded', return_value=False), \
             patch.object(self.service, 'info', return_value={'loaded':False}), \
             patch.object(self.service, 'processes') as processes, \
             patch.object(self.service, 'ctl') as ctl:
            self.assertEqual(self.service.stop(), {'loaded':False})
            processes.assert_not_called()
            ctl.assert_not_called()

    def test_pause_profile_failure_restores_previous_launch_settings(self):
        self.service.state.mkdir(parents=True)
        self.service.plist.write_bytes(b'previous settings')
        def failed_start(**kwargs):
            if kwargs:
                self.service.plist.write_bytes(b'new settings')
                raise RuntimeError('injected startup failure')
            self.assertEqual(self.service.plist.read_bytes(), b'previous settings')
            return {'voice_ready':True}
        with patch('mac_service.validate_local_config'), \
             patch.object(self.service, 'info', return_value={'loaded':True,'voice_ready':False}), \
             patch.object(self.service, 'stop'), \
             patch.object(self.service, 'start', side_effect=failed_start) as start:
            with self.assertRaisesRegex(RuntimeError, '已恢复'):
                self.service.set_pause_profile('slow')
            self.assertEqual(start.call_count, 2)

    def test_same_pause_profile_does_not_interrupt_running_service(self):
        ready = dict(loaded=True, voice_ready=True, page_ready=True, vad_silence_ms=1400)
        with patch('mac_service.validate_local_config'), \
             patch.object(self.service, 'info', return_value=ready), \
             patch.object(self.service, 'stop') as stop:
            self.assertEqual(self.service.set_pause_profile('slow'), ready)
            stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
