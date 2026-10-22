"""Cloud-only configuration. Values come from a private env file, never firmware."""
import os
import sys
from pathlib import Path
import yaml
from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT/'upstream/backend/main/xiaozhi-server'


def load_cloud_config(env_file=None, require_credentials=True):
    if env_file:
        path = Path(env_file)
        if path.stat().st_mode & 0o077:raise ValueError('Private env file must have mode 600')
        load_dotenv(path, override=False, interpolate=False)
    names = ('DOUBAO_SPEECH_API_KEY','ARK_API_KEY','ARK_MODEL_ID','DOUBAO_TTS_SPEAKER')
    missing = [name for name in names if not os.getenv(name)]
    if require_credentials and missing:raise ValueError('Missing configuration: '+', '.join(missing))
    config = yaml.safe_load((SERVER/'config.yaml').read_text())
    config.update(english_mvp={'enabled':True,'log_path':str(ROOT/'logs/cloud-requests.jsonl')},
                  read_config_from_api=False, delete_audio=True, enable_greeting=False,
                  enable_wakeup_words_response_cache=False, enable_stop_tts_notify=False,
                  exit_commands=[], mcp_endpoint=None)
    config['manager-api']={'url':''}
    config['selected_module']={'ASR':'SeedASR2','LLM':'DoubaoEnglish','TTS':'SeedTTS2'}
    speech = os.getenv('DOUBAO_SPEECH_API_KEY','offline-test-only')
    config['ASR']={'SeedASR2':{'type':'doubao_seed','api_key':speech,'timeout_s':25,
                            'hotwords':['桂圆干','龙眼干','多多益善']}}
    config['LLM']={'DoubaoEnglish':{'type':'doubao_english', 'api_key':os.getenv('ARK_API_KEY','offline-test-only'),
                   'model_name':os.getenv('ARK_MODEL_ID','doubao-seed-2-0-mini-260428'),
                   'timeout_s':20, 'streaming':True, 'meaning_requests_as_translation':True}}
    config['TTS']={'SeedTTS2':{'type':'doubao_seed','api_key':speech,
                   'speaker':os.getenv('DOUBAO_TTS_SPEAKER','offline-test-only'),
                   'help_speaker':os.getenv('DOUBAO_TTS_HELP_SPEAKER','zh_female_vv_uranus_bigtts'),
                   'timeout_s':25, 'bidirectional':True}}
    config['VAD']={}
    # Stage two is a local diagnostic, not an unauthenticated public server.
    config['server'].update(ip='127.0.0.1',port=18000,auth_key='offline-test-only',auth={'enabled':False})
    config['log'].update(log_dir=str(ROOT/'logs'), log_level='INFO')
    return config


def initialize_config(config):
    sys.path.insert(0,str(SERVER))
    os.chdir(SERVER)
    from core.utils.cache.manager import cache_manager, CacheType
    cache_manager.set(CacheType.CONFIG,'main_config',config)
    from config import settings
    settings.config_file_valid = True  # Explicit config above already validated.
