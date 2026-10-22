"""In-memory fixtures test the gate; they are not physical acceptance records."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from acceptance_report import EVENTS, RECOVERY_KEYS, TIMINGS, evaluate


class AcceptanceGateTest(unittest.TestCase):
    def setUp(self):
        self.expected={f'{i:02}':f'unit-test-{i}' for i in range(1,21)}
        self.rows=[];self.events=[];self.playback={'unit-test-only':[]}
        self.recovery={key:True for key in RECOVERY_KEYS}
        for i,(sentence_id,zh) in enumerate(self.expected.items(),1):
            rid=f'{i:032x}'
            row={'id':sentence_id,'zh':zh,'status':'pass','request_id':rid,
                 'asr':zh,'english':'Test sentence.','speaker_heard':'yes',
                 'server_no_crash':'yes','playback_log':'unit-test-only',
                 **{k:'100' for k in TIMINGS}}
            self.rows.append(row)
            for event in EVENTS:
                record={'event':event,'request_id':rid}
                if event=='request_complete':record.update(success=True,**{k:100 for k in TIMINGS})
                if event=='asr_result':record['text']=zh
                if event=='english_result':record['text']='Test sentence.'
                self.events.append(record)
            self.playback['unit-test-only'].append({'event':'playback_started','request_id':rid,
                                                    'source':'codec_output_returned'})

    def report(self):
        return evaluate(self.rows,self.recovery,self.expected,self.events,self.playback)

    def test_eighteen_valid_two_failed_with_all_evidence_can_pass(self):
        self.rows[-1]['status']=self.rows[-2]['status']='fail'
        result=self.report()
        self.assertTrue(result['mvp_passed']);self.assertEqual(result['passed'],18)

    def test_missing_recovery_checks_cannot_pass(self):
        self.recovery={}
        self.assertFalse(self.report()['mvp_passed'])

    def test_unverified_attempt_is_neither_a_pass_nor_a_confirmed_failure(self):
        self.rows[-1]['status'] = 'unverified'
        self.rows[-1]['speaker_heard'] = ''
        result = self.report()
        self.assertEqual(result['attempted'], 20)
        self.assertEqual(result['passed'], 19)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['unverified'], 1)
        self.assertFalse(result['sentence_gate_passed'])
        self.assertFalse(result['mvp_passed'])

    def test_unknown_or_string_recovery_values_cannot_pass(self):
        for value in (None,'true',1):
            with self.subTest(value=value):
                self.recovery['wifi_loss_device_reconnect']=value
                self.assertFalse(self.report()['mvp_passed'])

    def test_duplicate_sentence_or_request_cannot_inflate_success(self):
        self.rows[-1]=copy.deepcopy(self.rows[0])
        self.assertFalse(self.report()['mvp_passed'])

    def test_matching_server_send_without_playback_does_not_pass(self):
        self.playback={}
        self.assertFalse(self.report()['mvp_passed'])

    def test_wrong_request_id_in_playback_does_not_pass(self):
        for row in self.playback['unit-test-only']:row['request_id']='f'*32
        self.assertFalse(self.report()['mvp_passed'])

    def test_missing_or_invalid_timings_reject_claimed_pass(self):
        for value in ('','nan','inf','-1','9999'):
            with self.subTest(value=value):
                self.rows[0]['asr_ms']=value
                self.assertFalse(self.report()['mvp_passed'])

    def test_acoustic_observation_still_required(self):
        self.rows[0]['speaker_heard']='no'
        self.assertFalse(self.report()['mvp_passed'])

    def test_duplicate_terminal_or_server_error_invalidates_pass(self):
        terminal=next(r for r in self.events if r['event']=='request_complete')
        self.events.append(copy.deepcopy(terminal))
        self.assertFalse(self.report()['mvp_passed'])

    def test_changed_translation_cannot_match_another_request(self):
        self.rows[0]['english']='Unrelated output.'
        self.assertFalse(self.report()['mvp_passed'])
