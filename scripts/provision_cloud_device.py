"""Create a private per-device NVS image using ESP-IDF's official generator.
Never flashes, changes server registry, or includes supplier API keys.
"""
import argparse
import binascii
import csv
import hashlib
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import struct
from pathlib import Path

URL = 'wss://english.example.com/xiaozhi/v1/'
OFFSET = 0x10000
SIZE = 0x6000


def prepare(device_id, output, backup, idf_path, url=URL):
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', device_id):
        raise ValueError('Use the actual lowercase station MAC from esptool')
    original = backup.read_bytes()
    if len(original) != 0x410000:
        raise ValueError('Verified backup of offsets 0 through 0x410000 required for this board')
    if original[OFFSET:OFFSET+SIZE] != b'\xff' * SIZE:
        raise ValueError('Reserved cloud area is not blank; refuse to overwrite existing data')
    generator = idf_path / 'components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py'
    if not generator.is_file():
        raise ValueError('Set IDF_PATH to the existing official ESP-IDF checkout')
    spec = importlib.util.spec_from_file_location('esp_partitions',
        idf_path/'components/partition_table/gen_esp32part.py')
    parser = importlib.util.module_from_spec(spec);spec.loader.exec_module(parser)
    table = parser.PartitionTable.from_binary(original[0x8000:0x9000])
    expected = {'nvs':(0x9000,0x4000),'otadata':(0xd000,0x2000),
                'ota_0':(0x20000,0x3f0000),'ota_1':(0x410000,0x3f0000),
                'assets':(0x800000,0x800000)}
    for name, bounds in expected.items():
        part=table.find_by_name(name)
        if part is None or (part.offset,part.size)!=bounds:
            raise ValueError('Device partition layout differs from verified board')
    if any(p.offset < OFFSET+SIZE and p.offset+p.size > OFFSET for p in table):
        raise ValueError('Cloud area overlaps an existing partition')
    # Accept only factory selection or the exact already-confirmed ota_0 state.
    # seq/CRC format follows ESP-IDF app_update/otatool.py; no metadata is rewritten.
    seq=struct.pack('<I',1)
    confirmed=seq+b'\xff'*20+struct.pack('<II',2,binascii.crc32(seq,0xffffffff))+b'\xff'*(0x2000-32)
    if original[0xd000:0xf000] not in (b'\xff'*0x2000,confirmed):
        raise ValueError('OTA selection is not confirmed ota_0; inspect before programming')
    from urllib.parse import urlsplit
    endpoint = urlsplit(url)
    if endpoint.scheme != 'wss' or not endpoint.netloc or endpoint.hostname == 'english.example.com' or endpoint.path != '/xiaozhi/v1/' or endpoint.query or endpoint.fragment or endpoint.username:
        raise ValueError('Set your own wss:// host with /xiaozhi/v1/ path')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    token = secrets.token_urlsafe(32)
    credential = {'device_id': device_id, 'token': token, 'url': url}
    (output/'credential.json').write_text(json.dumps(credential, indent=2)+'\n')
    with (output/'cloudcfg.csv').open('x', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['key','type','encoding','value'])
        writer.writerow(['websocket','namespace','',''])
        for key, value in (('url',url),('device_id',device_id),('token',token)):
            writer.writerow([key,'data','string',value])
    subprocess.run([sys.executable, str(generator), 'generate', str(output/'cloudcfg.csv'),
                    str(output/'cloudcfg.bin'), hex(SIZE)], check=True, capture_output=True)
    metadata = {'device_id':device_id,'url':url,'token_sha256':hashlib.sha256(token.encode()).hexdigest(),
                'offset':OFFSET,'size':SIZE,'backup_sha256':hashlib.sha256(original).hexdigest(),
                'nvs_sha256':hashlib.sha256((output/'cloudcfg.bin').read_bytes()).hexdigest(),
                'flashed':False,'server_registered':False}
    (output/'provision.json').write_text(json.dumps(metadata,indent=2)+'\n')
    for p in output.iterdir():p.chmod(0o600)
    return metadata


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--device-id',required=True)
    p.add_argument('--url',required=True)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--backup',required=True,type=Path)
    p.add_argument('--idf-path',type=Path,default=os.environ.get('IDF_PATH'))
    a=p.parse_args();os.umask(0o077)
    if not a.idf_path:p.error('Source the existing ESP-IDF environment first')
    prepare(a.device_id,a.output.resolve(),a.backup,a.idf_path,a.url)
    print('Private device configuration prepared. No flash or server mutation performed.')


if __name__=='__main__':main()
