"""Synthetic ADULT/system voice fixtures; never count as child/hardware acceptance."""
import argparse, json, subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--count', type=int, choices=range(1,21), default=20)
parser.add_argument('--output', type=Path, default=root/'artifacts/synthetic')
args=parser.parse_args()
folder=args.output.resolve(); folder.mkdir(parents=True, exist_ok=True)
for row in json.loads((root/'benchmark/sentences.json').read_text())[:args.count]:
    aiff=folder/(row['id']+'.aiff'); wav=folder/(row['id']+'.wav')
    subprocess.run(['say','-v','Tingting','-r','155','-o',str(aiff),row['zh']],check=True)
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(aiff),'-ar','16000','-ac','1','-c:a','pcm_s16le',str(wav)],check=True)
    aiff.unlink()
print(f'{args.count} synthetic fixtures generated')
