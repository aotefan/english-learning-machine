"""Bounded, checksummed flash backup using official esptool; never writes flash."""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path
import esptool


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--size',type=lambda s:int(s,0),default=0x410000)
    a=p.parse_args();os.umask(0o077)
    if a.size<=0 or a.size>0x1000000 or a.size%0x10000:p.error('Use aligned size up to 16 MiB')
    if a.output.exists():p.error('Output exists; retain it and choose another path')
    a.output.mkdir(mode=0o700,parents=True)
    esp=None;chunks=[];device_id=None
    try:
        for offset in range(0,a.size,0x10000):
            for attempt in range(3):
                try:
                    if esp is None:
                        esp=esptool.detect_chip(a.port,baud=115200)
                        if esp.CHIP_NAME!='ESP32-S3':raise ValueError('Expected ESP32-S3')
                        actual=':'.join(f'{b:02x}' for b in esp.read_mac())
                        if device_id is not None and actual!=device_id:
                            raise ValueError('Serial port changed to another device')
                        device_id=actual
                        esp=esp.run_stub();esptool.attach_flash(esp)
                    # esptool verifies each returned block against the device MD5.
                    data=esp.read_flash(offset,0x10000)
                    break
                except Exception:
                    if esp is not None:
                        esp._port.close();esp=None
                    if attempt==2:raise
                    time.sleep(1)
            name=f'{offset:08x}.bin';(a.output/name).write_bytes(data);chunks.append(data)
            if offset%0x80000==0:print(f'Backed up {offset+len(data)}/{a.size} bytes',flush=True)
            time.sleep(.03)
        combined=b''.join(chunks);(a.output/'backup.bin').write_bytes(combined)
        report={'device_id':device_id,'offset':0,'size':a.size,'sha256':hashlib.sha256(combined).hexdigest(),
                'chunk_size':0x10000,'esptool_md5_verified':True,'flash_written':False}
        (a.output/'backup.json').write_text(json.dumps(report,indent=2)+'\n')
        print('Backup complete; all chunks verified',flush=True)
    finally:
        if esp is not None:
            try:esp.hard_reset()
            finally:esp._port.close()


if __name__=='__main__':main()
