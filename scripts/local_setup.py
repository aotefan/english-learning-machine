"""Validate this MVP's installed local providers before upstream imports/config loading."""
import importlib.util
import math
from pathlib import Path

import yaml

MODELS = {
    ('ASR', 'local_mlx', 'mlx_whisper'): (
        ['config.json'], [('weights.npz', 'weights.safetensors')], ['mlx_whisper']),
    ('LLM', 'local_english', 'marian'): (
        ['config.json', 'source.spm', 'target.spm', 'vocab.json'],
        [('pytorch_model.bin', 'model.safetensors')], ['torch', 'transformers']),
    ('LLM', 'local_english', 'mlx_qwen'): (
        ['config.json', 'tokenizer.json', 'tokenizer_config.json', 'model.safetensors'],
        [], ['mlx_lm']),
    ('TTS', 'local_kokoro', 'kokoro'): (
        ['config.json', 'kokoro-v1_0.pth'], [], ['kokoro', 'en_core_web_sm']),
}

VAD_PAUSES = {'standard': 800, 'slow': 1400}


def apply_vad_pause(config, milliseconds):
    """Adjust only the selected upstream Silero endpoint parameter."""
    if milliseconds is None:
        return
    if type(milliseconds) is not int or milliseconds not in VAD_PAUSES.values():
        raise ValueError('VAD pause must be 800 (standard) or 1400 (slow) ms.')
    selected = config.get('selected_module', {}).get('VAD')
    provider = config.get('VAD', {}).get(selected, {})
    if provider.get('type') != 'silero':
        raise ValueError('Pause profiles require the existing Silero VAD to be enabled.')
    provider['min_silence_duration_ms'] = milliseconds


def validate_local_config(path, port_override=None, vad_pause_override=None):
    path=Path(path)
    if not path.is_file():
        raise ValueError('Local config missing. Run .venv/bin/python scripts/configure.py first.')
    try:
        config=yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError('Invalid YAML in local config; correct it or run scripts/configure.py.') from exc
    if not isinstance(config,dict):
        raise ValueError('Local config must be a YAML mapping.')
    apply_vad_pause(config, vad_pause_override)
    try:
        if config.get('english_mvp',{}).get('enabled') is not True:
            raise ValueError('english_mvp.enabled must be true for this launcher.')
        if config.get('manager-api',{}).get('url') or config.get('read_config_from_api',False):
            raise ValueError('Use the local config from scripts/configure.py; manager API is not used by this MVP.')
        port=port_override if port_override is not None else config['server']['port']
        if isinstance(port,bool) or not isinstance(port,int) or not 1<=port<=65535:
            raise ValueError('server.port must be an integer from 1 to 65535.')
        required_modules=set()
        for stage in ('ASR','LLM','TTS'):
            selected=config['selected_module'][stage]
            provider=config[stage][selected]
            key=(stage,provider['type'],provider['engine'])
            if key not in MODELS:
                raise ValueError(f'{stage}: unsupported local provider {key[1:]}. Update the setup check when adding a provider.')
            timeout=provider.get('timeout_s',90)
            if isinstance(timeout,bool) or not math.isfinite(float(timeout)) or float(timeout)<=0:
                raise ValueError(f'{stage}: timeout_s must be positive and finite.')
            model=Path(provider['model_path'])
            if not model.is_absolute():model=path.parent.parent/model
            files,alternatives,modules=MODELS[key]
            files=list(files)
            if stage=='TTS':files.append(str(Path('voices')/provider.get('voice','af_heart.pt')))
            present=lambda name: (model/name).is_file() and (model/name).stat().st_size>0
            missing=[name for name in files if not present(name)]
            missing+=[' or '.join(names) for names in alternatives if not any(present(n) for n in names)]
            if missing:
                raise ValueError(f'{stage}: missing/empty model files in {model}: {", ".join(missing)}. '
                                 'Run scripts/download_models.py or correct model_path/voice.')
            required_modules.update(modules)
        vad = config.get('selected_module', {}).get('VAD')
        if vad:
            provider = config['VAD'][vad]
            if provider['type'] != 'silero':
                raise ValueError('Only the existing local Silero VAD is supported.')
            model = Path(provider['model_dir']) / 'src/silero_vad/data/silero_vad.onnx'
            if not model.is_absolute(): model = path.parent.parent / model
            if not model.is_file(): raise ValueError(f'Missing Silero model: {model}')
            required_modules.add('onnxruntime')
        return required_modules
    except (KeyError,TypeError,AttributeError) as exc:
        raise ValueError(f'Incomplete local provider config ({exc}); run scripts/configure.py.') from exc


def check_runtime(required_modules):
    missing=[name for name in sorted(required_modules) if importlib.util.find_spec(name) is None]
    if missing:
        raise ValueError('Missing Python packages: '+', '.join(missing)+
                         '. Run uv pip sync --python .venv/bin/python requirements-mac.lock.')
    # Reuse the upstream check and the installed codec/phonemizer implementations.
    from core.utils.util import check_ffmpeg_installed
    import opuslib_next
    from phonemizer.backend.espeak.wrapper import EspeakWrapper
    check_ffmpeg_installed()
    packet=opuslib_next.Encoder(16000,1,opuslib_next.APPLICATION_AUDIO).encode(bytes(1920),960)
    if len(opuslib_next.Decoder(16000,1).decode(packet,960))!=1920:
        raise ValueError('Opus roundtrip returned an unexpected PCM frame size.')
    return {'opus_roundtrip':True,'ffmpeg':True,'espeak_version':EspeakWrapper().version}
