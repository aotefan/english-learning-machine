"""Read-only local request viewer, using the backend's existing aiohttp dependency."""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import web

ASSETS = Path(__file__).resolve().parents[1] / 'debug'
MAX_BYTES = 1024 * 1024
SESSION_COOKIE = 'english_edge_session'
SESSION_LIFETIME = 400 * 24 * 3600
SESSION_RENEW_AFTER = 24 * 3600
SESSION_KEY_FILENAME = 'admin-session.key'
TIMINGS = ('audio_upload_ms', 'asr_ms', 'english_ms', 'tts_ms', 'return_ms', 'total_ms')
STAGES = {
    'recording_started': '收音中', 'audio_received': '识别中',
    'asr_result': '翻译中', 'english_result': '生成语音中',
    'tts_generated': '返回音频中', 'playback_started': '设备已回执',
    'audio_sent': '音频已发送', 'error': '失败',
}


def snapshot(path):
    """Bounded tail read: tolerate a concurrent append, missing file and rotation."""
    result = dict(requests=[], read_at=time.time(), last_event_at=None,
                  truncated=False, skipped_lines=0, log_status='ok')
    try:
        with Path(path).open('rb') as stream:
            size = stream.seek(0, 2)
            start = max(0, size - MAX_BYTES)
            result['truncated'] = bool(start)
            stream.seek(max(0, start - 1))
            if start:
                boundary = stream.read(1) == b'\n'
            else:
                boundary = True
            data = stream.read(MAX_BYTES)
            if not boundary:
                data = data.partition(b'\n')[2]
    except FileNotFoundError:
        result['log_status'] = 'missing'
        return result
    except OSError:
        result['log_status'] = 'unreadable'
        return result
    # An incomplete final line is retried on the next poll, never displayed.
    lines = data.split(b'\n')[:-1]
    rows = {}
    for line in lines:
        try:
            event = json.loads(line)
            stamp = event['time']
            if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                raise ValueError('invalid time')
            name = event['event']
            if not isinstance(name, str):
                raise ValueError('invalid event')
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            result['skipped_lines'] += 1
            continue
        result['last_event_at'] = max(result['last_event_at'] or stamp, stamp)
        rid = event.get('request_id')
        if not isinstance(rid, str) or not rid:
            continue
        row = rows.setdefault(rid, dict(request_id=rid, time=stamp, mode=None,
            stage='记录不完整', asr=None, english=None, error=None,
            playback_ack=False, success=None, timings={}))
        row['time'] = max(row['time'], stamp)
        if name == 'recording_started':
            row['mode'] = event.get('mode')
        if name in ('asr_result', 'english_result'):
            row['asr' if name == 'asr_result' else 'english'] = event.get('text')
        if name == 'error':
            row['error'] = event.get('error', 'unknown_error')
        if name == 'playback_started':
            row['playback_ack'] = True
        if row['success'] is None and not row['error'] and name in STAGES:
            row['stage'] = STAGES[name]
        if name == 'request_complete':
            row['success'] = event.get('success') is True
            row['stage'] = '链路完成' if row['success'] else '失败'
        if row['error']:
            row['stage'] = '失败'
        for key in TIMINGS:
            value = event.get(key)
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                row['timings'][key] = value
    result['requests'] = sorted(rows.values(), key=lambda r: r['time'], reverse=True)[:20]
    return result


def load_session_key(path):
    """Publish a complete private key once; never replace an existing key.

    The state directory survives release/container changes. A hard-link publish
    makes concurrent initial starts safe without exposing a partly written key.
    Invalid existing state fails closed rather than silently logging users out.
    """
    path = Path(path)
    temporary = None
    try:
        if not path.exists():
            fd, temporary = tempfile.mkstemp(prefix='.admin-session-', dir=path.parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
                directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except FileExistsError:
                pass  # Another process already published its complete key.
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid()):
                raise ValueError('Invalid key metadata')
            key = stream.read(33)
        if len(key) != 32:
            raise ValueError('Invalid key length')
        return key
    except (OSError, ValueError):
        raise RuntimeError('Admin session key unavailable or invalid') from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def create_app(log_path, *, billing_db=None, admin_file=None, runtime_status=None, update_budget=None,
               set_device_volume=None, device_status=None, set_device_mode=None):
    # The local read-only viewer has no authentication or persistent state.
    session_key = (load_session_key(Path(billing_db or admin_file).parent / SESSION_KEY_FILENAME)
                   if admin_file is not None else None)
    public_origin = os.getenv('ADMIN_PUBLIC_ORIGIN', '').rstrip('/')
    public_host = ''
    if public_origin:
        parsed = urlsplit(public_origin)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
            raise ValueError('ADMIN_PUBLIC_ORIGIN must be an HTTPS origin')
        public_host = parsed.netloc

    def public_admin_path(path):
        return path in ('/', '/login', '/logout', '/admin.css', '/learning', '/learning.js', '/monitor', '/monitor.js',
                        '/costs', '/costs.js', '/api/learning', '/api/requests', '/api/costs', '/api/diagnostics', '/api/budget',
                        '/api/device', '/api/device/volume', '/api/device/mode') or bool(
            re.fullmatch(r'/api/learning/[A-Za-z0-9]{1,64}/(?:review|practice|select|dismiss)', path))

    def safe_next(value):
        return value if value in ('/', '/learning', '/monitor', '/costs') else '/'

    def session_issued(request, password_hash):
        try:
            cookie = request.cookies[SESSION_COOKIE]
            if not re.fullmatch(r'[0-9]{1,12}\.[a-f0-9]{64}', cookie):
                return None
            stamp, signature = cookie.split('.', 1)
            issued = int(stamp)
            if not 0 <= time.time() - issued < SESSION_LIFETIME:
                return None
            expected = hmac.new(session_key,
                                ('english-edge-admin-session:' + stamp + ':' + password_hash).encode(), hashlib.sha256).hexdigest()
            return issued if hmac.compare_digest(signature, expected) else None
        except (KeyError, ValueError, TypeError):
            return None

    def set_session_cookie(response, request, password_hash):
        stamp = str(int(time.time()))
        signature = hmac.new(session_key,
            ('english-edge-admin-session:' + stamp + ':' + password_hash).encode(), hashlib.sha256).hexdigest()
        response.set_cookie(SESSION_COOKIE, stamp + '.' + signature,
                            max_age=SESSION_LIFETIME, httponly=True,
                            secure=bool(public_host and request.host == public_host) or request.secure,
                            samesite='Strict', path='/')

    @web.middleware
    async def local_only(request, handler):
        issued = None
        credentials = None
        local_host = request.host.split(':')[0] in ('127.0.0.1', 'localhost')
        public_admin = (billing_db is not None and admin_file is not None and public_host
                        and request.host == public_host and public_admin_path(request.path))
        if not local_host and not public_admin:
            raise web.HTTPForbidden()
        if admin_file is not None and not (request.path == '/login' and request.method in ('GET', 'POST')):
            try:
                credentials=json.loads(Path(admin_file).read_text())
                auth=request.headers.get('Authorization','')
                issued=session_issued(request, credentials['password_sha256'])
                valid=issued is not None
                if not valid and auth.startswith('Basic '):
                    username,password=base64.b64decode(auth[6:],validate=True).decode().split(':',1)
                    valid=username=='admin' and hmac.compare_digest(
                        hashlib.sha256(password.encode()).hexdigest(),credentials['password_sha256'])
            except Exception:valid=False
            if not valid:
                if request.method == 'GET' and request.path in ('/', '/learning', '/monitor', '/costs'):
                    raise web.HTTPSeeOther('/login?next=' + quote(request.path, safe=''))
                raise web.HTTPUnauthorized()
        raised_response = False
        try:
            response = await handler(request)
        except web.HTTPException as error:
            response = error
            raised_response = True
        if (issued is not None and request.path != '/logout' and response.status < 400
                and time.time() - issued >= SESSION_RENEW_AFTER):
            set_session_cookie(response, request, credentials['password_sha256'])
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; frame-ancestors 'none'",
            'Referrer-Policy': ('same-origin' if request.path in
                                ('/', '/login', '/learning', '/monitor', '/costs') else 'no-referrer'),
            'X-Robots-Tag': 'noindex, nofollow',
        })
        if raised_response:
            raise response
        return response

    async def index(request):
        filename='learning.html' if billing_db is not None else 'index.html'
        return web.Response(body=(ASSETS / filename).read_bytes(), content_type='text/html')

    async def admin_style(request):
        return web.Response(body=(ASSETS / 'admin.css').read_bytes(), content_type='text/css')

    async def script(request):
        return web.Response(body=(ASSETS / 'app.js').read_bytes(), content_type='application/javascript')

    async def requests(request):
        if billing_db is not None:
            from cloud_report import monitor_report
            data = await asyncio.to_thread(monitor_report, billing_db)
        else:
            data = await asyncio.to_thread(snapshot, log_path)
        return web.json_response(data)

    async def login_page(request):
        if admin_file is None:
            raise web.HTTPNotFound()
        next_path = safe_next(request.query.get('next', '/'))
        credentials = json.loads(Path(admin_file).read_text())
        issued = session_issued(request, credentials['password_sha256'])
        if issued is not None:
            response = web.HTTPSeeOther(next_path)
            if time.time() - issued >= SESSION_RENEW_AFTER:
                set_session_cookie(response, request, credentials['password_sha256'])
            raise response
        page = (ASSETS / 'login.html').read_text().replace('__NEXT__', next_path).replace('__ERROR__', '')
        response = web.Response(text=page, content_type='text/html')
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':"default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
            'Referrer-Policy':'same-origin','X-Robots-Tag':'noindex, nofollow'})
        return response

    async def login_submit(request):
        if admin_file is None:
            raise web.HTTPNotFound()
        expected = public_origin if request.host == public_host else f'{request.scheme}://{request.host}'
        if request.headers.get('Origin') not in (None, expected):
            raise web.HTTPForbidden()
        form = await request.post()
        password = form.get('password', '')
        credentials = json.loads(Path(admin_file).read_text())
        valid = isinstance(password, str) and hmac.compare_digest(
            hashlib.sha256(password.encode()).hexdigest(), credentials['password_sha256'])
        if not valid:
            page = (ASSETS / 'login.html').read_text().replace('__NEXT__', safe_next(form.get('next', '/'))).replace(
                '__ERROR__', '<p class="error" role="alert">口令不正确，请重试。</p>')
            response = web.Response(status=401, text=page, content_type='text/html')
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
            return response
        response = web.HTTPSeeOther(safe_next(form.get('next', '/')))
        set_session_cookie(response, request, credentials['password_sha256'])
        raise response

    async def logout(request):
        # POST form navigation sends Origin in browsers. Require it on the
        # public host so another site cannot log this browser out by submitting.
        expected = public_origin if request.host == public_host else f'{request.scheme}://{request.host}'
        origin = request.headers.get('Origin')
        if (request.host == public_host and origin != expected) or (origin is not None and origin != expected):
            raise web.HTTPForbidden()
        response = web.HTTPSeeOther('/login')
        response.del_cookie(SESSION_COOKIE, path='/', httponly=True,
                            secure=bool(public_host and request.host == public_host) or request.secure,
                            samesite='Strict')
        raise response

    app = web.Application(middlewares=[local_only], client_max_size=4096)
    app.add_routes([web.get('/', index), web.get('/app.js', script), web.get('/api/requests', requests)])
    if billing_db is not None:
        app.add_routes([web.get('/login', login_page), web.post('/login', login_submit), web.post('/logout', logout),
                        web.get('/admin.css', admin_style)])
        from cloud_report import billing_report, diagnostics_report
        from cloud_learning import learning_report, mark_reviewed, mark_practiced, set_candidate_decision
        async def diagnostics(request):
            data = await asyncio.to_thread(diagnostics_report, billing_db)
            data['runtime'] = runtime_status() if runtime_status else None
            return web.json_response(data)
        async def costs(request):
            return web.json_response(await asyncio.to_thread(billing_report,billing_db))
        async def budget_settings(request):
            # Require the existing admin auth plus explicit same-origin action.
            if admin_file is None or update_budget is None:
                raise web.HTTPForbidden()
            expected = public_origin if request.host == public_host else f'{request.scheme}://{request.host}'
            if (request.headers.get('X-English-Edge-Action') != 'budget' or
                    request.headers.get('Origin') != expected):
                raise web.HTTPForbidden()
            if request.content_type != 'application/json' or (request.content_length or 0) > 1024:
                raise web.HTTPBadRequest(text='Invalid budget settings')
            try:
                raw = await request.read()
                if len(raw) > 1024:
                    raise ValueError('invalid_budget_settings')
                body = json.loads(raw)
                if not isinstance(body, dict) or set(body) != {'turn_limit', 'limit_cny'}:
                    raise ValueError('invalid_budget_settings')
                # Decimal strings avoid floating-point rounding in money limits.
                if not isinstance(body['limit_cny'], str):
                    raise ValueError('invalid_budget_settings')
                result = await asyncio.to_thread(update_budget, body['turn_limit'], body['limit_cny'])
            except (ValueError, TypeError, ArithmeticError):
                raise web.HTTPBadRequest(text='Invalid budget settings') from None
            return web.json_response({'budget': result})
        async def device_volume(request):
            if admin_file is None or set_device_volume is None:
                raise web.HTTPForbidden()
            expected = public_origin if request.host == public_host else f'{request.scheme}://{request.host}'
            if (request.headers.get('X-English-Edge-Action') != 'volume'
                    or request.headers.get('Origin') != expected):
                raise web.HTTPForbidden()
            if request.content_type != 'application/json' or (request.content_length or 0) > 128:
                raise web.HTTPBadRequest(text='Invalid volume')
            try:
                raw = await request.read()
                if len(raw) > 128:
                    raise ValueError('invalid_volume')
                body = json.loads(raw)
                if (not isinstance(body, dict) or set(body) != {'volume'}
                        or type(body['volume']) is not int or not 0 <= body['volume'] <= 100):
                    raise ValueError('invalid_volume')
            except (ValueError, TypeError):
                raise web.HTTPBadRequest(text='Invalid volume') from None
            try:
                result = await set_device_volume(body['volume'])
            except (ValueError, asyncio.TimeoutError):
                raise web.HTTPConflict(text='Device volume could not be confirmed') from None
            return web.json_response(result)
        async def device_state(request):
            if admin_file is None or device_status is None:
                raise web.HTTPForbidden()
            return web.json_response(await device_status())
        async def device_mode(request):
            if admin_file is None or set_device_mode is None:
                raise web.HTTPForbidden()
            expected = public_origin if request.host == public_host else f'{request.scheme}://{request.host}'
            if (request.headers.get('X-English-Edge-Action') != 'mode'
                    or request.headers.get('Origin') != expected):
                raise web.HTTPForbidden()
            if request.content_type != 'application/json' or (request.content_length or 0) > 128:
                raise web.HTTPBadRequest(text='Invalid mode')
            try:
                raw = await request.read()
                if len(raw) > 128:raise ValueError('invalid_mode')
                body = json.loads(raw)
                if (not isinstance(body,dict) or set(body) != {'kind','mode'}
                        or body['kind'] not in ('teaching','listening')
                        or body['mode'] not in (('express','chat') if body['kind']=='teaching' else ('auto','manual'))):
                    raise ValueError('invalid_mode')
            except (ValueError, TypeError):
                raise web.HTTPBadRequest(text='Invalid mode') from None
            try:
                result = await set_device_mode(body['kind'],body['mode'])
            except (ValueError, asyncio.TimeoutError):
                raise web.HTTPConflict(text='Device mode could not be confirmed') from None
            return web.json_response(result)
        async def cost_page(request):
            return web.Response(body=(ASSETS/'costs.html').read_bytes(),content_type='text/html')
        async def cost_script(request):
            return web.Response(body=(ASSETS/'costs.js').read_bytes(),content_type='application/javascript')
        async def learning_page(request):
            return web.Response(body=(ASSETS/'learning.html').read_bytes(),content_type='text/html')
        async def learning_script(request):
            return web.Response(body=(ASSETS/'learning.js').read_bytes(),content_type='application/javascript')
        async def monitor_page(request):
            return web.Response(body=(ASSETS/'monitor.html').read_bytes(),content_type='text/html')
        async def monitor_script(request):
            return web.Response(body=(ASSETS/'monitor.js').read_bytes(),content_type='application/javascript')
        async def learning_cards(request):
            view = request.query.get('view', 'all')
            if view not in ('all', 'due', 'needs_help') or len(request.query.getall('view', [])) > 1:
                raise web.HTTPBadRequest(text='Invalid review view')
            return web.json_response(await asyncio.to_thread(learning_report,billing_db,view=view))
        async def review_card(request):
            # A custom header forces a browser preflight for cross-site POSTs.
            if request.headers.get('X-English-Edge-Action') != 'review':
                raise web.HTTPForbidden()
            origin = request.headers.get('Origin')
            expected_origin = public_origin if request.host == public_host else f'{request.scheme}://{request.host}'
            if (request.host == public_host and origin != expected_origin) or (
                origin is not None and origin != expected_origin):
                raise web.HTTPForbidden()
            outcome = 'viewed'
            support = 'none'
            if request.can_read_body:
                if request.content_type != 'application/json':
                    raise web.HTTPBadRequest(text='Expected JSON review outcome')
                try:
                    body = await request.json()
                except (ValueError, TypeError):
                    raise web.HTTPBadRequest(text='Invalid JSON review outcome')
                if not isinstance(body, dict) or 'outcome' not in body or (
                        set(body) - {'outcome', 'support'}) or (
                        body['outcome'] not in ('independent', 'assisted')):
                    raise web.HTTPBadRequest(text='Invalid review outcome')
                outcome = body['outcome']
                support = body.get('support', 'none')
            try:
                result = await asyncio.to_thread(mark_reviewed,billing_db,request.match_info['request_id'],
                                                outcome=outcome,support=support)
            except ValueError:
                raise web.HTTPBadRequest(text='Invalid review outcome or support')
            if result is None:
                raise web.HTTPNotFound()
            if result['status'] == 'not_due':
                return web.json_response(result,status=409)
            return web.json_response(result)
        async def practice_card(request):
            if request.headers.get('X-English-Edge-Action') != 'practice':
                raise web.HTTPForbidden()
            origin = request.headers.get('Origin')
            expected_origin = public_origin if request.host == public_host else f'{request.scheme}://{request.host}'
            if (request.host == public_host and origin != expected_origin) or (
                origin is not None and origin != expected_origin):
                raise web.HTTPForbidden()
            if request.content_length not in (None, 0) or request.can_read_body:
                raise web.HTTPBadRequest(text='Practice requests must not contain audio or other content')
            result = await asyncio.to_thread(mark_practiced,billing_db,request.match_info['request_id'])
            if result is None:
                raise web.HTTPNotFound()
            if result['status'] == 'too_soon':
                return web.json_response(result,status=409)
            return web.json_response(result)
        async def decide_candidate(request):
            decision = 'select' if request.path.endswith('/select') else 'dismiss'
            if request.headers.get('X-English-Edge-Action') != decision:
                raise web.HTTPForbidden()
            origin = request.headers.get('Origin')
            expected_origin = public_origin if request.host == public_host else f'{request.scheme}://{request.host}'
            if (request.host == public_host and origin != expected_origin) or (
                origin is not None and origin != expected_origin):
                raise web.HTTPForbidden()
            if request.content_length not in (None, 0) or request.can_read_body:
                raise web.HTTPBadRequest(text='Candidate decisions must not contain content')
            result = await asyncio.to_thread(set_candidate_decision,billing_db,
                                             request.match_info['request_id'],decision)
            if result is None:
                raise web.HTTPNotFound()
            if result['status'] == 'already_decided':
                return web.json_response(result,status=409)
            return web.json_response(result)
        app.add_routes([web.get('/costs',cost_page),web.get('/costs.js',cost_script),web.get('/api/costs',costs),
                        web.post('/api/budget',budget_settings),
                        web.post('/api/device/volume',device_volume),
                        web.get('/api/device',device_state), web.post('/api/device/mode',device_mode),
                        web.get('/api/diagnostics',diagnostics),web.get('/learning',learning_page),
                        web.get('/learning.js',learning_script),web.get('/monitor',monitor_page),
                        web.get('/monitor.js',monitor_script),web.get('/api/learning',learning_cards),
                        web.post('/api/learning/{request_id}/review',review_card),
                        web.post('/api/learning/{request_id}/practice',practice_card),
                        web.post('/api/learning/{request_id}/select',decide_candidate),
                        web.post('/api/learning/{request_id}/dismiss',decide_candidate)])
    return app


async def start_debug_page(log_path, port, *, billing_db=None, admin_file=None, host='127.0.0.1', runtime_status=None, update_budget=None,
                           set_device_volume=None, device_status=None, set_device_mode=None):
    runner = web.AppRunner(create_app(log_path,billing_db=billing_db,admin_file=admin_file,runtime_status=runtime_status,update_budget=update_budget,
                                    set_device_volume=set_device_volume, device_status=device_status,
                                    set_device_mode=set_device_mode), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    print(f'Local request viewer: http://127.0.0.1:{port}', flush=True)
    return runner


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event-log', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8001)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    web.run_app(create_app(args.event_log.expanduser().resolve()),
                host='127.0.0.1', port=args.port, access_log=None)
