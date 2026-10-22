"""Offline config check. Live tests use cloud_replay.py and ECS billing."""
import argparse
import json
import os
from pathlib import Path
from cloud_setup import load_cloud_config, initialize_config


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env-file',type=Path)
    p.add_argument('--check',action='store_true')
    p.add_argument('--live',action='store_true',help='Retired: use authenticated cloud_replay.py')
    p.add_argument('--input',type=Path)
    p.add_argument('--output',type=Path)
    a=p.parse_args()
    if a.live:
        raise ValueError('Direct paid probe retired. Use scripts/cloud_replay.py so ECS enforces the budget and records all stage costs.')
    config=load_cloud_config(a.env_file.resolve() if a.env_file else None,require_credentials=False)
    initialize_config(config)
    from core.providers.asr.doubao_seed import ASRProvider
    from core.providers.llm.doubao_english.doubao_english import LLMProvider
    from core.providers.tts.doubao_seed import TTSProvider
    from core.websocket_server import WebSocketServer
    fields=('DOUBAO_SPEECH_API_KEY','ARK_API_KEY','ARK_MODEL_ID','DOUBAO_TTS_SPEAKER',
            'DAILY_TURN_LIMIT','DAILY_CNY_LIMIT')
    print(json.dumps({'imports_ok':True,'paid_calls':0,'missing_config':[x for x in fields if not os.getenv(x)],
                      'stage':'live verification pending','selected_module':config['selected_module']}))
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except ValueError as exc:raise SystemExit(str(exc)) from None
    except OSError:raise SystemExit('Private configuration file unreadable.') from None
