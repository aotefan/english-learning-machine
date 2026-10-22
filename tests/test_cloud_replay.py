import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from cloud_replay import main, read_manifest, read_credential, validate_url


class ReplayGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        with wave.open(str(self.folder/'01.wav'),'wb') as f:
            f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(b'\0\0'*3200)
        self.manifest=self.folder/'manifest.json'
        self.manifest.write_text(json.dumps([{'id':'01','prompt':'Not a transcript','transcript':None}]))

    def tearDown(self):self.tmp.cleanup()

    def test_local_default_never_connects_and_preserves_unknown_reference(self):
        args=['cloud_replay','--manifest',str(self.manifest),'--input-dir',str(self.folder)]
        with patch.object(sys,'argv',args),patch('cloud_replay.connect') as connect,contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(),0)
        connect.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['paid_calls'],0)
        self.assertIsNone(read_manifest(self.manifest,self.folder,1)[0]['transcript'])

    def test_url_and_private_credential_guards(self):
        for url in ('ws://public.example/xiaozhi/v1/','wss://x/xiaozhi/v1/?token=secret','wss://u:p@x/xiaozhi/v1/','https://x/xiaozhi/v1/'):
            with self.assertRaises(ValueError):validate_url(url)
        validate_url('wss://english.example.com/xiaozhi/v1/')
        validate_url('ws://127.0.0.1:18000/xiaozhi/v1/')
        p=self.folder/'credential.json';p.write_text(json.dumps({'device_id':'d','token':'test'}));p.chmod(0o644)
        with self.assertRaises(ValueError):read_credential(p)
        p.chmod(0o600);self.assertEqual(read_credential(p)['device_id'],'d')
        p.write_text('[]')
        with self.assertRaises(ValueError):read_credential(p)

    def test_preflight_checks_whole_selected_batch_before_calls(self):
        self.manifest.write_text(json.dumps([{'id':'01'},{'id':'missing'}]))
        with patch('cloud_replay.connect') as connect:
            with self.assertRaises(OSError):read_manifest(self.manifest,self.folder,2)
        connect.assert_not_called()
        self.manifest.write_text(json.dumps([{'id':'01'},{'id':'01'}]))
        with self.assertRaisesRegex(ValueError,'Duplicate'):read_manifest(self.manifest,self.folder,2)
        with self.assertRaises(ValueError):read_manifest(self.manifest,self.folder,31)

    def test_old_direct_probe_cannot_bypass_cloud_billing(self):
        import probe_doubao
        with patch.object(sys,'argv',['probe_doubao','--live']),patch('probe_doubao.load_cloud_config') as config:
            with self.assertRaisesRegex(ValueError,'Direct paid probe retired'):probe_doubao.main()
        config.assert_not_called()


if __name__=='__main__':unittest.main()
