"""Fail-closed guards before provisioning any device identity or flash image."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('provision',Path(__file__).resolve().parents[1]/'scripts/provision_cloud_device.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class ProvisionTests(unittest.TestCase):
    def test_bad_device_id_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);out=root/'out'
            with self.assertRaises(ValueError):module.prepare('bad\nAuthorization: secret',out,root/'missing',root)
            self.assertFalse(out.exists())

    def test_partial_backup_is_rejected_before_generating_identity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);backup=root/'backup';backup.write_bytes(b'\xff'*0x16000);out=root/'out'
            with self.assertRaisesRegex(ValueError,'backup'):module.prepare('aa:bb:cc:dd:ee:ff',out,backup,root)
            self.assertFalse(out.exists())

    def test_preexisting_data_in_reserved_area_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);backup=root/'backup';data=bytearray(b'\xff'*0x410000)
            data[module.OFFSET+module.SIZE-1]=0;backup.write_bytes(data);out=root/'out'
            with self.assertRaisesRegex(ValueError,'not blank'):module.prepare('aa:bb:cc:dd:ee:ff',out,backup,root)
            self.assertFalse(out.exists())


if __name__=='__main__':unittest.main()
