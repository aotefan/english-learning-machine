"""Bounded admin speaker control through the firmware's existing MCP tools."""
import asyncio
import json
import secrets
import time


class DeviceControl:
    def __init__(self, authenticate):
        self.authenticate = authenticate
        self.sockets = {}
        self.pending = {}
        self.lock = asyncio.Lock()
        self.connections = {}
        self.cached = {}

    def attach(self, conn, features):
        self.connections[conn.device_id] = conn
        conn.mvp_device_controls = isinstance(features, dict) and features.get('mvp_device_controls') is True

    def device(self):
        devices = [(name, ws) for name, ws in self.sockets.items() if name != 'cloud-smoke']
        if len(devices) != 1:
            raise ValueError('one_online_device_required')
        return devices[0]

    def register(self, device, ws):
        self.sockets[device] = ws

    def unregister(self, device, ws):
        if self.sockets.get(device) is ws:
            self.sockets.pop(device, None)
            self.connections.pop(device, None)
            self.cached.pop(device, None)
        for (owner, _), (socket, future) in list(self.pending.items()):
            if owner == device and socket is ws and not future.done():
                future.set_exception(ValueError('device_disconnected'))

    def receive(self, device, ws, payload):
        if not isinstance(payload, dict) or type(payload.get('id')) is not int:
            return
        entry = self.pending.get((device, payload['id']))
        if entry is not None and entry[0] is ws and not entry[1].done():
            entry[1].set_result(payload)

    async def call(self, device, ws, name, arguments):
        if not self.authenticate(ws.request):
            raise ValueError('device_credential_revoked')
        # Firmware MCP requires numeric signed-32-bit request IDs.
        rid = 1_000_000 + secrets.randbelow(1_000_000_000)
        future = asyncio.get_running_loop().create_future()
        self.pending[(device, rid)] = (ws, future)
        try:
            await ws.send(json.dumps({'type':'mcp', 'payload': {
                'jsonrpc':'2.0', 'id':rid, 'method':'tools/call',
                'params':{'name':name, 'arguments':arguments}}}))
            reply = await asyncio.wait_for(future, 10)
            result = reply.get('result')
            if 'error' in reply or not isinstance(result, dict) or result.get('isError') is True:
                raise ValueError('device_control_failed')
            return result
        finally:
            self.pending.pop((device, rid), None)

    @staticmethod
    def text_result(result):
        for item in result.get('content', []):
            if isinstance(item, dict) and item.get('type') == 'text':
                try:
                    data = json.loads(item['text'])
                    if isinstance(data, dict):return data
                except (ValueError, TypeError, KeyError):pass
        raise ValueError('invalid_device_status')

    async def status(self):
        try:device, ws = self.device()
        except ValueError:
            return {'online':False,'controls_available':False,'teaching_mode':None,'volume':None,'listening_mode':None}
        conn = self.connections.get(device)
        enabled = getattr(conn, 'mvp_device_controls', False)
        data = {'online':True,'controls_available':enabled,'teaching_mode':getattr(conn,'mvp_teaching_mode',None),
                'volume':None,'listening_mode':None,'automatic_available':False,'observed_at':None}
        cached = self.cached.get(device)
        if cached:data.update(cached)
        if enabled and not self.lock.locked() and (not cached or time.time()-cached['observed_at']>=5):
            async with self.lock:
                try:
                    raw = self.text_result(await self.call(device, ws, 'self.english_learning.get_status', {}))
                    volume, listening = raw.get('volume'), raw.get('listening_mode')
                    if type(volume) is not int or not 0 <= volume <= 100 or listening not in ('auto','manual'):
                        raise ValueError('invalid_device_status')
                    cached = {'volume':volume,'listening_mode':listening,
                              'automatic_available':raw.get('automatic_available') is True,
                              'observed_at':time.time()}
                    self.cached[device] = cached
                    data.update(cached)
                except (ValueError, asyncio.TimeoutError):
                    data['status_error'] = True
        return data

    async def set_mode(self, kind, value):
        if kind not in ('teaching','listening') or value not in (
                ('express','chat') if kind=='teaching' else ('auto','manual')):
            raise ValueError('invalid_device_mode')
        if self.lock.locked():raise ValueError('device_control_busy')
        async with self.lock:
            device, ws = self.device()
            if not self.authenticate(ws.request):raise ValueError('device_credential_revoked')
            conn = self.connections.get(device)
            if conn is None:raise ValueError('device_not_ready')
            task = getattr(conn,'mvp_task',None)
            session = getattr(conn,'mvp_duplex_session',None)
            if (getattr(conn,'mvp_recording',False) or (task and not task.done())
                    or (getattr(conn,'mvp_audio_returning',False) and not getattr(conn,'mvp_playback_completed',False))
                    or (session and (session.capture or session.active or session.pending))):
                raise ValueError('device_busy')
            if kind == 'teaching':
                if getattr(conn,'mvp_teaching_mode',None) == value:
                    return {'kind':kind,'mode':value,'confirmed_by':'cloud'}
                conn.mvp_teaching_mode = value
                conn.mvp_dialogue_context = []
                conn.mvp_help_context = conn.mvp_clarification_context = conn.mvp_correction_context = None
                from core.handle.englishMvp import send_teaching_mode
                await send_teaching_mode(conn)
                return {'kind':kind,'mode':value,'confirmed_by':'cloud'}
            if not getattr(conn,'mvp_device_controls',False):raise ValueError('firmware_update_required')
            await self.call(device,ws,'self.english_learning.set_listening_mode',{'automatic':value=='auto'})
            raw = self.text_result(await self.call(device,ws,'self.english_learning.get_status',{}))
            if raw.get('listening_mode') != value:raise ValueError('device_mode_not_confirmed')
            self.cached.pop(device,None)
            return {'kind':kind,'mode':value,'confirmed_by':'device'}

    async def read_volume(self, device, ws):
        result = await self.call(device, ws, 'self.get_device_status', {})
        for item in result.get('content', []):
            if isinstance(item, dict) and item.get('type') == 'text':
                try:
                    status = json.loads(item['text'])
                    volume = status['audio_speaker']['volume']
                    if type(volume) is int and 0 <= volume <= 100:
                        return volume
                except (ValueError, TypeError, KeyError):
                    continue
        raise ValueError('device_volume_unknown')

    async def set_volume(self, volume):
        if type(volume) is not int or not 0 <= volume <= 100:
            raise ValueError('invalid_volume')
        if self.lock.locked():
            raise ValueError('device_control_busy')
        async with self.lock:
            device, ws = self.device()
            if not getattr(self.connections.get(device),'mvp_device_controls',False):
                raise ValueError('firmware_update_required')
            before = await self.read_volume(device, ws)
            if before != volume:
                await self.call(device, ws, 'self.audio_speaker.set_volume', {'volume':volume})
            after = await self.read_volume(device, ws)
            if after != volume:
                raise ValueError('device_volume_not_confirmed')
            self.cached.pop(device,None)
            return {'previous_volume':before, 'volume':after, 'confirmed':True}
