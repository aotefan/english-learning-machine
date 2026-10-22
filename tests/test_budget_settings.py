"""Quota changes on temporary state/local HTTP; no provider or production calls."""
import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import tests.test_cloud_service as service
from aiohttp.test_utils import TestClient, TestServer
from cloud_budget import reserve, update_limits
from debug_page import create_app


class BudgetSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.folder = Path(self.temp.name)
        self.registry = self.folder/'devices.json'
        self.registry.write_text(json.dumps({'device':hashlib.sha256(b'fixture-token').hexdigest()}))
        self.registry.chmod(0o600)
        self.admin = self.folder/'admin.json'
        self.admin.write_text(json.dumps({'password_sha256':hashlib.sha256(b'fixture-password').hexdigest()}))
        self.admin.chmod(0o600)
        self.env = dict(ASR_CNY_PER_HOUR='1',LLM_INPUT_CNY_PER_MILLION='.2',
                       LLM_OUTPUT_CNY_PER_MILLION='2',TTS_CNY_PER_10K_CHARS='3',
                       DAILY_CNY_LIMIT='20',DAILY_TURN_LIMIT='200')
        self.enterContext(patch.dict(os.environ,self.env))
        self.policy = service.CloudPolicy(self.folder,self.registry,live=True)
        self.auth = {'Authorization':'Basic '+base64.b64encode(b'admin:fixture-password').decode()}

    def tearDown(self): self.temp.cleanup()

    def client(self, **kwargs):
        return TestClient(TestServer(create_app(self.folder/'events.jsonl',billing_db=self.policy.db_path,
            admin_file=self.admin,runtime_status=self.policy.runtime_status,update_budget=self.policy.set_budget,**kwargs)))

    def daily_rows(self):
        with sqlite3.connect(self.policy.db_path) as db:return db.execute('SELECT * FROM daily').fetchall()

    async def test_saved_raise_and_lower_persist_without_resetting_usage(self):
        reserve(self.policy.db_path,'.12','20',200)
        before=self.daily_rows()
        async with self.client() as client:
            headers={**self.auth,'Origin':str(client.make_url('/')).rstrip('/'),'X-English-Edge-Action':'budget'}
            response=await client.post('/api/budget',headers=headers,json={'turn_limit':1000,'limit_cny':'126'})
            self.assertEqual(response.status,200)
            data=await response.json();self.assertEqual(data['budget']['remaining_turns'],999)
            self.assertEqual(self.daily_rows(),before)
            self.assertNotIn('fixture-password',json.dumps(data))
            restarted=service.CloudPolicy(self.folder,self.registry,live=True)
            self.assertEqual(restarted.runtime_status()['budget']['turn_limit'],1000)
            self.assertEqual(restarted.runtime_status()['budget']['limit_cny'],126)
            # An already-created policy reserves against the new persisted cap.
            self.assertEqual(reserve(self.policy.db_path,'.12','20',200)['daily_turns'],2)
            response=await client.post('/api/budget',headers=headers,json={'turn_limit':1,'limit_cny':'126'})
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['budget']['remaining_turns'],0)
            with self.assertRaisesRegex(ValueError,'exhausted'):reserve(self.policy.db_path,'.12','20',200)
            self.assertEqual(len(self.daily_rows()),1)

    async def test_api_auth_origin_action_and_input_validation(self):
        async with self.client() as client:
            origin=str(client.make_url('/')).rstrip('/')
            good={**self.auth,'Origin':origin,'X-English-Edge-Action':'budget'}
            body={'turn_limit':1000,'limit_cny':'126'}
            self.assertEqual((await client.post('/api/budget',headers={'Origin':origin},json=body)).status,401)
            for bad in ({**self.auth,'Origin':origin},
                        {**good,'Origin':'https://evil.example'},
                        {**self.auth,'X-English-Edge-Action':'budget'}):
                self.assertEqual((await client.post('/api/budget',headers=bad,json=body)).status,403)
            for invalid in ({'turn_limit':True,'limit_cny':'126'}, {'turn_limit':1.5,'limit_cny':'126'},
                            {'turn_limit':0,'limit_cny':'126'}, {'turn_limit':1000,'limit_cny':'NaN'},
                            {'turn_limit':1000,'limit_cny':'-1'}, {'turn_limit':1000,'limit_cny':'1.001'},
                            {'turn_limit':1000,'limit_cny':126}, {'turn_limit':1000,'limit_cny':'126','reset':True}):
                self.assertEqual((await client.post('/api/budget',headers=good,json=invalid)).status,400)
            self.assertEqual((await client.post('/api/budget',headers=good,data='not json')).status,400)
            self.assertEqual(self.policy.runtime_status()['budget']['turn_limit'],200)

    async def test_public_host_requires_origin_and_publishes_cost_setting_ui(self):
        with patch.dict(os.environ,ADMIN_PUBLIC_ORIGIN='https://english.example.com'):
            async with self.client() as client:
                headers={**self.auth,'Host':'english.example.com','Origin':'https://english.example.com','X-English-Edge-Action':'budget'}
                self.assertEqual((await client.post('/api/budget',headers=headers,json={'turn_limit':1000,'limit_cny':'126'})).status,200)
                headers.pop('Origin')
                self.assertEqual((await client.post('/api/budget',headers=headers,json={'turn_limit':1,'limit_cny':'1'})).status,403)
                page=await client.get('/costs',headers=headers)
                self.assertIn('每日额度设置',await page.text())
                self.assertEqual(page.headers['Cache-Control'],'no-store')

    def test_concurrent_reservations_obey_saved_limit_in_same_transaction(self):
        update_limits(self.policy.db_path,'126',3)
        def spend(_):
            try:reserve(self.policy.db_path,'.1','20',200);return True
            except ValueError:return False
        with ThreadPoolExecutor(6) as pool:self.assertEqual(sum(pool.map(spend,range(12))),3)
        self.assertEqual(self.policy.runtime_status()['budget']['remaining_turns'],0)

    def test_cost_limit_can_pause_and_raise_without_changing_round_usage(self):
        reserve(self.policy.db_path,'.12','20',200)
        update_limits(self.policy.db_path,'.10',1000)
        with self.assertRaisesRegex(ValueError,'exhausted'):reserve(self.policy.db_path,'.12','20',200)
        update_limits(self.policy.db_path,'126',1000)
        self.assertEqual(reserve(self.policy.db_path,'.12','20',200)['daily_turns'],2)
        standby=service.CloudPolicy(self.folder,self.registry)
        with self.assertRaisesRegex(ValueError,'disabled'):standby.set_budget(1000,'126')
