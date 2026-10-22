"""Create an explicit local-only override; no upstream cloud defaults selected."""
import argparse
from pathlib import Path
import yaml
root=Path(__file__).resolve().parents[1]
a=argparse.ArgumentParser();a.add_argument('--english', choices=['marian','mlx_qwen'],default='marian');a.add_argument('--asr',choices=['small','turbo'],default='small');a.add_argument('--host',default='127.0.0.1');a.add_argument('--port',type=int,default=8000);a.add_argument('--vad',choices=['off','silero'],default='off');args=a.parse_args()
config={
 'english_mvp': {'enabled':True, 'log_path':str(root/'logs/requests.jsonl')},
 'server':{'ip':args.host,'port':args.port,'auth_key':'local-mvp-not-an-internet-service','auth':{'enabled':False}},
 'selected_module':{'VAD':'SileroVAD' if args.vad=='silero' else None,'ASR':'LocalMLX','LLM':'LocalEnglish','TTS':'LocalKokoro','Memory':None,'Intent':None},
 'VAD':{'SileroVAD':{'type':'silero','model_dir':str(root/'upstream/backend/main/xiaozhi-server/models/snakers4_silero-vad'),'threshold':0.5,'threshold_low':0.3,'min_silence_duration_ms':800}},
 'ASR':{'LocalMLX':{'type':'local_mlx','engine':'mlx_whisper','model_path':str(root/'models'/('asr-mlx' if args.asr=='small' else 'asr-mlx-turbo')),'timeout_s':90}},
 'LLM':{'LocalEnglish':{'type':'local_english','engine':args.english,'model_path':str(root/'models'/('english-marian' if args.english=='marian' else 'english-qwen')),'timeout_s':90}},
 'TTS':{'LocalKokoro':{'type':'local_kokoro','engine':'kokoro','model_path':str(root/'models/tts-kokoro'),'timeout_s':90,'output_dir':'tmp/'}},
 'read_config_from_api':False,'manager-api':{'url':''},'delete_audio':True,
 'enable_stop_tts_notify':False,'enable_greeting':False,'enable_wakeup_words_response_cache':False,
 'close_connection_no_voice_time':3600,'exit_commands':[], 'mcp_endpoint':None,
 'log':{'log_dir':str(root/'logs'),'log_level':'INFO'},
}
# Upstream deep merge keeps unspecified selected modules. Explicit None requires filtering at initialization.
path=root/'upstream/backend/main/xiaozhi-server/data/.config.yaml';path.parent.mkdir(exist_ok=True);path.write_text(yaml.safe_dump(config,allow_unicode=True))
print(path)
