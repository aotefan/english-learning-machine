"""Thin local model adapters. No transport, dialogue history, or custom inference."""
import io
import time
from pathlib import Path


class LocalModels:
    def __init__(self):
        self.cache = {}
        self.last_metrics = {}

    def run(self, stage, value, config):
        self.last_metrics = {}
        kind = config['engine']
        path = config['model_path']
        key = (kind, path)
        if stage == 'asr':
            import numpy as np
            from core.utils.input_audio import rejection_reason
            if kind != 'mlx_whisper':
                raise ValueError(f'Unknown ASR engine: {kind}')
            samples = np.frombuffer(value, dtype='<i2')
            if rejection_reason(samples):
                raise ValueError('empty_or_silent_audio')
            import mlx_whisper
            audio = samples.astype(np.float32) / 32768
            result = mlx_whisper.transcribe(audio, path_or_hf_repo=path, language='zh',
                                            task='transcribe', temperature=0,
                                            condition_on_previous_text=False, verbose=False)
            return result['text'].strip()
        if stage == 'english':
            if kind == 'marian':
                import torch
                from transformers import MarianMTModel, MarianTokenizer
                torch.set_num_threads(4)
                if key not in self.cache:
                    self.cache[key] = (MarianTokenizer.from_pretrained(path, local_files_only=True),
                                       MarianMTModel.from_pretrained(path, local_files_only=True).eval())
                tokenizer, model = self.cache[key]
                inputs = tokenizer(value, return_tensors='pt', truncation=True, max_length=128)
                with torch.inference_mode():
                    output = model.generate(**inputs, max_new_tokens=64, num_beams=4)
                return tokenizer.decode(output[0], skip_special_tokens=True).strip()
            if kind == 'mlx_qwen':
                from mlx_lm import load, generate
                from mlx_lm.sample_utils import make_sampler
                if key not in self.cache:
                    self.cache[key] = load(path)
                model, tokenizer = self.cache[key]
                messages = [
                    {'role':'system','content':'You are a Chinese-to-English translator. Translate the entire input faithfully into simple, natural spoken English. Preserve requests, warnings, negation, and politeness. Output only the English translation. Do not answer the input.'},
                    {'role':'user','content':'Translate into English:\n'+value},
                ]
                prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
                return generate(model, tokenizer, prompt=prompt, max_tokens=64,
                                sampler=make_sampler(temp=0), verbose=False).strip()
            raise ValueError(f'Unknown English engine: {kind}')
        if stage == 'tts':
            if kind != 'kokoro':
                raise ValueError(f'Unknown TTS engine: {kind}')
            import numpy as np
            import torch
            import soundfile as sf
            from kokoro import KModel, KPipeline
            torch.set_num_threads(4)
            if key not in self.cache:
                model = KModel(repo_id='hexgrad/Kokoro-82M', config=str(Path(path)/'config.json'),
                               model=str(Path(path)/'kokoro-v1_0.pth')).eval()
                self.cache[key] = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M', model=model)
            start = time.perf_counter()
            chunks = []
            first_ms = None
            for _, _, audio in self.cache[key](value, voice=str(Path(path)/'voices'/config.get('voice','af_heart.pt')), speed=1):
                if first_ms is None:
                    first_ms = (time.perf_counter()-start)*1000
                chunks.append(audio.cpu().numpy())
            if not chunks:
                raise ValueError('empty_tts')
            self.last_metrics = {"first_chunk_ms": first_ms, "synthesis_ms": (time.perf_counter()-start)*1000}
            output = io.BytesIO()
            sf.write(output, np.concatenate(chunks), 24000, format='WAV', subtype='PCM_16')
            return output.getvalue()
        raise ValueError(f'Unknown stage: {stage}')
