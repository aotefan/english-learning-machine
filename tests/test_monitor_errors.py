"""Offline regression coverage for safe, actionable monitor error reporting."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cloud_report import diagnostics_report, monitor_report


class MonitorErrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'events.sqlite'
        self.stamp = time.time() - 60
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE events (time REAL, request_id TEXT, '
                       'device_id TEXT, event TEXT, data TEXT)')

    def record(self, request_id, event, data, *, device='test-device'):
        self.stamp += 0.01
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO events VALUES (?,?,?,?,?)',
                       (self.stamp, request_id, device, event,
                        json.dumps(data, ensure_ascii=False)))

    def failed(self, request_id, error, *, device='test-device'):
        self.record(request_id, 'error', {'error': error}, device=device)
        self.record(request_id, 'request_complete', {'success': False}, device=device)

    def rows(self):
        monitor = {row['request_id']: row for row in monitor_report(self.path)['requests']}
        diagnostics = {row['request_id']: row for row in diagnostics_report(self.path)['recent']}
        return monitor, diagnostics

    def test_silent_english_turn_is_success_without_missing_playback_error(self):
        self.record('silent', 'recording_started', {'mode': 'manual'})
        self.record('silent', 'asr_result', {'text': 'I found my green pencil.'})
        self.record('silent', 'intent_classified', {'intent': 'no_change'})
        self.record('silent', 'speech_not_needed', {'reason': 'natural_english_expression'})
        self.record('silent', 'request_complete', {'success': True, 'english_ms': 650})
        monitor, _ = self.rows()
        row = monitor['silent']
        self.assertEqual(row['intent'], 'no_change')
        self.assertEqual(row['stage'], '无需重复')
        self.assertTrue(row['success'])
        self.assertFalse(row['playback_ack'])
        self.assertIsNone(row['english'])
        self.assertIsNone(row['error'])

    def test_budget_rejected_before_asr_has_actionable_failure_without_transcript(self):
        self.record('budget', 'recording_started', {'mode': 'manual'})
        self.record('budget', 'audio_received', {'audio_upload_ms': 6896})
        self.record('budget', 'error', {'error': 'daily_cloud_budget_exhausted'})
        self.record('budget', 'request_complete', {'success': False, 'total_ms': 7354})

        monitor, diagnostics = self.rows()
        row = monitor['budget']
        self.assertEqual(row['error_code'], 'budget_exhausted')
        self.assertRegex(row['error'], r'额度|限额|预算')
        self.assertNotIn('请查看服务器日志', row['error'])
        self.assertIsNone(row['asr'])
        self.assertIsNone(row['english'])
        self.assertEqual(row['stage'], '失败')
        self.assertIs(row['success'], False)
        self.assertIs(row['playback_ack'], False)
        self.assertEqual(row['timings'], {'audio_upload_ms': 6896, 'total_ms': 7354})
        self.assertEqual(diagnostics['budget']['error_code'], 'budget_exhausted')
        self.assertIs(diagnostics['budget']['success'], False)

    def test_known_failures_share_categories_and_only_show_chinese_labels(self):
        cases = (
            ('empty_audio', 'empty_audio'),
            ('cloud_live_disabled', 'standby'),
            ('daily_cloud_budget_exhausted', 'budget_exhausted'),
            ('cloud_turn_rate_limited', 'rate_limited'),
            ('device_credential_revoked', 'credential_revoked'),
            ('seed_asr_failed:ValueError', 'asr_failed'),
            ('doubao_english_failed:ValueError', 'english_failed'),
            ('seed_tts_failed:ValueError', 'tts_failed'),
            ('TimeoutError', 'timeout'),
            ('synthetic_unknown_failure', 'other_error'),
        )
        for index, (error, _) in enumerate(cases):
            self.failed(str(index), error)

        monitor, diagnostics = self.rows()
        for index, (raw_error, category) in enumerate(cases):
            with self.subTest(category=category):
                row = monitor[str(index)]
                self.assertEqual(row['error_code'], category)
                self.assertEqual(diagnostics[str(index)]['error_code'], category)
                self.assertRegex(row['error'], r'[\u4e00-\u9fff]')
                self.assertNotIn(raw_error, row['error'])
                self.assertEqual(row['stage'], '失败')
                self.assertIs(row['success'], False)

    def test_arbitrary_exception_body_never_appears_in_either_response(self):
        secret = 'synthetic-private-token-DO-NOT-RENDER'
        unsafe = '<script>alert("synthetic")</script> Authorization: Bearer ' + secret
        self.failed('unknown', unsafe)
        monitor, diagnostics = self.rows()
        self.assertEqual(monitor['unknown']['error_code'], 'other_error')
        self.assertEqual(diagnostics['unknown']['error_code'], 'other_error')
        serialized = json.dumps((monitor, diagnostics), ensure_ascii=False)
        for prohibited in (secret, '<script>', 'Authorization', 'Bearer'):
            self.assertNotIn(prohibited, serialized)

    def test_known_marker_with_secret_suffix_has_identical_allowlisted_message(self):
        secret = 'synthetic-private-suffix-DO-NOT-RENDER'
        self.failed('clean', 'seed_asr_failed:ValueError')
        self.failed('unsafe', 'seed_asr_failed:ValueError:' + secret)
        monitor, diagnostics = self.rows()
        self.assertEqual(monitor['unsafe']['error_code'], 'asr_failed')
        self.assertEqual(diagnostics['unsafe']['error_code'], 'asr_failed')
        self.assertEqual(monitor['unsafe']['error'], monitor['clean']['error'])
        self.assertNotIn(secret, json.dumps((monitor, diagnostics), ensure_ascii=False))

    def test_failed_completion_without_error_is_not_treated_as_pending_or_success(self):
        self.record('incomplete', 'recording_started', {'mode': 'manual'})
        self.record('incomplete', 'request_complete', {'success': False})
        monitor, diagnostics = self.rows()
        self.assertIs(monitor['incomplete']['success'], False)
        self.assertEqual(monitor['incomplete']['stage'], '失败')
        self.assertIsNone(monitor['incomplete']['error_code'])
        self.assertIsNone(monitor['incomplete']['error'])
        self.assertIs(diagnostics['incomplete']['success'], False)
        self.assertIsNone(diagnostics['incomplete']['error_code'])

    def test_cloud_smoke_excluded_from_household_monitor_but_available_in_diagnostics(self):
        self.failed('household', 'daily_cloud_budget_exhausted')
        self.failed('synthetic-smoke', 'seed_tts_failed:ValueError', device='cloud-smoke')
        monitor, diagnostics = self.rows()
        self.assertEqual(set(monitor), {'household'})
        self.assertEqual(set(diagnostics), {'household', 'synthetic-smoke'})
        summary = diagnostics_report(self.path)
        self.assertEqual(summary['requests_in_window'], 2)
        self.assertEqual(summary['failed'], 2)


if __name__ == '__main__':
    unittest.main()
