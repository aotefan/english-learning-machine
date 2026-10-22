"""Run generated deployment shell against local fake Docker; no SSH or APIs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from deploy_ecs import mode_override, runtime_setup, set_mode_script, rollback_script


class RuntimeModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name) / 'english edge'
        self.bin = Path(self.temp.name) / 'bin'
        self.bin.mkdir()
        self.events = Path(self.temp.name) / 'events.jsonl'
        for name in ('old', 'new'):
            folder = self.base / 'releases' / name / 'deploy'
            folder.mkdir(parents=True)
            (folder / 'compose.yaml').write_text('services: {}\n')
            (folder / '.env').write_text('VOICE_IMAGE=local-test\n')
        (self.base / 'current').symlink_to(self.base / 'releases' / 'new')
        (self.base / 'previous').symlink_to(self.base / 'releases' / 'old')
        # macOS has no flock; runtime locking itself uses the host's Linux flock.
        (self.bin / 'flock').write_text('#!/bin/sh\nexit 0\n')
        (self.bin / 'flock').chmod(0o700)
        (self.bin / 'docker').write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sys
args = sys.argv[1:]
files = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == '-f']
override = pathlib.Path(files[-1])
config = json.loads(override.read_text().split('\\n', 1)[1])
command = config['services']['voice']['command']
mode = 'live' if '--live' in command else 'standby'
event = dict(mode=mode, args=args, release=str(pathlib.Path(files[0]).parents[1]))
with open(os.environ['MODE_TEST_EVENTS'], 'a') as stream:
    stream.write(json.dumps(event) + '\\n')
if 'up' in args and os.environ.get('MODE_TEST_FAIL') == mode:
    sys.exit(1)
''')
        (self.bin / 'docker').chmod(0o700)
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'MODE_TEST_EVENTS': str(self.events)}

    def tearDown(self):
        self.temp.cleanup()

    def run_shell(self, script, success=True, **env):
        result = subprocess.run(['sh', '-s'], input=script, text=True,
                                capture_output=True, env={**self.env, **env})
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def selected(self):
        return (self.base / 'runtime' / 'compose-mode.yaml').read_text()

    def calls(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_override_only_changes_startup_command(self):
        for mode in ('live', 'standby'):
            document = json.loads(mode_override(mode).split('\n', 1)[1])
            self.assertEqual(set(document['services']['voice']), {'command'})
            self.assertEqual('--live' in document['services']['voice']['command'], mode == 'live')
        with self.assertRaises(ValueError):
            mode_override('invalid; echo unsafe')

    def test_new_runtime_defaults_standby(self):
        self.run_shell('set -eu\n' + runtime_setup(str(self.base)))
        self.assertEqual(self.selected(), mode_override('standby'))
        self.assertEqual((self.base / 'runtime' / 'compose-mode.yaml').stat().st_mode & 0o777, 0o600)

    def test_live_survives_next_release_setup_and_legacy_rollback(self):
        self.run_shell(set_mode_script('live', str(self.base)))
        self.run_shell('set -eu\n' + runtime_setup(str(self.base)))
        self.assertEqual(self.selected(), mode_override('live'))
        self.run_shell(rollback_script(str(self.base)))
        self.assertEqual((self.base / 'current').resolve().name, 'old')
        self.assertEqual((self.base / 'previous').resolve().name, 'new')
        self.assertTrue(all(call['mode'] == 'live' for call in self.calls()))

    def test_explicit_standby_survives_later_setup(self):
        self.run_shell(set_mode_script('live', str(self.base)))
        self.run_shell(set_mode_script('standby', str(self.base)))
        self.run_shell('set -eu\n' + runtime_setup(str(self.base)))
        self.assertEqual(self.selected(), mode_override('standby'))

    def test_failed_live_restores_standby_and_does_not_save_choice(self):
        self.run_shell(set_mode_script('live', str(self.base)), success=False, MODE_TEST_FAIL='live')
        self.assertEqual(self.selected(), mode_override('standby'))
        up_calls = [call for call in self.calls() if 'up' in call['args']]
        self.assertEqual([call['mode'] for call in up_calls], ['live', 'standby'])
        self.assertFalse((self.base / 'runtime' / 'compose-mode.candidate.yaml').exists())

    def test_failed_rollback_keeps_release_links_and_selected_mode(self):
        self.run_shell(set_mode_script('live', str(self.base)))
        self.run_shell(rollback_script(str(self.base)), success=False, MODE_TEST_FAIL='live')
        self.assertEqual((self.base / 'current').resolve().name, 'new')
        self.assertEqual(self.selected(), mode_override('live'))
        self.assertEqual(Path(self.calls()[-1]['release']).name, 'new')


if __name__ == '__main__':
    unittest.main()
