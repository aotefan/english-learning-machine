"""Collect firmware codec-output evidence; requires the user's actual serial port."""
import argparse,json,time
from pathlib import Path
import serial
root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--port',required=True);p.add_argument('--baud',type=int,default=115200);args=p.parse_args()
(root/'logs').mkdir(exist_ok=True)
with serial.Serial(args.port,args.baud,timeout=1) as device, (root/'logs/playback.jsonl').open('a') as out:
    try:
        while True:
            line=device.readline().decode(errors='replace').strip()
            if line:print(line,flush=True)
            start=line.find('{"event":"playback_started"')
            if start>=0:
                record=json.JSONDecoder().raw_decode(line[start:])[0]
                record['host_received_at']=time.time()
                out.write(json.dumps(record)+'\n');out.flush()
    except KeyboardInterrupt:pass
