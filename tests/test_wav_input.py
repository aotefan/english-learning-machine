"""Reject unusable recordings before sending any audio to the voice server."""
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
import wave

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from protocol_benchmark import read_input


class WavInputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)

    def write_wav(self,rate=16000,channels=1,width=2,frames=1600):
        output=io.BytesIO()
        with wave.open(output,'wb') as wav:
            wav.setframerate(rate);wav.setnchannels(channels);wav.setsampwidth(width)
            wav.writeframes(bytes(frames*channels*width))
        raw=output.getvalue();(self.folder/'01.wav').write_bytes(raw)
        return raw

    def test_valid_audio_retains_file_hash_and_duration(self):
        raw=self.write_wav()
        pcm,metadata=read_input(self.folder,'01')
        self.assertEqual(pcm,bytes(3200))
        self.assertEqual(metadata['input_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(metadata['input_duration_ms'],100)

    def test_wrong_format_is_rejected(self):
        for options in ({'rate':44100},{'channels':2},{'width':1}):
            with self.subTest(options=options):
                self.write_wav(**options)
                with self.assertRaisesRegex(ValueError,'16000 Hz'):read_input(self.folder,'01')

    def test_empty_short_long_and_oversized_are_rejected(self):
        for frames in (0,1599,240001):
            with self.subTest(frames=frames):
                self.write_wav(frames=frames)
                with self.assertRaisesRegex(ValueError,'duration'):read_input(self.folder,'01')
        (self.folder/'01.wav').write_bytes(bytes(1024*1024+1))
        with self.assertRaisesRegex(ValueError,'1 MiB'):read_input(self.folder,'01')

    def test_truncated_payload_is_rejected(self):
        raw=self.write_wav()
        (self.folder/'01.wav').write_bytes(raw[:-2])
        with self.assertRaisesRegex(ValueError,'Truncated'):read_input(self.folder,'01')

    def test_ids_cannot_escape_input_or_output_directory(self):
        for sample in ('../01','/tmp/01','a/b','',None):
            with self.subTest(sample=sample):
                with self.assertRaisesRegex(ValueError,'sample ID'):read_input(self.folder,sample)
