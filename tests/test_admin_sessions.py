"""Persistent admin sessions; isolated files and loopback HTTP, no cloud calls."""
import base64
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import DummyCookieJar
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from debug_page import (SESSION_COOKIE, SESSION_KEY_FILENAME, SESSION_LIFETIME,
                        SESSION_RENEW_AFTER, create_app, load_session_key)


class AdminSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.admin = self.folder / 'admin.json'
        self.password = 'local-test-password-only'
        self.set_password(self.password)
        self.origin = 'https://englishlearning.example'
        self.host = {'Host': 'englishlearning.example'}
        environment = patch.dict(os.environ, {'ADMIN_PUBLIC_ORIGIN': self.origin})
        environment.start()
        self.addCleanup(environment.stop)

    def set_password(self, password):
        self.admin.write_text(json.dumps({
            'password_sha256': hashlib.sha256(password.encode()).hexdigest()}))

    def client(self):
        app = create_app(self.folder / 'events.jsonl',
                         billing_db=self.folder / 'billing.sqlite3', admin_file=self.admin)
        return TestClient(TestServer(app), cookie_jar=DummyCookieJar())

    def headers(self, cookie):
        return {**self.host, 'Cookie': SESSION_COOKIE + '=' + cookie}

    async def login(self, client):
        response = await client.post('/login', headers={**self.host, 'Origin': self.origin},
                                     data={'password': self.password, 'next': '/monitor'},
                                     allow_redirects=False)
        self.assertEqual(response.status, 303)
        return response

    async def test_restart_preserves_session_and_key_is_private(self):
        async with self.client() as first:
            response = await self.login(first)
            cookie = response.cookies[SESSION_COOKIE].value
        key_path = self.folder / SESSION_KEY_FILENAME
        original_key = key_path.read_bytes()
        self.assertEqual(len(original_key), 32)
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        self.assertEqual(list(self.folder.glob('.admin-session-*')), [])
        async with self.client() as restarted:
            page = await restarted.get('/monitor', headers=self.headers(cookie), allow_redirects=False)
            self.assertEqual(page.status, 200)
            self.assertEqual(key_path.read_bytes(), original_key)
            self.assertNotIn('Set-Cookie', page.headers)
            direct_login = await restarted.get('/login?next=/costs', headers=self.headers(cookie),
                                              allow_redirects=False)
            self.assertEqual(direct_login.status, 303)
            self.assertEqual(direct_login.headers['Location'], '/costs')
            self.assertNotIn('Set-Cookie', direct_login.headers)

    async def test_cookie_is_long_lived_secure_and_contains_no_password_or_key(self):
        async with self.client() as client:
            response = await self.login(client)
            cookie = response.cookies[SESSION_COOKIE]
            self.assertEqual(int(cookie['max-age']), 400 * 86400)
            self.assertTrue(cookie['secure'])
            self.assertTrue(cookie['httponly'])
            self.assertEqual(cookie['samesite'], 'Strict')
            self.assertEqual(cookie['path'], '/')
            exposed = str(response.headers) + await response.text()
            for secret in (self.password, hashlib.sha256(self.password.encode()).hexdigest(),
                           (self.folder / SESSION_KEY_FILENAME).read_bytes().hex()):
                self.assertNotIn(secret, exposed)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_active_sessions_renew_daily_and_survive_original_expiry(self):
        started = 1_800_000_000
        async with self.client() as client:
            with patch('debug_page.time.time', return_value=started):
                response = await self.login(client)
                cookie = response.cookies[SESSION_COOKIE].value
            with patch('debug_page.time.time', return_value=started + SESSION_RENEW_AFTER - 1):
                response = await client.get('/monitor', headers=self.headers(cookie))
                self.assertNotIn('Set-Cookie', response.headers)
            with patch('debug_page.time.time', return_value=started + SESSION_RENEW_AFTER):
                response = await client.get('/monitor', headers=self.headers(cookie))
                renewed = response.cookies[SESSION_COOKIE].value
                self.assertNotEqual(renewed, cookie)
                self.assertEqual(int(renewed.split('.')[0]), started + SESSION_RENEW_AFTER)
            with patch('debug_page.time.time', return_value=started + SESSION_LIFETIME):
                expired = await client.get('/monitor.js', headers=self.headers(cookie))
                self.assertEqual(expired.status, 401)
                fresh = await client.get('/monitor.js', headers=self.headers(renewed))
                self.assertEqual(fresh.status, 200)

    async def test_tampered_malformed_expired_and_future_cookies_are_rejected(self):
        started = 1_800_000_000
        async with self.client() as client:
            with patch('debug_page.time.time', return_value=started):
                response = await self.login(client)
                cookie = response.cookies[SESSION_COOKIE].value
                stamp, signature = cookie.split('.')
                for invalid in ('junk', cookie + 'x', '0' + cookie,
                                str(int(stamp) + 1) + '.' + signature,
                                stamp + '.' + ('0' if signature[0] != '0' else '1') + signature[1:]):
                    with self.subTest(invalid=invalid[:12]):
                        response = await client.get('/monitor.js', headers=self.headers(invalid))
                        self.assertEqual(response.status, 401)
                        self.assertNotIn('Set-Cookie', response.headers)
            for clock in (started - 1, started + SESSION_LIFETIME):
                with patch('debug_page.time.time', return_value=clock):
                    response = await client.get('/monitor.js', headers=self.headers(cookie))
                    self.assertEqual(response.status, 401)

    async def test_password_rotation_revokes_cookie_and_basic_auth_still_works(self):
        async with self.client() as client:
            cookie = (await self.login(client)).cookies[SESSION_COOKIE].value
            self.set_password('local-new-password')
            response = await client.get('/monitor.js', headers=self.headers(cookie))
            self.assertEqual(response.status, 401)
            basic = base64.b64encode(b'admin:local-new-password').decode()
            response = await client.get('/monitor.js', headers={**self.host, 'Authorization': 'Basic ' + basic})
            self.assertEqual(response.status, 200)
            self.assertNotIn('Set-Cookie', response.headers)

    async def test_logout_is_authenticated_post_same_origin_and_clears_cookie(self):
        async with self.client() as client:
            cookie = (await self.login(client)).cookies[SESSION_COOKIE].value
            headers = self.headers(cookie)
            self.assertEqual((await client.post('/logout', headers=self.host, allow_redirects=False)).status, 401)
            self.assertEqual((await client.get('/logout', headers=headers, allow_redirects=False)).status, 405)
            for origin in (None, 'https://foreign.example'):
                rejected = await client.post('/logout', headers={**headers, **({'Origin': origin} if origin else {})},
                                             allow_redirects=False)
                self.assertEqual(rejected.status, 403)
            # Even a session due for renewal must not be renewed during logout.
            issued = int(cookie.split('.')[0])
            with patch('debug_page.time.time', return_value=issued + SESSION_RENEW_AFTER):
                response = await client.post('/logout', headers={**headers, 'Origin': self.origin},
                                             allow_redirects=False)
            self.assertEqual(response.status, 303)
            self.assertEqual(response.headers['Location'], '/login')
            cleared = response.cookies[SESSION_COOKIE]
            self.assertEqual(cleared.value, '')
            self.assertEqual(cleared['max-age'], '0')
            self.assertTrue(cleared['secure'])
            self.assertTrue(cleared['httponly'])
            self.assertEqual(cleared['samesite'], 'Strict')
            self.assertEqual((await client.get('/monitor.js', headers=self.host)).status, 401)

    async def test_login_redirect_is_local_and_failed_login_never_sets_cookie(self):
        async with self.client() as client:
            response = await client.post('/login', headers=self.host,
                                         data={'password': 'wrong'}, allow_redirects=False)
            self.assertEqual(response.status, 401)
            self.assertNotIn('Set-Cookie', response.headers)
            cookie = (await self.login(client)).cookies[SESSION_COOKIE].value
            response = await client.get('/login?next=https://foreign.example',
                                        headers=self.headers(cookie), allow_redirects=False)
            self.assertEqual(response.headers['Location'], '/')

    def test_key_creation_is_atomic_for_concurrent_starts(self):
        path = self.folder / SESSION_KEY_FILENAME
        with ThreadPoolExecutor(max_workers=8) as pool:
            keys = list(pool.map(lambda _: load_session_key(path), range(24)))
        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(keys[0], path.read_bytes())
        self.assertEqual(list(self.folder.glob('.admin-session-*')), [])

    def test_invalid_or_unreadable_key_fails_closed_without_regeneration(self):
        path = self.folder / SESSION_KEY_FILENAME
        for value, mode in ((b'', 0o600), (b'short-secret', 0o600), (b'x' * 33, 0o600),
                            (b'x' * 32, 0o644), (b'x' * 32, 0o000)):
            path.chmod(0o600) if path.exists() else None
            path.write_bytes(value)
            path.chmod(mode)
            with self.subTest(length=len(value), mode=mode):
                with self.assertRaisesRegex(RuntimeError, '^Admin session key unavailable or invalid$'):
                    self.client()
                path.chmod(0o600)
                self.assertEqual(path.read_bytes(), value)
        path.unlink()
        destination = self.folder / 'unrelated-private-file'
        destination.write_bytes(b'x' * 32)
        destination.chmod(0o600)
        path.symlink_to(destination)
        with self.assertRaises(RuntimeError):
            self.client()
        self.assertTrue(path.is_symlink())
        self.assertEqual(destination.read_bytes(), b'x' * 32)

    def test_unauthenticated_local_viewer_does_not_create_session_state(self):
        create_app(self.folder / 'events.jsonl')
        self.assertFalse((self.folder / SESSION_KEY_FILENAME).exists())


if __name__ == '__main__':
    unittest.main()
