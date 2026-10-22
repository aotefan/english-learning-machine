import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'upstream/backend/main/xiaozhi-server'))
from core.utils.diagnostic_audio import DiagnosticAudioCapture


class CaptureTest(unittest.TestCase):
    def test_exact_audio_permissions_and_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = DiagnosticAudioCapture(directory, limit=1)
            pcm = b'\x01\x00\xff\xff' * 1600
            path = capture.save('a' * 32, [pcm[:100], pcm[100:]])
            with wave.open(str(path)) as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 16000))
                self.assertEqual(wav.readframes(wav.getnframes()), pcm)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(capture.save('b' * 32, [pcm]))
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_expired_capture_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = DiagnosticAudioCapture(directory)
            with patch('core.utils.diagnostic_audio.time.monotonic', return_value=capture.deadline):
                self.assertIsNone(capture.save('a' * 32, [b'\x00\x00']))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_pcm_and_filename_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = DiagnosticAudioCapture(directory)
            for rid, pcm in [('a' * 32, b''), ('a' * 32, b'1'), ('a' * 32, b'00' * 240001), ('../file', b'00')]:
                with self.assertRaises(ValueError):
                    capture.save(rid, [pcm])
            self.assertEqual(capture.remaining, 4)

    def test_existing_recording_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = DiagnosticAudioCapture(directory)
            path = capture.save('a' * 32, [b'12'])
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                capture.save('a' * 32, [b'34'])
            self.assertEqual(path.read_bytes(), original)
