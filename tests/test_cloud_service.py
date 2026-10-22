import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from cloud_setup import load_cloud_config, initialize_config
initialize_config(load_cloud_config(require_credentials=False))
from cloud_service import CloudPolicy, attach_policy
from cloud_learning import learning_report, mark_reviewed, mark_practiced, set_candidate_decision
from cloud_report import report, billing_report, diagnostics_report, monitor_report
from core.websocket_server import WebSocketServer
import websockets
from websockets.datastructures import Headers


class CloudServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        self.registry=self.folder/'devices.json'
        self.registry.write_text(json.dumps({'device':hashlib.sha256(b'random-token').hexdigest()}))
        self.registry.chmod(0o600)
        self.policy=CloudPolicy(self.folder,self.registry)
        self.request=SimpleNamespace(path='/xiaozhi/v1/',headers=Headers({
            'Device-Id':'device','Authorization':'Bearer random-token'}))
        self.conn=SimpleNamespace(device_id='device',mvp_request_id='r',websocket=SimpleNamespace(request=self.request))

    def tearDown(self):
        self.temp.cleanup()

    def _seed_review_rows(self, now, entries):
        import sqlite3
        with sqlite3.connect(self.policy.db_path) as db:
            for entry in entries:
                row={'time':now,'device_id':'device','asr_raw':'测试表达',
                     'english':'I want some water.','playback_reported':1,**entry}
                db.execute(f'INSERT INTO learning({",".join(row)}) VALUES({",".join("?" for _ in row)})',
                           tuple(row.values()))

    def test_revocation_query_and_header_validation(self):
        self.assertTrue(self.policy.authenticate(self.request))
        self.request.path+='?authorization=random-token'
        self.assertFalse(self.policy.authenticate(self.request))
        self.request.path='/xiaozhi/v1/'
        self.registry.write_text(json.dumps({'other':hashlib.sha256(b'other').hexdigest()}))
        self.assertFalse(self.policy.authenticate(self.request))

    def test_standby_cannot_spend_and_budget_persists(self):
        with self.assertRaisesRegex(ValueError,'disabled'):
            self.policy.before_turn(self.conn,5)
        env=dict(ASR_CNY_PER_HOUR='1',LLM_INPUT_CNY_PER_MILLION='.2',LLM_OUTPUT_CNY_PER_MILLION='2',
                 TTS_CNY_PER_10K_CHARS='3',DAILY_CNY_LIMIT='1',DAILY_TURN_LIMIT='1')
        with patch.dict(os.environ,env):
            live=CloudPolicy(self.folder,self.registry,live=True)
            self.assertEqual(live.before_turn(self.conn,5)['daily_turns'],1)
            restarted=CloudPolicy(self.folder,self.registry,live=True)
            with self.assertRaisesRegex(ValueError,'budget_exhausted'):
                restarted.before_turn(self.conn,5)

    def test_reservation_covers_expanded_teaching_input_before_calling_providers(self):
        # This limit covers the former 4096-token estimate, but cannot cover
        # the current 6144-byte teaching input envelope plus audio/output.
        env=dict(ASR_CNY_PER_HOUR='1',LLM_INPUT_CNY_PER_MILLION='.2',LLM_OUTPUT_CNY_PER_MILLION='2',
                 TTS_CNY_PER_10K_CHARS='3',DAILY_CNY_LIMIT='.123',DAILY_TURN_LIMIT='100')
        with patch.dict(os.environ,env):
            live=CloudPolicy(self.folder,self.registry,live=True)
            with self.assertRaisesRegex(ValueError,'budget_exhausted'):
                live.before_turn(self.conn,5)
            self.assertEqual(live.runtime_status()['budget']['turns'],0)

    def test_usage_and_original_text_survive_restart(self):
        import sqlite3
        for row in ({'request_id':'r','event':'asr_result','text':'嗯，I goed home yesterday。'},
                    {'request_id':'r','event':'english_result','text':'I went home yesterday.'},
                    {'request_id':'r','event':'playback_started'},
                    {'request_id':'r','event':'playback_confirmed'},
                    {'request_id':'r','event':'request_complete','asr_ms':20,'success':True}):
            self.policy.record(self.conn,row)
        with sqlite3.connect(self.policy.db_path) as db:
            self.assertEqual(db.execute('SELECT asr_raw,english,playback_reported FROM learning').fetchone(),
                             ('嗯，I goed home yesterday。','I went home yesterday.',1))
        self.assertEqual(report(self.policy.db_path)['recent_mean_ms']['asr_ms'],20)

    def test_learning_write_waits_for_completion_without_claiming_playback(self):
        import sqlite3
        for row in ({'event':'asr_result','text':'synthetic source'},
                    {'event':'intent_classified','intent':'express'},
                    {'event':'english_result','text':'I need some paper.'}):
            self.policy.record(self.conn,{'request_id':'r',**row})
        with sqlite3.connect(self.policy.db_path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM learning').fetchone()[0],0)
        self.policy.record(self.conn,{'request_id':'r','event':'request_complete','success':True})
        with sqlite3.connect(self.policy.db_path) as db:
            self.assertEqual(db.execute('SELECT asr_raw,english,playback_reported FROM learning').fetchone(),
                ('synthetic source','I need some paper.',0))
        self.assertIsNone(self.policy.last_played_english('device'))
        self.policy.record(self.conn,{'request_id':'r','event':'playback_confirmed'})
        self.assertEqual(self.policy.last_played_english('device'),'I need some paper.')

    def test_repeat_uses_recent_acknowledged_expression_from_same_device(self):
        import time
        self.policy.record(self.conn,{'request_id':'first','event':'asr_result','text':'我要喝水'})
        self.policy.record(self.conn,{'request_id':'first','event':'english_result','text':'I want some water.'})
        self.policy.cache_pending_audio('device','first','I want some water.',b'generated-wav')
        self.assertIsNone(self.policy.last_played_english('device'))
        self.assertIsNone(self.policy.last_played_audio('device','I want some water.'))
        self.policy.record(self.conn,{'request_id':'first','event':'playback_started'})
        self.assertIsNone(self.policy.last_played_english('device'))
        self.policy.record(self.conn,{'request_id':'first','event':'playback_confirmed'})
        self.assertEqual(self.policy.last_played_english('device'),'I want some water.')
        self.assertEqual(self.policy.last_played_audio('device','I want some water.'),b'generated-wav')
        self.assertIsNone(self.policy.last_played_audio('other','I want some water.'))
        self.assertIsNone(self.policy.last_played_audio('device','different sentence'))
        self.assertIsNone(self.policy.last_played_english('other'))
        self.policy.record(self.conn,{'request_id':'repeat','event':'asr_result','text':'say again'})
        self.policy.record(self.conn,{'request_id':'repeat','event':'intent_classified','intent':'repeat'})
        self.policy.record(self.conn,{'request_id':'repeat','event':'english_result','text':'I want some water.'})
        self.policy.record(self.conn,{'request_id':'repeat','event':'playback_started'})
        self.policy.record(self.conn,{'request_id':'repeat','event':'playback_confirmed'})
        self.assertEqual(self.policy.last_played_english('device'),'I want some water.')
        self.assertEqual([card['request_id'] for card in learning_report(self.policy.db_path)['candidates']],['first'])
        self.assertEqual(set_candidate_decision(self.policy.db_path,'first','select')['status'],'selected')
        cards=learning_report(self.policy.db_path)['cards']
        self.assertEqual([card['request_id'] for card in cards],['first'])
        self.assertEqual(monitor_report(self.policy.db_path)['requests'][0]['intent'],'repeat')
        self.assertIsNone(self.policy.last_played_english('device',now=time.time()+601))
        self.assertIsNone(self.policy.last_played_audio('device','I want some water.',now=time.time()+601))

    def test_acknowledged_chat_reply_is_repeatable_but_not_a_review_card(self):
        for rid,intent,spoken in (
            ('first','express',"I'm hungry."),
            ('second','chat','What would you like to eat?'),
        ):
            self.policy.record(self.conn,{'request_id':rid,'event':'asr_result','text':'synthetic'})
            self.policy.record(self.conn,{'request_id':rid,'event':'intent_classified','intent':intent})
            self.policy.record(self.conn,{'request_id':rid,'event':'english_result','text':spoken})
            self.policy.record(self.conn,{'request_id':rid,'event':'playback_confirmed'})
        self.assertEqual(self.policy.last_played_english('device'),'What would you like to eat?')
        set_candidate_decision(self.policy.db_path,'first','select')
        self.assertEqual([card['request_id'] for card in learning_report(self.policy.db_path)['cards']],
                         ['first'])
        rejected=self.policy.reject_last_expression('device')
        self.assertEqual(rejected['request_id'],'second')
        self.assertIsNone(self.policy.last_played_english('device'))
        self.assertEqual([card['request_id'] for card in learning_report(self.policy.db_path)['cards']],
                         ['first'])

    def test_help_turns_do_not_replace_last_expression_or_create_review_cards(self):
        for rid, intent, spoken in (
            ('idea', 'express', 'Can I have some more juice?'),
            ('meaning', 'explain', 'some more 是再来一些。'),
            ('unrelated', 'explain_only', 'say again 是再说一遍。'),
            ('slower', 'slow', 'Can I have some more juice?'),
            ('question', 'clarify', '你说的是哪个东西？'),
        ):
            self.conn.mvp_request_id=rid
            self.policy.record(self.conn,{'request_id':rid,'event':'asr_result','text':'synthetic input'})
            self.policy.record(self.conn,{'request_id':rid,'event':'intent_classified','intent':intent})
            self.policy.record(self.conn,{'request_id':rid,'event':'english_result','text':spoken})
            self.policy.record(self.conn,{'request_id':rid,'event':'playback_started'})
            self.policy.record(self.conn,{'request_id':rid,'event':'playback_confirmed'})
        self.assertEqual(self.policy.last_played_english('device'),'Can I have some more juice?')
        set_candidate_decision(self.policy.db_path,'idea','select')
        self.assertEqual([row['request_id'] for row in learning_report(self.policy.db_path)['cards']],['idea'])
        report = monitor_report(self.policy.db_path)
        self.assertEqual(next(row['intent'] for row in report['requests']
                              if row['request_id'] == 'unrelated'), 'explain_only')

    def test_rejected_expression_is_not_replayed_or_reviewed(self):
        import sqlite3
        for rid, spoken in (('older','Where is my coat?'), ('mistake','Where is my red hat?')):
            self.policy.record(self.conn,{'request_id':rid,'event':'asr_result','text':'synthetic input'})
            self.policy.record(self.conn,{'request_id':rid,'event':'intent_classified','intent':'express'})
            self.policy.record(self.conn,{'request_id':rid,'event':'english_result','text':spoken})
            self.policy.cache_pending_audio('device',rid,spoken,b'generated-wav')
            self.policy.record(self.conn,{'request_id':rid,'event':'playback_confirmed'})
        self.assertEqual(self.policy.last_played_english('device'),'Where is my red hat?')
        rejected=self.policy.reject_last_expression('device')
        self.assertEqual(rejected['request_id'],'mistake')
        self.assertIsNone(self.policy.last_played_english('device'))
        self.assertIsNone(self.policy.last_played_audio('device','Where is my red hat?'))
        set_candidate_decision(self.policy.db_path,'older','select')
        self.assertEqual([card['request_id'] for card in learning_report(self.policy.db_path)['cards']],['older'])
        self.assertIsNone(mark_reviewed(self.policy.db_path,'mistake'))
        self.assertIsNone(mark_practiced(self.policy.db_path,'mistake'))
        self.assertIsNone(self.policy.reject_last_expression('device'))
        with sqlite3.connect(self.policy.db_path) as db:
            self.assertEqual(db.execute('SELECT rejected, english FROM learning WHERE request_id=?',
                                        ('mistake',)).fetchone(),(1,'Where is my red hat?'))

    def test_correction_does_not_reject_an_older_card_for_unconfirmed_audio(self):
        for rid, spoken, confirmed in (
            ('older','Where is my coat?',True),('pending','Where is my red hat?',False)):
            self.policy.record(self.conn,{'request_id':rid,'event':'asr_result','text':'synthetic input'})
            self.policy.record(self.conn,{'request_id':rid,'event':'intent_classified','intent':'express'})
            self.policy.record(self.conn,{'request_id':rid,'event':'english_result','text':spoken})
            if confirmed:
                self.policy.record(self.conn,{'request_id':rid,'event':'playback_confirmed'})
        self.assertIsNone(self.policy.reject_last_expression('device'))
        self.assertIsNone(self.policy.last_played_english('device'))
        set_candidate_decision(self.policy.db_path,'older','select')
        self.assertEqual([card['request_id'] for card in learning_report(self.policy.db_path)['cards']],['older'])

    def test_review_schedule_and_legacy_learning_migration(self):
        import sqlite3,time
        now=time.time()
        legacy=self.folder/'legacy';legacy.mkdir()
        with sqlite3.connect(legacy/'usage.sqlite') as db:
            db.execute('CREATE TABLE learning (request_id TEXT PRIMARY KEY, time REAL, device_id TEXT, asr_raw TEXT, english TEXT, playback_reported INTEGER DEFAULT 0)')
            db.execute('INSERT INTO learning VALUES (?,?,?,?,?,?)',
                       ('old',now,'device','妈妈，我想喝水。','Mom, I want to drink water.',1))
        migrated=CloudPolicy(legacy,self.registry)
        first=learning_report(migrated.db_path,now=now)
        self.assertEqual(first['due_count'],0)
        self.assertEqual(first['cards'],[])
        self.assertEqual([row['request_id'] for row in first['candidates']],['old'])
        self.assertEqual(set_candidate_decision(migrated.db_path,'old','select',now=now)['status'],'selected')
        first=learning_report(migrated.db_path,now=now)
        self.assertEqual(first['due_count'],1)
        self.assertEqual(first['cards'][0]['review_count'],0)
        self.assertEqual(first['cards'][0]['practice_count'],0)
        self.assertEqual(first['cards'][0]['independent_count'],0)
        self.assertEqual(first['cards'][0]['assisted_count'],0)
        self.assertEqual(first['cards'][0]['request_id'],'old')
        practiced=mark_practiced(migrated.db_path,'old',now=now)
        self.assertEqual(practiced['practice_count'],1)
        self.assertEqual(mark_practiced(migrated.db_path,'old',now=now+1)['status'],'too_soon')
        self.assertEqual(mark_practiced(migrated.db_path,'old',now=now+10)['practice_count'],2)
        self.assertEqual(learning_report(migrated.db_path,now=now+10)['cards'][0]['practice_count'],2)
        done=mark_reviewed(migrated.db_path,'old',now=now)
        self.assertEqual(done['review_count'],1)
        self.assertEqual(done['next_review_at'],now+86400)
        self.assertEqual(mark_reviewed(migrated.db_path,'old',now=now+1)['status'],'not_due')
        self.assertEqual(mark_reviewed(migrated.db_path,'old',now=now+86400)['review_count'],2)
        self.assertEqual(learning_report(migrated.db_path,now=now+7*86400+1)['cards'],[])

    def test_recall_outcomes_are_self_reported_and_schedule_follow_up(self):
        import sqlite3,time
        now=time.time()
        with sqlite3.connect(self.policy.db_path) as db:
            db.execute('''INSERT INTO learning(request_id,time,device_id,asr_raw,english,playback_reported)
                          VALUES(?,?,?,?,?,1)''',('recall',now,'device','我想喝水','I want some water.'))
        set_candidate_decision(self.policy.db_path,'recall','select',now=now)
        independent=mark_reviewed(self.policy.db_path,'recall',outcome='independent',now=now)
        self.assertEqual(independent['next_review_at'],now+2*86400)
        self.assertEqual((independent['independent_count'],independent['assisted_count']),(1,0))
        self.assertEqual(mark_reviewed(self.policy.db_path,'recall',outcome='assisted',now=now+1)['status'],'not_due')
        assisted=mark_reviewed(self.policy.db_path,'recall',outcome='assisted',now=now+2*86400)
        self.assertEqual(assisted['next_review_at'],now+3*86400)
        self.assertEqual((assisted['independent_count'],assisted['assisted_count']),(1,1))
        card=learning_report(self.policy.db_path,now=now+2*86400)['cards'][0]
        self.assertEqual(card['last_review_outcome'],'assisted')
        with self.assertRaisesRegex(ValueError,'invalid_review_outcome'):
            mark_reviewed(self.policy.db_path,'recall',outcome='mastered',now=now+3*86400)

    def test_review_support_validation_preserves_counts_and_existing_schedule(self):
        import sqlite3,time
        now=time.time()
        with sqlite3.connect(self.policy.db_path) as db:
            db.execute('''INSERT INTO learning(request_id,time,device_id,asr_raw,english,playback_reported)
                          VALUES(?,?,?,?,?,1)''',('supported',now,'device','我想喝水','I want some water.'))
        set_candidate_decision(self.policy.db_path,'supported','select',now=now)
        before=learning_report(self.policy.db_path,now=now)['cards'][0]
        for support in ('', 'unknown', None, 0, True, [], {}):
            with self.subTest(support=support):
                with self.assertRaisesRegex(ValueError,'invalid_review_support'):
                    mark_reviewed(self.policy.db_path,'supported',outcome='assisted',support=support,now=now)
        for support in ('hint','answer'):
            with self.subTest(independent_support=support):
                with self.assertRaisesRegex(ValueError,'independent_review_cannot_use_support'):
                    mark_reviewed(self.policy.db_path,'supported',outcome='independent',support=support,now=now)
        self.assertEqual(learning_report(self.policy.db_path,now=now)['cards'][0],before)
        hinted=mark_reviewed(self.policy.db_path,'supported',outcome='assisted',support='hint',now=now)
        self.assertEqual((hinted['review_count'],hinted['independent_count'],hinted['assisted_count']),(1,0,1))
        self.assertEqual(hinted['next_review_at'],now+86400)
        self.assertEqual(mark_reviewed(self.policy.db_path,'supported',outcome='assisted',support='answer',
                                       now=now+1)['status'],'not_due')
        answered=mark_reviewed(self.policy.db_path,'supported',outcome='assisted',support='answer',now=now+86400)
        self.assertEqual((answered['review_count'],answered['independent_count'],answered['assisted_count']),(2,0,2))
        self.assertEqual(answered['next_review_at'],now+2*86400)
        independent=mark_reviewed(self.policy.db_path,'supported',outcome='independent',support='none',now=now+2*86400)
        self.assertEqual((independent['review_count'],independent['independent_count'],independent['assisted_count']),(3,1,2))
        self.assertEqual(independent['next_review_at'],now+4*86400)

    def test_duplicate_review_cards_keep_counts_and_latest_review_schedule(self):
        import sqlite3,time
        now=time.time()
        self._seed_review_rows(now,[
            {'request_id':'first','time':now-100,'review_selected':1,'review_count':2,
             'independent_count':1,'assisted_count':1,'practice_count':3,
             'last_reviewed_at':now-10,'next_review_at':now+86400,'last_review_outcome':'assisted'},
            {'request_id':'later-input','time':now-50,'english':' I  WANT some water! ',
             'review_selected':1,'review_count':1,'independent_count':1,'practice_count':2,
             'last_reviewed_at':now-20,'next_review_at':now-1,'last_review_outcome':'independent'},
            {'request_id':'new-duplicate','english':'I want\tsome water。'},
        ])
        with sqlite3.connect(self.policy.db_path) as db:
            db.execute('INSERT INTO billing_turns(request_id,time,reserved_cny,status) VALUES(?,?,?,?)',
                       ('new-duplicate',now,.01,'success'))
            original=db.execute('SELECT * FROM learning ORDER BY request_id').fetchall()
        listing=learning_report(self.policy.db_path,now=now)
        self.assertEqual(len(listing['cards']),1)
        self.assertEqual(listing['candidates'],[])
        self.assertEqual(listing['due_count'],0)
        card=listing['cards'][0]
        self.assertEqual((card['review_count'],card['practice_count'],card['independent_count'],
                          card['assisted_count']),(3,5,2,1))
        self.assertEqual((card['last_reviewed_at'],card['next_review_at'],card['last_review_outcome']),
                         (now-10,now+86400,'assisted'))
        self.assertEqual(card['occurrence_count'],3)
        self.assertEqual(len(card['review_key']),64)
        self._seed_review_rows(now,[{'request_id':'another-duplicate','english':'i want some water?'}])
        repeated=learning_report(self.policy.db_path,now=now)['cards'][0]
        self.assertEqual(repeated['review_key'],card['review_key'])
        self.assertEqual(repeated['request_id'],card['request_id'])
        self.assertEqual(repeated['next_review_at'],card['next_review_at'])
        with sqlite3.connect(self.policy.db_path) as db:
            self.assertEqual(db.execute('SELECT * FROM learning WHERE request_id != ? ORDER BY request_id',
                                        ('another-duplicate',)).fetchall(),original)
            self.assertEqual(db.execute('SELECT count(*) FROM billing_turns').fetchone()[0],1)

    def test_review_deduplication_is_scoped_to_device_exact_words_and_retention(self):
        import time
        now=time.time()
        self._seed_review_rows(now,[
            {'request_id':'old-case','time':now-1},
            {'request_id':'same-words','english':'  i want\nsome water . ! '},
            {'request_id':'other-device','device_id':'other-device'},
            {'request_id':'other-word','english':'I need some water.'},
            {'request_id':'negation','english':"I don't want some water."},
            {'request_id':'internal-comma','english':'I want, some water.'},
            {'request_id':'quote','english':'"I want some water."'},
            {'request_id':'expired','time':now-7*86400-1,'review_selected':1},
            {'request_id':'not-played','playback_reported':0,'review_selected':1},
            {'request_id':'rejected','rejected':1,'review_selected':1},
            {'request_id':'chat','intent':'chat','review_selected':1},
            {'request_id':'diagnostic','device_id':'cloud-smoke','review_selected':1},
        ])
        listing=learning_report(self.policy.db_path,now=now)
        self.assertEqual(listing['cards'],[])
        candidates={row['request_id']:row for row in listing['candidates']}
        self.assertEqual(set(candidates),{'same-words','other-device','other-word','negation','internal-comma','quote'})
        self.assertEqual(candidates['same-words']['occurrence_count'],2)
        self.assertEqual(len({row['review_key'] for row in candidates.values()}),6)
        self.assertIsNone(set_candidate_decision(self.policy.db_path,'expired','select',now=now))

    def test_review_and_practice_aliases_cannot_double_count_the_same_group(self):
        import sqlite3,time
        from concurrent.futures import ThreadPoolExecutor
        now=time.time()
        self._seed_review_rows(now,[
            {'request_id':'a','time':now-20,'review_selected':1,'review_count':2,
             'independent_count':1,'assisted_count':1,'practice_count':3,
             'last_reviewed_at':now-2*86400,'next_review_at':now-1},
            {'request_id':'b','time':now-10,'review_selected':1,'review_count':1,
             'independent_count':1,'practice_count':2,'last_reviewed_at':now-86400,'next_review_at':now-1},
            {'request_id':'c'},
        ])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda rid:mark_reviewed(self.policy.db_path,rid,
                outcome='independent',now=now),('a','c')))
        self.assertEqual(sorted(result['status'] for result in results),['not_due','reviewed'])
        card=learning_report(self.policy.db_path,now=now)['cards'][0]
        self.assertEqual((card['review_count'],card['independent_count'],card['assisted_count']),(4,3,1))
        self.assertEqual(card['next_review_at'],now+3*86400)
        self.assertEqual(mark_reviewed(self.policy.db_path,'b',now=now+1)['status'],'not_due')
        with ThreadPoolExecutor(max_workers=2) as pool:
            practiced=list(pool.map(lambda rid:mark_practiced(self.policy.db_path,rid,now=now),('b','c')))
        self.assertEqual(sorted(result['status'] for result in practiced),['practiced','too_soon'])
        self.assertEqual(mark_practiced(self.policy.db_path,'a',now=now+1)['status'],'too_soon')
        self.assertEqual(mark_practiced(self.policy.db_path,'a',now=now+10)['practice_count'],7)
        with sqlite3.connect(self.policy.db_path) as db:
            counts=db.execute('SELECT sum(review_count),sum(practice_count),sum(independent_count),'
                              'sum(assisted_count) FROM learning').fetchone()
            self.assertEqual(counts,(4,7,3,1))
            self.assertEqual(db.execute('SELECT count(*) FROM learning').fetchone()[0],3)

    def test_candidate_decisions_apply_to_duplicates_and_future_matching_inputs(self):
        import sqlite3,time
        now=time.time()
        self._seed_review_rows(now,[{'request_id':'a','time':now-1},{'request_id':'b'}])
        candidate=learning_report(self.policy.db_path,now=now)['candidates'][0]
        selected=set_candidate_decision(self.policy.db_path,'a','select',now=now)
        self.assertEqual(selected,{'status':'selected','request_id':'b','review_key':candidate['review_key']})
        self.assertEqual(set_candidate_decision(self.policy.db_path,'b','dismiss',now=now)['status'],'already_decided')
        self._seed_review_rows(now,[{'request_id':'c'}])
        listing=learning_report(self.policy.db_path,now=now)
        self.assertEqual(len(listing['cards']),1)
        self.assertEqual(listing['candidates'],[])
        self.assertEqual(listing['cards'][0]['review_key'],candidate['review_key'])
        self.assertEqual(set_candidate_decision(self.policy.db_path,'c','select',now=now)['status'],'already_decided')
        self._seed_review_rows(now,[{'request_id':'dismiss-a','english':'Good night.'},
                                   {'request_id':'dismiss-b','english':' good NIGHT! '}])
        self.assertEqual(set_candidate_decision(self.policy.db_path,'dismiss-a','dismiss',now=now)['status'],'dismissed')
        self._seed_review_rows(now,[{'request_id':'dismiss-c','english':'Good night?'}])
        self.assertEqual(set_candidate_decision(self.policy.db_path,'dismiss-c','select',now=now)['status'],'already_decided')
        self.assertIsNone(mark_reviewed(self.policy.db_path,'dismiss-c',now=now))
        self.assertIsNone(mark_practiced(self.policy.db_path,'dismiss-b',now=now))
        self.assertEqual(learning_report(self.policy.db_path,now=now)['candidates'],[])
        with sqlite3.connect(self.policy.db_path) as db:
            self.assertEqual(db.execute('SELECT sum(review_selected) FROM learning WHERE request_id IN (?,?)',
                                        ('a','b')).fetchone()[0],2)
            self.assertEqual(db.execute('SELECT sum(review_dismissed) FROM learning WHERE request_id IN (?,?)',
                                        ('dismiss-a','dismiss-b')).fetchone()[0],2)

    def test_review_limits_and_due_count_apply_after_grouping(self):
        import time
        now=time.time()
        selected=[{'request_id':f'card-{index}','english':f'I have {index} pencils.','review_selected':1}
                  for index in range(52)]
        candidates=[{'request_id':f'candidate-{index}','english':f'I have {index} books.'}
                    for index in range(22)]
        duplicates=[{'request_id':f'duplicate-{index}','english':' I HAVE 0 pencils! ','review_selected':1}
                    for index in range(55)]
        self._seed_review_rows(now,selected+candidates+duplicates)
        listing=learning_report(self.policy.db_path,now=now)
        self.assertEqual((len(listing['cards']),listing['due_count'],listing['truncated']),(50,52,True))
        self.assertEqual((len(listing['candidates']),listing['candidates_truncated']),(20,True))
        self.assertEqual(len({row['review_key'] for row in listing['cards']}),50)

    def test_review_progress_counts_groups_and_filters_before_limit(self):
        import time
        now=time.time()
        self._seed_review_rows(now,[
            {'request_id':f'new-{index}','english':f'I have {index} pencils.',
             'review_selected':1} for index in range(52)] + [
            {'request_id':'help','time':now-100,'english':'Can I have some paper?',
             'review_selected':1,'review_count':1,'assisted_count':1,
             'last_reviewed_at':now-20,'next_review_at':now+100,'last_review_outcome':'assisted'},
            {'request_id':'help-duplicate','time':now-50,'english':'CAN I HAVE SOME PAPER!',
             'review_count':1,'independent_count':1,'last_reviewed_at':now-30,
             'next_review_at':now+100,'last_review_outcome':'independent'},
            {'request_id':'follow-only','english':'I like drawing.','review_selected':1,
             'practice_count':8},
            {'request_id':'diagnostic','device_id':'cloud-smoke','english':'A test.',
             'review_selected':1,'assisted_count':9},
            {'request_id':'rejected','english':'Wrong.','review_selected':1,
             'rejected':1,'independent_count':9},
            {'request_id':'unplayed','english':'Unheard.','review_selected':1,
             'playback_reported':0,'independent_count':9},
            {'request_id':'expired','time':now-8*86400,'english':'Old.',
             'review_selected':1,'independent_count':9}])
        report=learning_report(self.policy.db_path,now=now)
        self.assertEqual(report['summary'],dict(selected_count=54,due_count=53,
                         needs_help_count=1,independent_count=1,assisted_count=1))
        self.assertTrue(report['truncated'])
        self.assertNotIn('help',[card['request_id'] for card in report['cards']])
        helped=learning_report(self.policy.db_path,now=now,view='needs_help')
        self.assertEqual([card['request_id'] for card in helped['cards']],['help'])
        self.assertEqual(helped['matched_count'],1)
        self.assertFalse(helped['truncated'])
        self.assertEqual(helped['summary'],report['summary'])
        self.assertFalse(helped['cards'][0]['due'])
        due=learning_report(self.policy.db_path,now=now,view='due')
        self.assertEqual(due['matched_count'],53)
        self.assertTrue(all(card['due'] for card in due['cards']))
        mark_reviewed(self.policy.db_path,'help',outcome='independent',now=now+101)
        improved=learning_report(self.policy.db_path,now=now+101,view='needs_help')
        self.assertEqual(improved['cards'],[])
        self.assertEqual(improved['summary']['needs_help_count'],0)
        self.assertEqual(improved['summary']['assisted_count'],1)
        self.assertEqual(improved['summary']['independent_count'],2)
        with self.assertRaisesRegex(ValueError,'invalid_review_view'):
            learning_report(self.policy.db_path,view='mastered')

    def test_diagnostic_device_never_creates_a_review_card(self):
        import sqlite3,time
        now=time.time()
        with sqlite3.connect(self.policy.db_path) as db:
            for request_id,device_id in (('actual','device'),('sample','cloud-smoke')):
                db.execute('''INSERT INTO learning(request_id,time,device_id,asr_raw,english,playback_reported)
                              VALUES(?,?,?,?,?,1)''',
                           (request_id,now,device_id,'我想喝水','I want some water.'))
        set_candidate_decision(self.policy.db_path,'actual','select',now=now)
        report=learning_report(self.policy.db_path,now=now)
        self.assertEqual([row['request_id'] for row in report['cards']],['actual'])
        self.assertEqual(report['due_count'],1)
        self.assertIsNone(mark_reviewed(self.policy.db_path,'sample',now=now))
        self.assertIsNone(mark_practiced(self.policy.db_path,'sample',now=now))
        self.assertIsNone(set_candidate_decision(self.policy.db_path,'sample','select',now=now))

    def test_dismissed_candidate_stays_out_of_review(self):
        import sqlite3,time
        now=time.time()
        with sqlite3.connect(self.policy.db_path) as db:
            db.execute('''INSERT INTO learning(request_id,time,device_id,asr_raw,english,playback_reported)
                          VALUES(?,?,?,?,?,1)''',('private',now,'device','家庭里的话','Some words.'))
        self.assertEqual(set_candidate_decision(self.policy.db_path,'private','dismiss',now=now)['status'],'dismissed')
        self.assertEqual(set_candidate_decision(self.policy.db_path,'private','select',now=now)['status'],'already_decided')
        listing=learning_report(self.policy.db_path,now=now)
        self.assertEqual((listing['cards'],listing['candidates']),([],[]))
        self.assertIsNone(mark_reviewed(self.policy.db_path,'private',now=now))

    def test_expired_speech_text_is_pruned_without_a_new_turn(self):
        import sqlite3,time
        now=time.time()
        old=now-7*86400-1
        with sqlite3.connect(self.policy.db_path) as db:
            db.execute('INSERT INTO events VALUES(?,?,?,?,?)',
                       (old,'old','device','asr_result',json.dumps({'text':'expired speech'})))
            db.execute('''INSERT INTO learning(request_id,time,device_id,asr_raw,english)
                          VALUES(?,?,?,?,?)''',('old',old,'device','expired speech','Expired speech.'))
            db.execute('INSERT INTO billing_turns(request_id,time,reserved_cny,status) VALUES(?,?,?,?)',
                       ('old',old,0.01,'success'))
        self.policy.prune_expired(now=now)
        with sqlite3.connect(self.policy.db_path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events WHERE request_id="old"').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM learning WHERE request_id="old"').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM billing_turns WHERE request_id="old"').fetchone()[0],1)

    def test_migration_keeps_cards_that_were_already_reviewed(self):
        import sqlite3,time
        legacy=self.folder/'engaged';legacy.mkdir()
        now=time.time()
        with sqlite3.connect(legacy/'usage.sqlite') as db:
            db.execute('''CREATE TABLE learning(request_id TEXT PRIMARY KEY,time REAL,device_id TEXT,
                          asr_raw TEXT,english TEXT,playback_reported INTEGER DEFAULT 0,
                          review_count INTEGER NOT NULL DEFAULT 0,practice_count INTEGER NOT NULL DEFAULT 0)''')
            db.execute('INSERT INTO learning VALUES(?,?,?,?,?,?,?,?)',
                       ('used',now,'device','我想喝水','I want some water.',1,1,0))
            db.execute('INSERT INTO learning VALUES(?,?,?,?,?,?,?,?)',
                       ('unused',now,'device','我要出门','I want to go out.',1,0,0))
        migrated=CloudPolicy(legacy,self.registry)
        listing=learning_report(migrated.db_path,now=now)
        self.assertEqual([row['request_id'] for row in listing['cards']],['used'])
        self.assertEqual([row['request_id'] for row in listing['candidates']],['unused'])

    async def test_review_page_auth_and_cross_site_write_guard(self):
        from debug_page import create_app
        from aiohttp.test_utils import TestClient, TestServer
        import base64
        admin=self.folder/'admin.json'
        admin.write_text(json.dumps({'password_sha256':hashlib.sha256(b'admin-secret').hexdigest()}))
        admin.chmod(0o600)
        for row in ({'event':'asr_result','text':'妈妈，我想喝水。'},
                    {'event':'english_result','text':'Mom, I want to drink water.'},
                    {'event':'playback_started'}, {'event':'playback_confirmed'}):
            self.policy.record(self.conn,{'request_id':'r',**row})
            self.policy.record(self.conn,{'request_id':'r2',**row,
                **({'text':'Mom, I want some juice.'} if row['event']=='english_result' else {})})
        async with TestClient(TestServer(create_app(self.folder/'none',billing_db=self.policy.db_path,admin_file=admin))) as client:
            self.assertEqual((await client.get('/api/learning')).status,401)
            response=await client.get('/learning',allow_redirects=False)
            self.assertEqual(response.status,303)
            self.assertEqual(response.headers['Location'],'/login?next=/learning')
            auth={'Authorization':'Basic '+base64.b64encode(b'admin:admin-secret').decode()}
            page=await client.get('/learning',headers=auth)
            self.assertEqual(page.status,200)
            self.assertIn('学习复习',await page.text())
            response=await client.get('/api/learning',headers=auth)
            self.assertEqual(response.status,200)
            listing=await response.json()
            self.assertEqual(listing['cards'],[])
            self.assertEqual(len(listing['candidates']),2)
            select='/api/learning/r/select'
            self.assertEqual((await client.post(select,headers=auth)).status,403)
            self.assertEqual((await client.post(select,headers={**auth,'X-English-Edge-Action':'select',
                'Origin':'https://evil.example'})).status,403)
            self.assertEqual((await client.post(select,headers={**auth,'X-English-Edge-Action':'select'})).status,200)
            self.assertEqual((await client.post(select,headers={**auth,'X-English-Edge-Action':'select'})).status,409)
            self.assertEqual((await client.post('/api/learning/r2/select',headers={**auth,
                'X-English-Edge-Action':'select'})).status,200)
            selected_cards=(await (await client.get('/api/learning',headers=auth)).json())['cards']
            self.assertEqual(next(card['english'] for card in selected_cards if card['request_id']=='r'),
                             'Mom, I want to drink water.')
            url='/api/learning/r/review'
            self.assertEqual((await client.post(url,headers=auth)).status,403)
            self.assertEqual((await client.post(url,headers={**auth,'X-English-Edge-Action':'review',
                'Origin':'https://evil.example'})).status,403)
            response=await client.post(url,headers={**auth,'X-English-Edge-Action':'review'})
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['review_count'],1)
            self.assertEqual((await client.post(url,headers={**auth,'X-English-Edge-Action':'review'})).status,409)
            outcome_url='/api/learning/r2/review'
            outcome_headers={**auth,'X-English-Edge-Action':'review','Content-Type':'application/json'}
            self.assertEqual((await client.post(outcome_url,headers=outcome_headers,
                json={'outcome':'mastered'})).status,400)
            response=await client.post(outcome_url,headers=outcome_headers,json={'outcome':'independent'})
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['independent_count'],1)
            practice='/api/learning/r/practice'
            action={**auth,'X-English-Edge-Action':'practice'}
            self.assertEqual((await client.post(practice,headers=auth)).status,403)
            self.assertEqual((await client.post(practice,headers={**action,'Origin':'https://evil.example'})).status,403)
            self.assertEqual((await client.post(practice,headers=action,data=b'audio bytes')).status,400)
            response=await client.post(practice,headers=action)
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['practice_count'],1)
            self.assertEqual((await client.post(practice,headers=action)).status,409)
            cards=(await (await client.get('/api/learning',headers=auth)).json())['cards']
            self.assertEqual(next(card for card in cards if card['request_id']=='r')['practice_count'],1)
            assisted=await client.get('/api/learning?view=needs_help',headers=auth)
            self.assertEqual(assisted.status,200)
            filtered=await assisted.json()
            self.assertEqual(filtered['view'],'needs_help')
            self.assertTrue(all(card['last_review_outcome']=='assisted' for card in filtered['cards']))
            self.assertEqual((await client.get('/api/learning?view=mastered',headers=auth)).status,400)
            self.assertEqual((await client.get('/api/learning?view=due&view=all',headers=auth)).status,400)
            self.assertEqual((await client.get('/api/learning?view=due')).status,401)
            for row in ({'event':'asr_result','text':'只供测试'},
                        {'event':'english_result','text':'For a local test.'},
                        {'event':'playback_confirmed'}):
                self.policy.record(self.conn,{'request_id':'r3',**row})
            dismiss='/api/learning/r3/dismiss'
            self.assertEqual((await client.post(dismiss,headers={**auth,
                'X-English-Edge-Action':'dismiss'})).status,200)
            self.assertEqual((await client.post('/api/learning/r3/select',headers={**auth,
                'X-English-Edge-Action':'select'})).status,409)
            listing=await (await client.get('/api/learning',headers=auth)).json()
            self.assertNotIn('r3',[row['request_id'] for row in listing['candidates']])

    async def test_review_api_support_contract_and_legacy_request_compatibility(self):
        from debug_page import create_app
        from aiohttp.test_utils import TestClient, TestServer
        import base64,sqlite3,time
        now=time.time()
        admin=self.folder/'admin.json'
        admin.write_text(json.dumps({'password_sha256':hashlib.sha256(b'admin-secret').hexdigest()}))
        admin.chmod(0o600)
        cases=[('hint',{'outcome':'assisted','support':'hint'},0,1,1),
               ('answer',{'outcome':'assisted','support':'answer'},0,1,1),
               ('none',{'outcome':'independent','support':'none'},1,0,2),
               ('legacy-independent',{'outcome':'independent'},1,0,2),
               ('legacy-assisted',{'outcome':'assisted'},0,1,1),
               ('legacy-viewed',None,0,0,1)]
        with sqlite3.connect(self.policy.db_path) as db:
            db.executemany('''INSERT INTO learning(request_id,time,device_id,asr_raw,english,
                              playback_reported,review_selected) VALUES(?,?,?,?,?,1,1)''',
                           [(rid,now,'device','我想喝水',f'I want {index} cups of water.')
                            for index,(rid,*_) in enumerate(cases)])
        async with TestClient(TestServer(create_app(self.folder/'none',billing_db=self.policy.db_path,admin_file=admin))) as client:
            headers={'Authorization':'Basic '+base64.b64encode(b'admin:admin-secret').decode(),
                     'X-English-Edge-Action':'review'}
            url='/api/learning/hint/review'
            invalid=[[],{}, {'support':'hint'}, {'outcome':'viewed','support':'none'},
                     {'outcome':'independent','support':'hint'}, {'outcome':'independent','support':'answer'},
                     {'outcome':'assisted','support':'hint','extra':True},
                     *[{'outcome':'assisted','support':support}
                       for support in ('unknown','',None,0,True,[],{})]]
            for body in invalid:
                with self.subTest(invalid=body):
                    response=await client.post(url,headers=headers,json=body)
                    self.assertEqual(response.status,400)
            untouched=next(card for card in learning_report(self.policy.db_path,now=now)['cards']
                           if card['request_id']=='hint')
            self.assertEqual((untouched['review_count'],untouched['independent_count'],
                              untouched['assisted_count']),(0,0,0))
            self.assertIsNone(untouched['next_review_at'])
            for rid,body,independent_count,assisted_count,delay_days in cases:
                with self.subTest(valid=rid):
                    url=f'/api/learning/{rid}/review'
                    before=time.time()
                    response=await client.post(url,headers=headers,**({'json':body} if body is not None else {}))
                    after=time.time()
                    self.assertEqual(response.status,200)
                    result=await response.json()
                    self.assertEqual((result['review_count'],result['independent_count'],result['assisted_count']),
                                     (1,independent_count,assisted_count))
                    self.assertEqual(result['last_review_outcome'],body['outcome'] if body else 'viewed')
                    self.assertGreaterEqual(result['next_review_at'],before+delay_days*86400)
                    self.assertLessEqual(result['next_review_at'],after+delay_days*86400)
                    retry=await client.post(url,headers=headers,**({'json':body} if body is not None else {}))
                    self.assertEqual(retry.status,409)

    async def test_public_admin_host_uses_review_as_root_and_shared_navigation(self):
        from debug_page import create_app
        from aiohttp.test_utils import TestClient, TestServer
        import base64
        admin=self.folder/'admin.json'
        admin.write_text(json.dumps({'password_sha256':hashlib.sha256(b'admin-secret').hexdigest()}))
        admin.chmod(0o600)
        for row in ({'event':'asr_result','text':'妈妈，我想喝水。'},
                    {'event':'english_result','text':'Mom, I want to drink water.'},
                    {'event':'playback_started'}, {'event':'playback_confirmed'}):
            self.policy.record(self.conn,{'request_id':'public1',**row})
        self.policy.record(self.conn,{'request_id':'public1','event':'error','error':'PRIVATE SECRET'})
        import sqlite3,time
        with sqlite3.connect(self.policy.db_path) as db:
            db.execute('INSERT INTO events VALUES(?,?,?,?,?)',
                       (time.time()-8*86400,'old','device','asr_result',json.dumps({'text':'EXPIRED PRIVATE TEXT'})))
            db.execute('INSERT INTO events VALUES(?,?,?,?,?)',
                       (time.time(),'diagnostic','cloud-smoke','error',json.dumps({'error':'DIAGNOSTIC ONLY'})))
        with patch.dict(os.environ,{'ADMIN_PUBLIC_ORIGIN':'https://english.example.com'}):
            app=create_app(self.folder/'none',billing_db=self.policy.db_path,admin_file=admin)
        async with TestClient(TestServer(app)) as client:
            host={'Host':'english.example.com'}
            auth={'Authorization':'Basic '+base64.b64encode(b'admin:admin-secret').decode()}
            response=await client.get('/',headers=host,allow_redirects=False)
            self.assertEqual(response.status,303)
            self.assertEqual(response.headers['Location'],'/login?next=/')
            self.assertEqual((await client.get('/login',headers=host)).status,200)
            for path in ('/','/learning','/monitor','/costs','/api/learning','/api/costs'):
                self.assertEqual((await client.get(path,headers={**host,**auth})).status,200,path)
            root=await (await client.get('/',headers={**host,**auth})).text()
            learning=await (await client.get('/learning',headers={**host,**auth})).text()
            costs=await (await client.get('/costs',headers={**host,**auth})).text()
            self.assertEqual(root,learning)
            self.assertIn('href="/admin.css"',root)
            self.assertIn('href="/admin.css"',costs)
            self.assertIn('href="/costs"',root)
            self.assertIn('href="/"',costs)
            self.assertIn('href="/monitor"',root+costs)
            self.assertNotIn('>首页<',root+costs)
            self.assertEqual((await client.get('/admin.css',headers={**host,**auth})).status,200)
            self.assertEqual((await client.get('/api/requests',headers=host)).status,401)
            events=await (await client.get('/api/requests',headers={**host,**auth})).json()
            self.assertIn('处理失败',events['requests'][0]['error'])
            self.assertEqual(events['requests'][0]['asr'],'妈妈，我想喝水。')
            self.assertEqual(events['requests'][0]['english'],'Mom, I want to drink water.')
            self.assertNotIn('PRIVATE SECRET',json.dumps(events))
            self.assertNotIn('EXPIRED PRIVATE TEXT',json.dumps(events))
            self.assertNotIn('DIAGNOSTIC ONLY',json.dumps(events))
            self.assertEqual((await client.get('/api/requests',headers={**auth,'Host':'evil.example'})).status,403)
            self.assertEqual((await client.get('/learning',headers={**auth,'Host':'evil.example'})).status,403)
            selected={**host,**auth,'X-English-Edge-Action':'select',
                      'Origin':'https://english.example.com'}
            self.assertEqual((await client.post('/api/learning/public1/select',headers=selected)).status,200)
            url='/api/learning/public1/review'
            headers={**host,**auth,'X-English-Edge-Action':'review'}
            self.assertEqual((await client.post(url,headers=headers)).status,403)
            self.assertEqual((await client.post(url,headers={**headers,'Origin':'https://evil.example'})).status,403)
            self.assertEqual((await client.post(url,headers={**headers,'Origin':'https://english.example.com'})).status,200)
            practice='/api/learning/public1/practice'
            practice_headers={**host,**auth,'X-English-Edge-Action':'practice'}
            self.assertEqual((await client.post(practice,headers=practice_headers)).status,403)
            self.assertEqual((await client.post(practice,headers={**practice_headers,'Origin':'https://english.example.com'})).status,200)

    async def test_login_page_cookie_and_password_rotation(self):
        from debug_page import create_app
        from aiohttp.test_utils import TestClient, TestServer
        admin=self.folder/'admin.json'
        admin.write_text(json.dumps({'password_sha256':hashlib.sha256(b'admin-secret').hexdigest()}))
        app=create_app(self.folder/'none',billing_db=self.policy.db_path,admin_file=admin)
        async with TestClient(TestServer(app)) as client:
            page=await client.get('/login?next=/learning')
            self.assertEqual(page.status,200)
            self.assertIn('请输入管理口令',await page.text())
            self.assertNotIn('WWW-Authenticate',page.headers)
            self.assertEqual(page.headers['Referrer-Policy'],'same-origin')
            bad=await client.post('/login',data={'password':'wrong','next':'/learning'})
            self.assertEqual(bad.status,401)
            self.assertIn('口令不正确',await bad.text())
            self.assertNotIn('WWW-Authenticate',bad.headers)
            blocked=await client.post('/login',data={'password':'admin-secret'},headers={'Origin':'https://evil.example'})
            self.assertEqual(blocked.status,403)
            good=await client.post('/login',data={'password':'admin-secret','next':'/learning'},allow_redirects=False)
            self.assertEqual(good.status,303)
            self.assertEqual(good.headers['Location'],'/learning')
            self.assertIn('HttpOnly',good.headers['Set-Cookie'])
            self.assertIn('SameSite=Strict',good.headers['Set-Cookie'])
            cookie=good.cookies['english_edge_session'].value
            self.assertEqual((await client.get('/learning',headers={'Cookie':'english_edge_session='+cookie})).status,200)
            self.assertEqual((await client.get('/api/costs',headers={'Cookie':'english_edge_session='+cookie})).status,200)
            client.session.cookie_jar.clear()
            self.assertEqual((await client.get('/api/costs',headers={'Cookie':'english_edge_session='+cookie+'x'})).status,401)
            admin.write_text(json.dumps({'password_sha256':hashlib.sha256(b'changed').hexdigest()}))
            self.assertEqual((await client.get('/api/costs',headers={'Cookie':'english_edge_session='+cookie})).status,401)

    async def test_handshake_auth_happens_before_handler(self):
        server=WebSocketServer(load_cloud_config(require_credentials=False))
        attach_policy(server,self.policy)
        async with websockets.serve(server._handle_connection,'127.0.0.1',0,
                                    process_request=server._http_response) as listener:
            url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/xiaozhi/v1/'
            for headers in ({},{'Device-Id':'device','Authorization':'Bearer wrong'}):
                with self.assertRaises(websockets.exceptions.InvalidStatus) as caught:
                    async with websockets.connect(url,additional_headers=headers):pass
                self.assertEqual(caught.exception.response.status_code,401)
            self.assertEqual(len(self.policy.active_devices),0)
        server._llm.close()

    def test_stage_billing_keeps_partial_failure_and_survives_restart(self):
        env=dict(ASR_CNY_PER_HOUR='1',LLM_INPUT_CNY_PER_MILLION='.2',LLM_OUTPUT_CNY_PER_MILLION='2',
                 TTS_CNY_PER_10K_CHARS='3',DAILY_CNY_LIMIT='1',DAILY_TURN_LIMIT='10')
        with patch.dict(os.environ,env):
            live=CloudPolicy(self.folder,self.registry,live=True)
            live.before_turn(self.conn,5)
            for row in ({'event':'asr_started'}, {'event':'asr_usage','input_audio_seconds':5},
                        {'event':'english_started'}, {'event':'llm_usage','input_tokens':400,'output_tokens':60},
                        {'event':'tts_started'}, {'event':'request_complete','success':False}):
                live.record(self.conn,{'request_id':'r',**row})
            bill=billing_report(live.db_path)
            self.assertAlmostEqual(bill['stages']['asr']['estimated_cny'],5/3600)
            self.assertAlmostEqual(bill['stages']['english']['estimated_cny'],.0002)
            self.assertEqual(bill['stages']['tts']['uncertain_calls'],1)
            self.assertIsNone(bill['supplier_billed_cny'])
            CloudPolicy(self.folder,self.registry,live=True)
            self.assertEqual(billing_report(live.db_path),bill)

    async def test_billing_page_is_private_and_has_no_transcript(self):
        from debug_page import create_app
        from aiohttp.test_utils import TestClient, TestServer
        import base64
        admin=self.folder/'admin.json'
        admin.write_text(json.dumps({'password_sha256':hashlib.sha256(b'admin-secret').hexdigest()}))
        admin.chmod(0o600)
        self.policy.record(self.conn,{'request_id':'r','event':'asr_result','text':'PRIVATE CHILD TEXT'})
        async with TestClient(TestServer(create_app(self.folder/'none',billing_db=self.policy.db_path,admin_file=admin))) as client:
            r=await client.get('/api/costs');self.assertEqual(r.status,401)
            headers={'Authorization':'Basic '+base64.b64encode(b'admin:admin-secret').decode()}
            r=await client.get('/api/costs',headers=headers)
            self.assertEqual(r.status,200)
            text=await r.text();self.assertNotIn('PRIVATE CHILD TEXT',text);self.assertNotIn('admin-secret',text)
            self.assertEqual(r.headers['Cache-Control'],'no-store')
            r=await client.get('/costs',headers=headers);self.assertEqual(r.status,200)
            r=await client.get('/api/costs',headers={**headers,'Host':'evil.example'})
            self.assertEqual(r.status,403)

    def test_ecs_bill_keeps_actual_yearly_charge_without_account_identity(self):
        from sync_ecs_bill import summarize
        result=summarize([{'InstanceID':'ecs-test','Currency':'CNY','PretaxAmount':99,
            'BillAccountName':'PRIVATE ACCOUNT','ServicePeriod':'1','ServicePeriodUnit':'年',
            'SubscriptionType':'Subscription','ProductDetail':'云服务器ECS-包年包月'}],'ecs-test','2026-10')
        self.assertEqual(result['amount_due_cny'],99)
        self.assertEqual(result['items'][0]['period_unit'],'年')
        self.assertNotIn('PRIVATE ACCOUNT',json.dumps(result))
        with self.assertRaises(ValueError):
            summarize([{'InstanceID':'other-project','Currency':'CNY','PretaxAmount':99}],'ecs-test','2026-10')

    def test_diagnostics_redacts_content_and_keeps_missing_timings_unknown(self):
        for row in ({'event':'asr_result','text':'PRIVATE CHILD TEXT','asr_ms':20},
                    {'event':'error','error':'RuntimeError: seed_tts_failed: PRIVATE SECRET'},
                    {'event':'request_complete','success':False,'asr_ms':20,'tts_ms':None,'total_ms':float('nan')},
                    {'event':'playback_started','stop_to_playback_ack_ms':500},
                    {'event':'playback_confirmed'}):
            self.policy.record(self.conn,{'request_id':'r',**row})
        result=diagnostics_report(self.policy.db_path)
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertEqual(result['failed'],1)
        self.assertEqual(result['timings']['asr_ms'],{'samples':1,'mean_ms':20,'p95_ms':20})
        self.assertIsNone(result['timings']['total_ms']['mean_ms'])
        self.assertEqual(result['timings']['tts_ms']['samples'],0)
        self.assertEqual(result['recent'][0]['error_code'],'tts_failed')
        self.assertTrue(result['recent'][0]['playback_reported'])
        self.assertEqual(result['timings']['stop_to_playback_ack_ms']['mean_ms'],500)

    def test_diagnostics_window_and_runtime_budget_do_not_reserve(self):
        for i in range(120):
            self.policy.record(self.conn,{'request_id':str(i),'event':'request_complete','success':True,'asr_ms':i})
        result=diagnostics_report(self.policy.db_path)
        self.assertEqual(result['requests_in_window'],100)
        self.assertEqual(len(result['recent']),20)
        self.assertEqual(result['timings']['asr_ms']['mean_ms'],69.5)
        self.assertEqual(result['timings']['asr_ms']['p95_ms'],114)
        env=dict(ASR_CNY_PER_HOUR='1',LLM_INPUT_CNY_PER_MILLION='.2',LLM_OUTPUT_CNY_PER_MILLION='2',
                 TTS_CNY_PER_10K_CHARS='3',DAILY_CNY_LIMIT='1',DAILY_TURN_LIMIT='10',ARK_API_KEY='PRIVATE KEY')
        with patch.dict(os.environ,env):
            live=CloudPolicy(self.folder,self.registry,live=True)
            reservation=live.before_turn(self.conn,5)
            first=live.runtime_status();second=live.runtime_status()
        self.assertEqual(first['budget'],second['budget'])
        self.assertEqual(first['budget']['remaining_turns'],9)
        self.assertAlmostEqual(first['budget']['remaining_cny'],1-reservation['reserved_cny'])
        self.assertNotIn('PRIVATE KEY',json.dumps(first))
        self.assertIsNone(first['vendor_verified'])
        self.assertIsNone(self.policy.runtime_status()['budget']['remaining_cny'])

    async def test_diagnostics_uses_admin_auth_and_runtime_callback(self):
        from debug_page import create_app
        from aiohttp.test_utils import TestClient,TestServer
        import base64
        admin=self.folder/'admin.json'
        admin.write_text(json.dumps({'password_sha256':hashlib.sha256(b'admin-secret').hexdigest()}))
        async with TestClient(TestServer(create_app(self.folder/'none',billing_db=self.policy.db_path,
                admin_file=admin,runtime_status=self.policy.runtime_status))) as client:
            r=await client.get('/api/diagnostics');self.assertEqual(r.status,401)
            headers={'Authorization':'Basic '+base64.b64encode(b'admin:admin-secret').decode()}
            r=await client.get('/api/diagnostics',headers=headers)
            self.assertEqual(r.status,200);self.assertEqual(r.headers['Cache-Control'],'no-store')
            result=await r.json();self.assertEqual(result['runtime']['mode'],'STANDBY')
            self.assertEqual(result['completed'],0)


if __name__=='__main__':unittest.main()
