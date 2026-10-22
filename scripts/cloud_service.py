"""Cloud policy around the existing XiaoZhi server; no replacement transport."""
import hashlib
import hmac
import json
import math
import os
import sqlite3
import time
from pathlib import Path

from cloud_budget import positive, reserve, stored_limits, update_limits
from cloud_learning import ensure_learning_schema


class CloudPolicy:
    def __init__(self, state_dir, devices_file, live=False):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / 'usage.sqlite'
        self.devices_file = Path(devices_file)
        self.live = live
        self.started_at = time.monotonic()
        self.active_devices = set()
        from device_control import DeviceControl
        self.device_control = DeviceControl(self.authenticate)
        self.last_turn = {}
        # Only synthesized output, never microphone audio; bounded in memory.
        self.pending_audio = {}
        self.last_audio = {}
        self.rates = None
        self.limit = None
        self.turns = None
        if live:
            self.rates = [positive(os.getenv(k, ''), k) for k in (
                'ASR_CNY_PER_HOUR', 'LLM_INPUT_CNY_PER_MILLION',
                'LLM_OUTPUT_CNY_PER_MILLION', 'TTS_CNY_PER_10K_CHARS')]
            self.limit = positive(os.getenv('DAILY_CNY_LIMIT', ''), 'DAILY_CNY_LIMIT')
            self.turns = int(os.getenv('DAILY_TURN_LIMIT', '0'))
            if self.turns <= 0:
                raise ValueError('DAILY_TURN_LIMIT must be positive')
        # Device file contains only SHA-256 hashes of random per-device tokens.
        self._devices()
        with sqlite3.connect(self.db_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS events (time REAL, request_id TEXT, device_id TEXT, event TEXT, data TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS events_time ON events(time)')
            db.execute('CREATE TABLE IF NOT EXISTS learning (request_id TEXT PRIMARY KEY, time REAL, device_id TEXT, asr_raw TEXT, english TEXT, playback_reported INTEGER DEFAULT 0)')
            ensure_learning_schema(db)
            # Billing metadata has no speech/text content and survives transcript cleanup.
            db.execute('CREATE TABLE IF NOT EXISTS billing_turns (request_id TEXT PRIMARY KEY, time REAL, reserved_cny REAL, status TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS billing_stages (request_id TEXT, stage TEXT, time REAL, status TEXT, input_units REAL, output_units REAL, estimated_cny REAL, rates TEXT, PRIMARY KEY(request_id,stage))')
            db.execute("UPDATE billing_stages SET status='unknown' WHERE status='pending'")
            db.execute("UPDATE billing_turns SET status='interrupted' WHERE status='processing'")
            # A restart must retain the barrier for a generated but unfinished
            # turn, even when its deferred learning row was not written yet.
            self.learning_inflight = {device:(rid,stamp) for device,rid,stamp in db.execute(
                '''SELECT e.device_id,e.request_id,e.time FROM events e
                   LEFT JOIN learning l ON l.request_id=e.request_id
                   WHERE e.event='english_result' AND e.time>=? AND l.request_id IS NULL
                   ORDER BY e.time''', (time.time()-600,)) if device}
        self.last_cleanup = 0
        self.prune_expired()

    def prune_expired(self, *, now=None):
        """Remove retained speech text even if no new voice request arrives."""
        now = time.time() if now is None else now
        with sqlite3.connect(self.db_path, timeout=10) as db:
            db.execute('DELETE FROM events WHERE time < ?', (now - 7*86400,))
            db.execute('DELETE FROM learning WHERE time < ?', (now - 7*86400,))
        self.last_cleanup = now

    def runtime_status(self):
        """Read-only state; readiness is configuration, never vendor verification."""
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).date().isoformat()
        with sqlite3.connect(self.db_path) as db:
            used = (0, 0)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='daily'").fetchone():
                used = db.execute('SELECT turns,micros FROM daily WHERE day=?', (day,)).fetchone() or used
            limit, turns = stored_limits(db, self.limit, self.turns)
        missing = [k for k in ('DOUBAO_SPEECH_API_KEY', 'ARK_API_KEY', 'ARK_MODEL_ID',
                              'DOUBAO_TTS_SPEAKER', 'DAILY_TURN_LIMIT', 'DAILY_CNY_LIMIT')
                   if not os.getenv(k)]
        return {'mode': 'LIVE' if self.live else 'STANDBY',
                'uptime_seconds': round(time.monotonic() - self.started_at),
                'active_connections': len(self.active_devices), 'connection_limit': 2,
                'missing_configuration': missing, 'vendor_verified': None,
                'budget': {'utc_day': day, 'reserved_cny': used[1]/1e6,
                           'turns': used[0], 'enabled': self.live,
                           'limit_cny': float(limit) if limit is not None else None,
                           'turn_limit': turns,
                           'remaining_cny': max(0, float(limit)-used[1]/1e6) if self.live else None,
                           'remaining_turns': max(0, turns-used[0]) if self.live else None}}

    def set_budget(self, turn_limit, daily_limit):
        if not self.live:
            raise ValueError('cloud_live_disabled')
        update_limits(self.db_path, daily_limit, turn_limit)
        return self.runtime_status()['budget']

    def _devices(self):
        if self.devices_file.stat().st_mode & 0o077:
            raise ValueError('device registry must have mode 600')
        value = json.loads(self.devices_file.read_text())
        if not isinstance(value, dict) or not value:
            raise ValueError('empty device registry')
        if any(not isinstance(k, str) or not isinstance(v, str) or len(v) != 64
               or any(c not in '0123456789abcdef' for c in v) for k, v in value.items()):
            raise ValueError('invalid device registry')
        return value

    def authenticate(self, request):
        if request.path != '/xiaozhi/v1/':
            return False  # No query credentials and no alternate paths.
        try:
            device = request.headers.get('device-id', '')
            auth = request.headers.get('authorization', '')
            if not auth.startswith('Bearer ') or len(auth) > 512:
                return False
            expected = self._devices().get(device)
            actual = hashlib.sha256(auth[7:].encode()).hexdigest()
            return bool(expected and hmac.compare_digest(actual, expected))
        except Exception:
            return False  # Invalid/missing registry fails closed; supports revocation.

    def before_turn(self, conn, duration_seconds):
        if not self.live:
            raise ValueError('cloud_live_disabled')
        if not self.authenticate(conn.websocket.request):
            raise ValueError('device_credential_revoked')
        now = time.monotonic()
        if (not getattr(conn, 'mvp_duplex_turn', False)
                and now - self.last_turn.get(conn.device_id, -100) < 3):
            raise ValueError('cloud_turn_rate_limited')
        self.last_turn[conn.device_id] = now
        rates = self.rates
        # Match teaching_response's 6144-byte input envelope (including its
        # protocol headroom), rather than the earlier 4096-token estimate.
        amount = (rates[0] * positive(duration_seconds, 'duration') / 3600
                  + rates[1] * 6144 / 1000000 + rates[2] * 256 / 1000000
                  + rates[3] * 400 / 10000)
        reservation = reserve(self.db_path, amount, self.limit, self.turns)
        with sqlite3.connect(self.db_path) as db:
            db.execute('INSERT INTO billing_turns VALUES(?,?,?,?)',
                       (conn.mvp_request_id,time.time(),reservation['reserved_cny'],'processing'))
        return reservation

    def error_response(self, reason, *, now=None):
        """Safe device recovery hints; never retry or resubmit a paid turn."""
        from cloud_report import classify_error
        code = classify_error(reason)
        retry_ms = 3000
        if code == 'budget_exhausted':
            now = time.time() if now is None else now
            # Daily quotas use UTC. Keep the user's listening choice while the
            # device waits for a fresh day instead of repeatedly uploading.
            retry_ms = max(1, min(86400000, math.ceil((86400 - now % 86400) * 1000)))
        elif code in ('standby', 'credential_revoked'):
            retry_ms = 60000
        return {'error_code': code, 'retry_after_ms': retry_ms}

    def last_played_english(self, device_id, *, now=None):
        """Only the same device's recent, acknowledged English turn is repeatable."""
        if not isinstance(device_id, str) or not device_id:
            return None
        now = time.time() if now is None else now
        inflight = self.learning_inflight.get(device_id)
        if inflight and 0 <= now-inflight[1] < 600:return None
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                '''SELECT english, rejected, playback_reported FROM learning WHERE device_id = ? AND time >= ?
                   AND intent IN ('express', 'chat', 'mode')
                   AND english IS NOT NULL AND english != ''
                   ORDER BY time DESC LIMIT 1''', (device_id, now - 600)).fetchone()
        return row[0] if row and not row[1] and row[2] else None

    def reject_last_expression(self, device_id, *, now=None):
        """Exclude only the most recent confirmed expression; retain it for diagnosis."""
        now = time.time() if now is None else now
        inflight = self.learning_inflight.get(device_id)
        if inflight and 0 <= now-inflight[1] < 600:return None
        with sqlite3.connect(self.db_path, timeout=5) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(
                '''SELECT request_id, asr_raw, english, rejected, playback_reported, intent FROM learning
                   WHERE device_id = ? AND time >= ? AND intent IN ('express', 'chat', 'mode')
                     AND english IS NOT NULL AND english != ''
                   ORDER BY time DESC LIMIT 1''', (device_id, now - 600)).fetchone()
            if not row or row[3] or not row[4] or row[5] == 'mode':
                return None
            db.execute('UPDATE learning SET rejected = 1 WHERE request_id = ?', (row[0],))
        self.last_audio.pop(device_id, None)
        return {'request_id': row[0], 'transcript': row[1], 'english': row[2]}

    def cache_pending_audio(self, device_id, request_id, english, wav):
        if not (isinstance(device_id, str) and isinstance(request_id, str) and
                isinstance(english, str) and isinstance(wav, bytes) and 0 < len(wav) <= 1500000):
            return
        self.pending_audio = {rid: entry for rid, entry in self.pending_audio.items()
                              if entry[0] != device_id}
        self.pending_audio[request_id] = (device_id, english, wav, time.time())
        if len(self.pending_audio) > 4:
            oldest = min(self.pending_audio, key=lambda rid: self.pending_audio[rid][3])
            self.pending_audio.pop(oldest, None)

    def last_played_audio(self, device_id, english, *, now=None):
        now = time.time() if now is None else now
        entry = self.last_audio.get(device_id)
        if not entry or now - entry[2] > 600 or entry[0] != english:
            return None
        return entry[1]

    def record(self, conn, row):
        """Bounded text/usage records, never raw audio or authentication headers."""
        now = time.time()
        rid = row.get('request_id')
        device = conn.device_id
        name = row['event']
        if name == 'recording_started':
            conn.cloud_usage = {}
        usage = getattr(conn, 'cloud_usage', {})
        if name in ('asr_usage', 'llm_usage', 'tts_usage'):
            usage[name] = row
            conn.cloud_usage = usage
        if name == 'request_complete' and self.rates:
            # Actual reported usage is separate from conservative reservations.
            values = (usage.get('asr_usage', {}).get('input_audio_seconds'),
                      usage.get('llm_usage', {}).get('input_tokens'),
                      usage.get('llm_usage', {}).get('output_tokens'),
                      usage.get('tts_usage', {}).get('billed_text_words'))
            row['estimated_cny'] = None
            if all(isinstance(v, (int, float)) and v >= 0 for v in values):
                from decimal import Decimal
                row['estimated_cny'] = float(sum(
                    rate * Decimal(str(value)) / denominator
                    for rate,value,denominator in zip(self.rates,values,(3600,1000000,1000000,10000))))
            row['usage_complete'] = row['estimated_cny'] is not None
        with sqlite3.connect(self.db_path, timeout=5) as db:
            stage_map={'asr_started':'asr','asr_usage':'asr','english_started':'english',
                       'llm_usage':'english','tts_started':'tts','tts_usage':'tts'}
            if self.rates and name in stage_map:
                from decimal import Decimal
                stage=stage_map[name]
                if name.endswith('_started'):
                    db.execute('INSERT OR IGNORE INTO billing_stages(request_id,stage,time,status,rates) VALUES(?,?,?,?,?)',
                               (rid,stage,now,'pending',json.dumps([str(x) for x in self.rates])))
                else:
                    input_units=row.get({'asr':'input_audio_seconds','english':'input_tokens','tts':'billed_text_words'}[stage])
                    output_units=row.get('output_tokens') if stage=='english' else 0
                    known=all(isinstance(v,(int,float)) and v>=0 for v in (input_units,output_units))
                    amount=None
                    if known:
                        n=Decimal(str(input_units));m=Decimal(str(output_units))
                        amount=float(self.rates[0]*n/3600 if stage=='asr' else
                                     (self.rates[1]*n+self.rates[2]*m)/1000000 if stage=='english' else self.rates[3]*n/10000)
                    db.execute('UPDATE billing_stages SET status=?,input_units=?,output_units=?,estimated_cny=? WHERE request_id=? AND stage=?',
                               ('estimated' if known else 'unknown',input_units,output_units,amount,rid,stage))
            if name=='request_complete':
                db.execute("UPDATE billing_stages SET status='unknown' WHERE request_id=? AND status='pending'",(rid,))
                db.execute('UPDATE billing_turns SET status=? WHERE request_id=?',
                           ('success' if row.get('success') else 'failed',rid))
            if now - self.last_cleanup > 3600:
                # Seven-day pilot retention, including raw text. Budget table is retained.
                db.execute('DELETE FROM events WHERE time < ?', (now - 7*86400,))
                db.execute('DELETE FROM learning WHERE time < ?', (now - 7*86400,))
                self.last_cleanup = now
            db.execute('INSERT INTO events VALUES (?,?,?,?,?)',
                       (now, rid, device, name, json.dumps(row, ensure_ascii=False)))
            # Per-turn deferred learning writes stay off the first-sound path.
            # Diagnostic events and cost reservations remain immediate.
            pending = conn.__dict__.setdefault('cloud_learning_pending', {})
            if name == 'asr_result':
                pending[rid] = {'time':now, 'asr_raw':row['text'], 'intent':'express', 'english':None}
                if len(pending) > 4:pending.pop(next(iter(pending)))
            elif name == 'intent_classified':
                if rid in pending:pending[rid]['intent'] = row['intent']
                else:db.execute('UPDATE learning SET intent=? WHERE request_id=?', (row['intent'], rid))
            elif name == 'english_result':
                if rid in pending:
                    pending[rid]['english'] = row['text']
                    if pending[rid]['intent'] in ('express','chat','mode'):
                        self.learning_inflight[device] = (rid,now)
                else:db.execute('UPDATE learning SET english=? WHERE request_id=?', (row['text'], rid))
            if name in ('request_complete','playback_confirmed'):
                learned = pending.pop(rid, None)
                if learned is not None:
                    db.execute('INSERT OR IGNORE INTO learning(request_id,time,device_id,asr_raw,intent,english) VALUES(?,?,?,?,?,?)',
                        (rid,learned['time'],device,learned['asr_raw'],learned['intent'],learned['english']))
                inflight = self.learning_inflight.get(device)
                if inflight and inflight[0] == rid:self.learning_inflight.pop(device,None)
            if name == 'playback_confirmed':
                db.execute('UPDATE learning SET playback_reported=1 WHERE request_id=?', (rid,))
                cached = self.pending_audio.pop(rid, None)
                if cached and cached[0] == device:
                    self.last_audio[device] = (cached[1], cached[2], now)
                    if len(self.last_audio) > 4:
                        oldest = min(self.last_audio, key=lambda key: self.last_audio[key][2])
                        self.last_audio.pop(oldest, None)
            if name == 'request_complete' and not row.get('success'):
                self.pending_audio.pop(rid, None)


def attach_policy(server, policy):
    """Reuse all upstream connection, Opus and request recovery behavior."""
    from core.auth import AuthenticationError
    original_http = server._http_response
    original_connection = server._handle_connection
    server.cloud_policy = policy

    async def http_response(ws, request):
        if request.path == '/healthz':
            return ws.respond(200, 'ok\n')  # Liveness only, not vendor readiness.
        if request.headers.get('upgrade', '').lower() != 'websocket':
            return ws.respond(404, 'not found\n')
        if not policy.authenticate(request):
            return ws.respond(401, 'unauthorized\n')
        device = request.headers.get('device-id')
        if device in policy.active_devices or len(policy.active_devices) >= 2:
            return ws.respond(429, 'connection limit\n')
        return await original_http(ws, request)

    async def authenticate(ws):
        if not policy.authenticate(ws.request):
            raise AuthenticationError('unauthorized')

    async def connection(ws):
        device = ws.request.headers.get('device-id', '')
        if not policy.authenticate(ws.request):
            await ws.close(code=1008, reason='unauthorized')
            return
        if device in policy.active_devices or len(policy.active_devices) >= 2:
            await ws.close(code=1013, reason='connection limit')
            return
        policy.active_devices.add(device)
        policy.device_control.register(device, ws)
        try:
            await original_connection(ws)
        finally:
            policy.device_control.unregister(device, ws)
            policy.active_devices.discard(device)

    server._http_response = http_response
    server._handle_auth = authenticate
    server._handle_connection = connection
    return server
