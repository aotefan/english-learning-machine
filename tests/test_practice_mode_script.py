"""Offline guards for the four-turn single practice-mode smoke script."""
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
import test_practice_mode as practice


class PracticeModeScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        rows = []
        for sample_id, text in practice.TURNS:
            path = self.folder / (sample_id + '.wav')
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0' * 3200)
            rows.append({'id': sample_id, 'synthetic_text': text, **practice.wav_metadata(path)})
        self.manifest = {'version': 1, 'source': 'macos_system_synthetic_speech',
                         'family_recordings': False, 'turns': rows}
        self.write_manifest()

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self):
        (self.folder / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_default_local_check_never_enters_live_or_reads_missing_credentials(self):
        with patch.object(practice, 'live_batch') as live, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(practice.main(['--fixture-dir', str(self.folder),
                '--credential-file', str(self.folder / 'does-not-exist.json')]), 0)
        live.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual((result['mode'], result['validated_samples'], result['paid_calls']),
                         ('LOCAL_CHECK', 4, 0))
        self.assertEqual(result['network_calls'], 0)
        self.assertFalse(result['credentials_read'])

    def test_exact_fixed_four_turns_passed_to_mocked_live_batch(self):
        batch = AsyncMock(return_value={'automated_checks_passed': True})
        with patch.object(practice, 'live_batch', batch), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(practice.main(['--fixture-dir', str(self.folder), '--live',
                '--output', str(self.folder.parent / (self.folder.name + '-result'))]), 0)
        batch.assert_awaited_once()
        self.assertEqual([(row['id'], row['synthetic_text']) for row in batch.await_args.args[2]],
                         list(practice.TURNS))
        self.assertEqual(len(batch.await_args.args[2]), 4)

    def test_changed_text_or_hash_rejected_before_live(self):
        self.manifest['turns'][0]['synthetic_text'] = 'Arbitrary recording'
        self.write_manifest()
        with patch.object(practice, 'live_batch') as live:
            with self.assertRaisesRegex(ValueError, 'fixed_fictional'):
                practice.main(['--fixture-dir', str(self.folder), '--live'])
        live.assert_not_called()
        self.manifest['turns'][0]['synthetic_text'] = practice.TURNS[0][1]
        self.manifest['turns'][0]['wav_sha256'] = '0' * 64
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'hash_or_duration'):
            practice.validate_fixtures(self.folder)

    def test_no_overwrite_and_no_generation_combined_with_live(self):
        with patch.object(practice.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'already_exists'):
                practice.generate_fixtures(self.folder, practice.DEFAULT_VOICE)
        run.assert_not_called()
        with patch.object(practice, 'generate_fixtures') as generate, \
                patch.object(practice, 'live_batch') as live, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                practice.main(['--generate-fixtures', '--live'])
        generate.assert_not_called()
        live.assert_not_called()

    def test_mode_announcements_exact_and_topic_checks_are_only_weak_signals(self):
        for sample_id, text in practice.ANNOUNCEMENTS.items():
            self.assertTrue(all(practice.response_checks(sample_id, {'success': True, 'english': text}).values()))
            self.assertFalse(practice.response_checks(sample_id, {
                'success': True, 'english': text + ' Extra words.'})['exact_mode_announcement'])
        self.assertTrue(practice.response_checks('noodles', {
            'success': True, 'english': 'What kind of noodles would you like?'})['expected_topic_words'])
        self.assertTrue(practice.response_checks('book', {
            'success': True, 'english': 'What book would you like to read?'})['expected_topic_words'])
        self.assertFalse(practice.response_checks('book', {
            'success': True, 'english': 'What kind of noodles?'})['expected_topic_words'])


if __name__ == '__main__':
    unittest.main()
