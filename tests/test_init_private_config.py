"""Private initialization never overwrites credentials or prints secret values."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from init_private_config import initialize


class PrivateConfigTests(unittest.TestCase):
    def test_permissions_and_token_registry_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = initialize(Path(tmp) / 'private', 'fixture-device', 'ws://127.0.0.1:18000/xiaozhi/v1/')
            client = json.loads((folder / 'cloud-client.json').read_text())
            registry = json.loads((folder / 'devices.json').read_text())
            self.assertEqual(registry['fixture-device'], hashlib.sha256(client['token'].encode()).hexdigest())
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in folder.iterdir()))
            self.assertIn('DOUBAO_LIVE_ENABLED=0', (folder / 'cloud.env').read_text())

    def test_existing_credentials_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = initialize(Path(tmp) / 'private', 'fixture', 'ws://localhost:18000/xiaozhi/v1/')
            before = {p.name: p.read_bytes() for p in folder.iterdir()}
            with self.assertRaises(FileExistsError):
                initialize(folder, 'other', 'ws://localhost:18000/xiaozhi/v1/')
            self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_remote_plaintext_and_query_credentials_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for url in ('ws://example.com/xiaozhi/v1/', 'wss://example.com/xiaozhi/v1/?token=fixture', 'wss://user:pass@example.com/xiaozhi/v1/'):
                with self.assertRaises(ValueError):
                    initialize(Path(tmp) / 'private', 'fixture', url)
            self.assertFalse((Path(tmp) / 'private').exists())

    def test_generated_credentials_are_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [initialize(Path(tmp) / str(i), 'fixture', 'ws://localhost:18000/xiaozhi/v1/') for i in range(2)]
            self.assertNotEqual((paths[0] / 'cloud-client.json').read_bytes(), (paths[1] / 'cloud-client.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
