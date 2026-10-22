"""Run the actual lite audio engine on the host; stub only hardware/wake APIs."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ManualCaptureTest(unittest.TestCase):
    def test_partial_frame_exact_frame_and_next_recording(self):
        compiler = shutil.which("c++")
        if not compiler:
            self.skipTest("C++ compiler unavailable")
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            (tmp / "model_path.h").write_text("""#pragma once
struct srmodel_list_t {};
inline const char* esp_srmodel_filter(srmodel_list_t*, const char*, void*) { return nullptr; }
""")
            (tmp / "esp_wn_models.h").write_text('#define ESP_WN_PREFIX "wn"\n')
            (tmp / "esp_log.h").write_text("""#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
""")
            # Keep production engine code intact; the stand-ins below have no
            # buffering or flushing logic and never activate a wake model.
            (tmp / "hardware.h").write_text("""#include <cstdint>
#include <functional>
#include <string>
#include <vector>
#include "model_path.h"
#define _AUDIO_CODEC_H
#define ESP_WAKE_WORD_H
class AudioCodec {
public:
    int channels = 1;
    int input_channels() const { return channels; }
};
class EspWakeWord {
    std::string word;
public:
    bool Initialize(AudioCodec*, srmodel_list_t*) { return true; }
    void Feed(const std::vector<int16_t>&) {}
    void OnWakeWordDetected(std::function<void(const std::string&)>) {}
    void Start() {}
    void Stop() {}
    size_t GetFeedSize() { return 160; }
    void EncodeWakeWordData() {}
    bool GetWakeWordOpus(std::vector<uint8_t>&) { return false; }
    const std::string& GetLastDetectedWakeWord() const { return word; }
};
""")
            (tmp / "test.cc").write_text("""#include <algorithm>
#include <cassert>
#include "engines/lite_audio_engine.h"
int main() {
    AudioCodec codec;
    LiteAudioEngine engine;
    assert(engine.Initialize(&codec, 60, nullptr));
    std::vector<std::vector<int16_t>> frames;
    engine.OnOutput([&](std::vector<int16_t>&& pcm) { frames.push_back(std::move(pcm)); });
    engine.EnableVoiceProcessing(true);
    for (int i = 0; i < 7; ++i) engine.Feed(std::vector<int16_t>(160, 123));
    assert(frames.size() == 1 && frames[0].size() == 960);
    engine.FlushRawAudio();
    assert(frames.size() == 2 && frames[1].size() == 960);
    assert(std::all_of(frames[1].begin(), frames[1].begin()+160, [](auto x){return x==123;}));
    assert(std::all_of(frames[1].begin()+160, frames[1].end(), [](auto x){return x==0;}));
    engine.FlushRawAudio();
    assert(frames.size() == 2); // Repeated release cannot emit phantom audio.
    engine.EnableVoiceProcessing(false);
    engine.EnableVoiceProcessing(true);
    engine.Feed(std::vector<int16_t>(960, 456));
    engine.FlushRawAudio();
    assert(frames.size() == 3);
    assert(std::all_of(frames[2].begin(), frames[2].end(), [](auto x){return x==456;}));
    engine.Feed(std::vector<int16_t>(160, 999));
    engine.EnableVoiceProcessing(false); // Abort drops its tail.
    engine.FlushRawAudio();
    assert(frames.size() == 3);
    codec.channels = 2;
    engine.EnableVoiceProcessing(true);
    std::vector<int16_t> stereo;
    for (int i = 0; i < 160; ++i) { stereo.push_back(42); stereo.push_back(-42); }
    engine.Feed(std::move(stereo));
    engine.FlushRawAudio();
    assert(frames.size() == 4 && frames.back().size() == 960);
    assert(std::all_of(frames.back().begin(), frames.back().begin()+160, [](auto x){return x==42;}));
    assert(std::all_of(frames.back().begin()+160, frames.back().end(), [](auto x){return x==0;}));
}
""")
            binary = tmp / "capture-test"
            subprocess.run([compiler, "-std=c++17", "-pthread", "-I", str(tmp),
                            "-I", str(ROOT / "main/audio"), "-include", str(tmp / "hardware.h"),
                            str(ROOT / "main/audio/engines/lite_audio_engine.cc"),
                            str(tmp / "test.cc"), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True, capture_output=True)
