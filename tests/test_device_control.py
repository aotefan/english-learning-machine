"""Offline MCP ownership and admin authorization regression tests."""
import asyncio,json,sys,tempfile,hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'scripts'))
from device_control import DeviceControl
from debug_page import create_app
from aiohttp.test_utils import TestClient,TestServer
import base64
class ControlTests(unittest.IsolatedAsyncioTestCase):
 async def test_verified_volume_boundaries_wrong_socket_and_disconnect(self):
  control=DeviceControl(lambda _:True);state={'volume':40};calls=[]
  class Socket:
   request=object()
   async def send(self,raw):
    msg=json.loads(raw)['payload'];name=msg['params']['name'];calls.append(name)
    if name.endswith('set_volume'):state['volume']=msg['params']['arguments']['volume'];text='true'
    else:text=json.dumps({'audio_speaker':state})
    wrong=Socket();control.receive('device',wrong,{'id':msg['id'],'result':{}})
    self.assert_incoming(control,msg)
    control.receive('device',self,{'id':msg['id'],'result':{'content':[{'type':'text','text':text}]}})
   def assert_incoming(self,control,msg):
    assert not control.pending[('device',msg['id'])][1].done()
  ws=Socket();control.register('device',ws);control.attach(SimpleNamespace(device_id='device'),{'mvp_device_controls':True})
  self.assertEqual(await control.set_volume(100),{'volume':100,'previous_volume':40,'confirmed':True})
  self.assertEqual(calls,['self.get_device_status','self.audio_speaker.set_volume','self.get_device_status'])
  for value in (-1,101,True,50.5):
   with self.assertRaisesRegex(ValueError,'invalid_volume'):await control.set_volume(value)
  control.unregister('device',ws)
  with self.assertRaisesRegex(ValueError,'online_device'):await control.set_volume(100)
 async def test_http_auth_origin_range_and_action_confirmation(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder);admin=path/'admin.json';admin.write_text(json.dumps({'password_sha256':hashlib.sha256(b'fixture').hexdigest()}));admin.chmod(0o600)
   calls=[]
   async def setter(volume):calls.append(volume);return {'volume':volume,'confirmed':True}
   async def mode(kind,value):calls.append((kind,value));return {'kind':kind,'mode':value,'confirmed_by':'cloud'}
   app=create_app(path/'events.jsonl',billing_db=path/'test.sqlite',admin_file=admin,set_device_volume=setter,set_device_mode=mode)
   async with TestClient(TestServer(app)) as client:
    url=str(client.make_url('/')).rstrip('/');auth='Basic '+base64.b64encode(b'admin:fixture').decode()
    self.assertEqual((await client.post('/api/device/volume',json={'volume':100})).status,401)
    self.assertEqual((await client.post('/api/device/volume',json={'volume':100},headers={'Authorization':auth})).status,403)
    headers={'Authorization':auth,'Origin':url,'X-English-Edge-Action':'volume'}
    self.assertEqual((await client.post('/api/device/volume',json={'volume':True},headers=headers)).status,400)
    self.assertEqual(calls,[])
    response=await client.post('/api/device/volume',json={'volume':100},headers=headers)
    self.assertEqual(response.status,200);self.assertTrue((await response.json())['confirmed'])
    headers['X-English-Edge-Action']='mode'
    response=await client.post('/api/device/mode',json={'kind':'teaching','mode':'chat'},headers=headers)
    self.assertEqual(response.status,200);self.assertEqual((await response.json())['mode'],'chat')
    self.assertEqual((await client.post('/api/device/mode',json={'kind':'teaching','mode':'automatic'},headers=headers)).status,400)
    self.assertEqual(calls,[100,('teaching','chat')])
if __name__ == '__main__': unittest.main()
