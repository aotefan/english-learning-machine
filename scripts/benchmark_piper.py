import json,time,resource
from pathlib import Path
from piper import PiperVoice
root=Path(__file__).resolve().parents[1]
start=time.perf_counter();voice=PiperVoice.load(str(root/'models/piper/en_US-ljspeech-medium.onnx'));load_ms=(time.perf_counter()-start)*1000
rows=json.loads((root/'benchmark/sentences.json').read_text()); dest=root/'artifacts/piper';dest.mkdir(exist_ok=True)
with (root/'benchmark/tts-piper.jsonl').open('w') as out:
 for i,row in enumerate(rows):
  start=time.perf_counter();first=None;audio=[]
  for chunk in voice.synthesize(row['reference']):
   if first is None:first=(time.perf_counter()-start)*1000
   audio.append(chunk.audio_int16_bytes)
  record={'id':row['id'],'source':'text_corpus','candidate':'piper-ljspeech-medium','success':bool(audio),'cold':i==0,'load_ms':load_ms if i==0 else None,'latency_ms':(time.perf_counter()-start)*1000,'first_chunk_ms':first,'peak_rss_mb':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024/1024}
  print(json.dumps(record),flush=True);out.write(json.dumps(record)+'\n')
  import wave
  with wave.open(str(dest/f"{row['id']}.wav"),'wb') as wav:
   wav.setframerate(chunk.sample_rate);wav.setnchannels(chunk.sample_channels);wav.setsampwidth(chunk.sample_width);wav.writeframes(b''.join(audio))
