"""Offline guards for the optional paid scene smoke script; no provider calls."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import test_scene_practice as scene_check


class ScenePracticeScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        rows = []
        for sample_id, text in scene_check.TURNS:
            path = self.folder / (sample_id + '.wav')
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0' * 3200)
            rows.append({'id': sample_id, 'synthetic_text': text,
                         **scene_check.wav_metadata(path)})
        self.manifest = {'version': 1, 'source': 'macos_system_synthetic_speech',
                         'family_recordings': False, 'turns': rows}
        self.write_manifest()

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self):
        (self.folder / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_local_default_does_not_enter_live_batch_or_require_credentials(self):
        with patch.object(scene_check, 'live_batch') as live, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(scene_check.main(['--fixture-dir', str(self.folder),
                '--credential-file', str(self.folder / 'does-not-exist.json')]), 0)
        live.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual((report['mode'], report['network_calls'], report['paid_calls']),
                         ('LOCAL_CHECK', 0, 0))
        self.assertFalse(report['credentials_read'])
        self.assertEqual((report['case'], report['selected_samples']), ('full', 10))

    def test_tidying_case_selects_only_opening_and_blocks_without_network(self):
        rows = scene_check.validate_fixtures(self.folder)
        self.assertEqual([row['id'] for row in scene_check.select_case(rows, 'tidying')],
                         ['tidy_start', 'blocks'])
        with patch.object(scene_check, 'live_batch') as live, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(scene_check.main(['--fixture-dir', str(self.folder),
                                              '--case', 'tidying']), 0)
        live.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual((report['case'], report['validated_samples'], report['selected_samples']),
                         ('tidying', 10, 2))
        self.assertEqual((report['network_calls'], report['paid_calls']), (0, 0))
        with self.assertRaisesRegex(ValueError, 'invalid_scene_case'):
            scene_check.select_case(rows, 'arbitrary')

    def test_live_argument_routing_uses_selected_case_with_mocked_batch(self):
        mocked_batch = AsyncMock(return_value={'automated_checks_passed': True})
        with patch.object(scene_check, 'live_batch', mocked_batch), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scene_check.main(['--fixture-dir', str(self.folder), '--case', 'tidying',
                '--live', '--output', str(self.folder.parent / (self.folder.name + '-result'))]), 0)
        mocked_batch.assert_awaited_once()
        self.assertEqual([row['id'] for row in mocked_batch.await_args.args[2]],
                         ['tidy_start', 'blocks'])
        self.assertEqual(mocked_batch.await_args.kwargs, {'case': 'tidying'})

    def test_fixed_fictional_inputs_and_hashes_are_checked_before_live(self):
        self.manifest['turns'][0]['synthetic_text'] = 'Changed sample'
        self.write_manifest()
        with patch.object(scene_check, 'live_batch') as live:
            with self.assertRaisesRegex(ValueError, 'fixed_fictional'):
                scene_check.main(['--fixture-dir', str(self.folder), '--live',
                                  '--output', str(self.folder / 'result')])
        live.assert_not_called()
        self.manifest['turns'][0]['synthetic_text'] = scene_check.TURNS[0][1]
        self.manifest['turns'][0]['wav_sha256'] = '0' * 64
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'hash_or_duration'):
            scene_check.validate_fixtures(self.folder)

    def test_fixture_generation_does_not_overwrite_existing_directory(self):
        with patch.object(scene_check.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'already_exists'):
                scene_check.generate_fixtures(self.folder, scene_check.DEFAULT_VOICE)
        run.assert_not_called()

    def test_exact_scene_opening_and_help_replay_checks(self):
        for sample_id, opening in scene_check.SCENE_OPENINGS.items():
            self.assertTrue(all(scene_check.response_checks(sample_id,
                {'success': True, 'english': opening}, {}).values()))
            self.assertFalse(scene_check.response_checks(sample_id,
                {'success': True, 'english': 'Unrelated sentence.'}, {})['exact_scene_opening'])
        previous = {'noodles': {'english': "I'd like some noodles."}}
        self.assertTrue(all(scene_check.response_checks('meaning', {
            'success': True, 'english': "意思是想吃些面条。 I'd like some noodles."
        }, previous).values()))
        self.assertTrue(scene_check.response_checks('repeat', {
            'success': True, 'english': "I'd like some noodles."
        }, previous)['repeats_exact_pre_help_english'])
        self.assertFalse(scene_check.response_checks('repeat', {
            'success': True, 'english': 'I want noodles.'
        }, previous)['repeats_exact_pre_help_english'])


if __name__ == '__main__':
    unittest.main()
