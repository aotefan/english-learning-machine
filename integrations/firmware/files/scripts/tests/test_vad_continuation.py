"""Run the production AFE result handler on synthetic PCM and controlled VAD labels."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class VadContinuationTest(unittest.TestCase):
    def test_weak_continuation_cancels_silence_without_weak_idle_triggers(self):
        compiler = shutil.which("c++")
        if not compiler:
            self.skipTest("C++ compiler unavailable")
        source = (ROOT / "main/audio/engines/afe_audio_engine.cc").read_text()
        start = source.index("void AfeAudioEngine::HandleVoiceResult(")
        end = source.index("\n}\n", start) + len("\n}\n")
        handler = source[start:end]
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            test = tmp / "test.cc"
            test.write_text(r'''
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <deque>
#include <functional>
#include <vector>
#include "engines/input_warmup.h"
#define CONFIG_ENGLISH_MVP_CLOUD 1
#define CONFIG_USE_AUDIO_PROCESSOR 1
#define ESP_LOGI(...) ((void)0)
int64_t now_us = 1000000;
int64_t esp_timer_get_time() { return now_us; }
enum { VAD_SILENCE, VAD_SPEECH };
struct afe_fetch_result_t {
    int16_t* data;
    int data_size;
    int vad_state;
};
class AfeAudioEngine {
public:
    struct VadEnergyChunk { uint64_t squares; int64_t sum; size_t samples; };
    std::deque<VadEnergyChunk> vad_energy_chunks_;
    uint64_t vad_energy_sum_ = 0;
    int64_t vad_sample_sum_ = 0;
    size_t vad_energy_samples_ = 0;
    bool vad_energy_rejected_ = false, vad_speech_admitted_ = false;
    int64_t vad_last_reject_log_us_ = 0;
    InputWarmup input_warmup_;
    bool is_speaking_ = false;
    std::atomic<bool> output_reset_pending_{true};
    int frame_samples_ = 960;
    std::vector<int16_t> output_buffer_;
    std::function<void(bool)> vad_state_change_callback_;
    std::function<void(std::vector<int16_t>&&)> output_callback_;
    void HandleVoiceResult(const afe_fetch_result_t* result);
};
''' + handler + r'''
int main() {
    AfeAudioEngine engine;
    bool capturing = false;
    int starts = 0, endings = 0, output_frames = 0;
    int64_t silence_since = 0;
    engine.vad_state_change_callback_ = [&](bool speaking) {
        if (speaking) {
            silence_since = 0;
            if (!capturing) { capturing = true; ++starts; }
        } else if (capturing && silence_since == 0) {
            silence_since = now_us;
        }
    };
    engine.output_callback_ = [&](std::vector<int16_t>&& pcm) {
        assert(pcm.size() == 960);
        ++output_frames;
    };
    // No speech recording or model call: alternate signed synthetic samples.
    // VAD labels are injected, so this tests the firmware gate, not ESP-SR accuracy.
    auto feed = [&](int amplitude, int state, int chunks) {
        for (int n = 0; n < chunks; ++n) {
            std::vector<int16_t> pcm(320);
            for (size_t i = 0; i < pcm.size(); ++i) pcm[i] = i % 2 ? amplitude : -amplitude;
            now_us += 20000;
            afe_fetch_result_t result{pcm.data(), int(pcm.size() * sizeof(int16_t)), state};
            engine.HandleVoiceResult(&result);
            if (capturing && silence_since && now_us - silence_since >= 1200000) {
                ++endings;
                capturing = false;
                silence_since = 0;
            }
        }
    };

    engine.input_warmup_.OnInputEnabled();
    feed(4000, VAD_SPEECH, 32); // Cold input still rejects even loud candidates.
    assert(starts == 0);
    feed(0, VAD_SILENCE, 250);
    feed(500, VAD_SPEECH, 40); // Warm idle noise/soft playback tail cannot open a turn.
    assert(starts == 0);

    feed(4000, VAD_SPEECH, 32);
    assert(starts == 1 && capturing);
    feed(0, VAD_SILENCE, 40); // A sub-threshold 800 ms pause while thinking.
    assert(capturing && silence_since != 0);
    feed(500, VAD_SPEECH, 40); // A quieter continuation lasts beyond the old silence deadline.
    assert(capturing && endings == 0 && silence_since == 0);
    feed(0, VAD_SILENCE, 40);
    feed(500, VAD_SPEECH, 40); // Repeated weak continuation must also keep the same turn.
    assert(capturing && starts == 1 && endings == 0);
    feed(0, VAD_SILENCE, 61); // Genuine silence keeps the existing 1200 ms end policy.
    assert(!capturing && endings == 1);
    assert(output_frames > 0);

    // Match EnableVoiceProcessing(false) and its deferred output reset, then rearm.
    engine.is_speaking_ = false;
    engine.output_reset_pending_ = true;
    feed(500, VAD_SPEECH, 40);
    assert(starts == 1 && !capturing); // No admitted-speech latch leaks into the next turn.
    feed(4000, VAD_SPEECH, 32);
    assert(starts == 2 && capturing); // New turn still starts normally on qualifying speech.
    engine.input_warmup_.OnInputEnabled();
    feed(0, VAD_SILENCE, 1);
    feed(4000, VAD_SPEECH, 32);
    assert(!engine.is_speaking_); // A physical input restart still requires settling.
}
''')
            binary = tmp / "vad-continuation-test"
            subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-variable", "-I", str(ROOT / "main/audio"),
                            str(test), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
