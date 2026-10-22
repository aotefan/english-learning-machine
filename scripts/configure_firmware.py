"""Create an upstream build variant only after board identity is supplied. No GPIOs."""
import argparse,json
from pathlib import Path
from urllib.parse import urlparse
root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--board',required=True);p.add_argument('--variant',required=True);p.add_argument('--url',required=True);args=p.parse_args()
url=urlparse(args.url)
if url.scheme not in ('ws','wss') or not url.hostname or any(c in args.url for c in '\n\r"'):
    p.error('provide an actual Mac WebSocket URL')
base=root/'upstream/firmware/main/boards';board=(base/args.board).resolve()
if not board.is_relative_to(base.resolve()):p.error('board must be under upstream main/boards')
config=json.loads((board/'config.json').read_text());matches=[b for b in config['builds'] if b['name']==args.variant]
if len(matches)!=1:p.error('variant must exactly match one upstream build name')
build=matches[0];build['name']+='-english-mvp'
lines=build.get('sdkconfig_append',[])
lines=[line for line in lines if not any(key in line for key in ['CONFIG_USE_AUDIO_PROCESSOR','CONFIG_USE_DEVICE_AEC','CONFIG_USE_SERVER_AEC','CONFIG_ENGLISH_MVP'])]
lines+=['CONFIG_ENGLISH_MVP_LOCAL=y',f'CONFIG_ENGLISH_MVP_WS_URL="{args.url}"','CONFIG_USE_AUDIO_PROCESSOR=n','CONFIG_USE_DEVICE_AEC=n','CONFIG_USE_SERVER_AEC=n']
build['sdkconfig_append']=lines;config['builds']=[build]
path=board/'config.english-mvp.json'
if path.exists():p.error(f'{path} already exists; inspect it instead of overwriting')
path.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n')
print('Created',path);print('From upstream/firmware run:')
print(f'python scripts/build.py {args.board} --config config.english-mvp.json --name {build["name"]} --wake-word disabled')
