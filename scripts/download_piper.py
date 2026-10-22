import urllib.request,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]; folder=root/'models/piper';folder.mkdir(exist_ok=True)
repo='rhasspy/piper-voices'
meta=json.load(urllib.request.urlopen('https://huggingface.co/api/models/'+repo)); (root/'research/rhasspy--piper-voices.json').write_text(json.dumps(meta,indent=2))
for file in ['en_US-ljspeech-medium.onnx','en_US-ljspeech-medium.onnx.json','MODEL_CARD']:
 url=f"https://huggingface.co/{repo}/resolve/{meta['sha']}/en/en_US/ljspeech/medium/{file}"
 urllib.request.urlretrieve(url,folder/file)
 print(file,flush=True)
