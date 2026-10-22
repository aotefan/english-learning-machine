"""Startup must reject broken/misdirected local setup before upstream imports."""
import copy
import math
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from local_setup import validate_local_config, apply_vad_pause


class LocalSetupTests(unittest.TestCase):
    def test_pause_changes_only_selected_silero_parameter(self):
        config = {'selected_module': {'VAD':'local'}, 'ASR': {'model':'unchanged'},
                  'VAD': {'local': {'type':'silero', 'threshold':.5, 'min_silence_duration_ms':800}}}
        expected = copy.deepcopy(config)
        expected['VAD']['local']['min_silence_duration_ms'] = 1400
        apply_vad_pause(config, 1400)
        self.assertEqual(config, expected)
        for invalid in (True, 0, 1400.0, '1400', 999999):
            with self.assertRaises(ValueError):apply_vad_pause(config, invalid)
        with self.assertRaisesRegex(ValueError, 'Silero'):
            apply_vad_pause({'selected_module':{'VAD':None}}, 1400)

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        self.path=self.root/'data/.config.yaml'
        self.path.parent.mkdir()
        self.config={'english_mvp':{'enabled':True},'server':{'port':8000},
                     'selected_module':{'ASR':'asr','LLM':'english','TTS':'tts'}}
        for stage,name,kind,engine,files in [
            ('ASR','asr','local_mlx','mlx_whisper',['config.json','weights.npz']),
            ('LLM','english','local_english','marian',['config.json','source.spm','target.spm','vocab.json','pytorch_model.bin']),
            ('TTS','tts','local_kokoro','kokoro',['config.json','kokoro-v1_0.pth','voices/af_heart.pt'])]:
            model=self.root/name
            for file in files:
                dest=model/file;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(b'fixture')
            self.config[stage]={name:{'type':kind,'engine':engine,'model_path':str(model),'timeout_s':90}}

    def check(self,config=None):
        self.path.write_text(yaml.safe_dump(config if config is not None else self.config))
        return validate_local_config(self.path)

    def test_valid_selected_models_do_not_require_unused_qwen(self):
        modules=self.check()
        self.assertIn('mlx_whisper',modules)
        self.assertNotIn('mlx_lm',modules)

    def test_missing_config_and_invalid_yaml_are_actionable(self):
        with self.assertRaisesRegex(ValueError,'configure.py'):validate_local_config(self.path)
        self.path.write_text('broken: [')
        with self.assertRaisesRegex(ValueError,'Invalid YAML'):validate_local_config(self.path)

    def test_missing_or_empty_selected_weights_fail_before_inference(self):
        weight=self.root/'asr/weights.npz';weight.unlink()
        with self.assertRaisesRegex(ValueError,'ASR: missing/empty'):self.check()
        weight.touch()
        with self.assertRaisesRegex(ValueError,'ASR: missing/empty'):self.check()

    def test_relative_model_path_and_custom_voice(self):
        self.config['TTS']['tts']['model_path']='tts'
        self.config['TTS']['tts']['voice']='missing.pt'
        with self.assertRaisesRegex(ValueError,'voices/missing.pt'):self.check()
        (self.root/'tts/voices/missing.pt').write_bytes(b'fixture')
        self.check()

    def test_remote_config_and_nonlocal_provider_are_rejected(self):
        for field,value in [('manager-api',{'url':'https://example.invalid'}),('read_config_from_api',True)]:
            with self.subTest(field=field):
                config=copy.deepcopy(self.config);config[field]=value
                with self.assertRaisesRegex(ValueError,'manager API'):self.check(config)
        self.config['ASR']['asr']['type']='unconfigured_remote'
        with self.assertRaisesRegex(ValueError,'unsupported local provider'):self.check()

    def test_invalid_port_and_unbounded_deadline(self):
        for port in (0,65536,True,'8000'):
            with self.subTest(port=port):
                config=copy.deepcopy(self.config);config['server']['port']=port
                with self.assertRaisesRegex(ValueError,'server.port'):self.check(config)
        for timeout in (0,-1,math.inf,math.nan,True):
            with self.subTest(timeout=timeout):
                self.config['ASR']['asr']['timeout_s']=timeout
                with self.assertRaisesRegex(ValueError,'timeout_s'):self.check()
