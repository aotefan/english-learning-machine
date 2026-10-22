"""Exercise the actual application resume methods with hardware/transport stand-ins."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def application_method(name):
    source = (ROOT / "main/application.cc").read_text()
    start = source.index(f"void Application::{name}(")
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


class MvpPlaybackResumeTest(unittest.TestCase):
    def test_playback_and_input_drain_resume_without_clock_tick(self):
        compiler = shutil.which("c++")
        if not compiler:
            self.skipTest("C++ compiler unavailable")
        methods = "\n".join(application_method(name) for name in (
            "MaybeResumeMvpAutoListening", "HandleMvpPlaybackStop",
            "MaybeConfirmMvpPlayback", "ArmDeviceVad"))
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            source = tmp / "test.cc"
            source.write_text(r'''
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>
#include "cloud_turn_recovery.h"
#define CONFIG_ENGLISH_MVP_CLOUD 1
#define CONFIG_USE_AUDIO_PROCESSOR 1
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
int64_t now_us = 1000000;
int64_t esp_timer_get_time() { return now_us; }
enum DeviceState { kDeviceStateIdle, kDeviceStateSpeaking };
struct Audio {
    bool playback_idle = false;
    bool uplink_drained = true;
    bool running = false;
    int enables = 0;
    bool IsPlaybackIdle() { return playback_idle; }
    bool IsUplinkDrained() { return uplink_drained; }
    void FinishRawCapture() { uplink_drained = false; }
    void EnableVoiceProcessing(bool enable) {
        assert(playback_idle && uplink_drained);
        running = enable;
        ++enables;
    }
    bool IsAudioProcessorRunning() { return running; }
};
struct Protocol {
    bool opened = true;
    bool can_ack = true;
    int completions = 0;
    bool IsAudioChannelOpened() { return opened; }
    bool SendPlaybackCompleted(const std::string& request_id) {
        assert(request_id == "current");
        if (!can_ack) return false;
        ++completions;
        return true;
    }
};
class Application {
public:
    Protocol transport;
    Protocol* protocol_ = &transport;
    Audio audio_service_;
    CloudTurnRecovery mvp_cloud_recovery_;
    DeviceState state = kDeviceStateSpeaking;
    bool mvp_auto_mode_ = true, mvp_network_ready_ = true;
    bool mvp_connecting_ = false, mvp_waiting_response_ = true;
    bool mvp_stopping_ = false, mvp_capture_started_ = false;
    bool mvp_receiving_audio_ = true, mvp_playback_reset_pending_ = false;
    bool mvp_playback_started_ = true, mvp_playback_stop_received_ = false;
    bool mvp_vad_monitoring_ = false;
    int64_t mvp_auto_resume_at_us_ = 0, mvp_vad_silence_since_us_ = 0;
    std::string mvp_playback_request_id_ = "current";
    std::vector<int> mvp_vad_preroll_;
    DeviceState GetDeviceState() { return state; }
    void SetDeviceState(DeviceState next) { state = next; }
    void ResetDeviceVad() { mvp_vad_monitoring_ = false; }
    void MaybeResumeMvpAutoListening();
    void HandleMvpPlaybackStop(const std::string& request_id);
    void MaybeConfirmMvpPlayback();
    void ArmDeviceVad();
};
''' + methods + r'''
// These helpers deliver the same three events as the main loop/socket handler.
void Stop(Application& app) {
    app.mvp_receiving_audio_ = false;
    app.HandleMvpPlaybackStop("current");
}
void Drain(Application& app) {
    app.audio_service_.playback_idle = true;
    app.MaybeConfirmMvpPlayback();
}
void InputFinished(Application& app) {
    app.audio_service_.uplink_drained = true;
    app.MaybeResumeMvpAutoListening();
}
int main() {
    // Stop precedes the final PCM. Neither stop nor input ACK can truncate it.
    Application early_stop;
    Stop(early_stop);
    InputFinished(early_stop);
    assert(early_stop.audio_service_.enables == 0);
    Drain(early_stop);
    assert(early_stop.audio_service_.enables == 1);
    assert(early_stop.transport.completions == 1);
    assert(early_stop.mvp_auto_resume_at_us_ == 0);

    // Final PCM precedes stop. No confirmation or listening on a stream gap.
    Application late_stop;
    Drain(late_stop);
    late_stop.MaybeResumeMvpAutoListening();
    assert(late_stop.transport.completions == 0);
    assert(late_stop.audio_service_.enables == 0);
    Stop(late_stop);
    assert(late_stop.transport.completions == 1);
    assert(late_stop.audio_service_.enables == 0); // input stop still pending
    InputFinished(late_stop);
    assert(late_stop.audio_service_.enables == 1);

    // Both output events precede the input task's stop ACK. It wakes rearming.
    Application late_input;
    Stop(late_input);
    Drain(late_input);
    assert(late_input.audio_service_.enables == 0);
    InputFinished(late_input);
    assert(late_input.audio_service_.enables == 1);
    late_input.MaybeResumeMvpAutoListening();
    late_input.MaybeConfirmMvpPlayback();
    assert(late_input.audio_service_.enables == 1);
    assert(late_input.transport.completions == 1);
    assert(now_us == 1000000); // No 300 ms delay or 1 s clock tick was used.

    // A successful silent expression has no START, PCM or playback identity.
    // Its ordinary STOP still releases waiting/capture and permits rearming.
    Application silent;
    silent.state = kDeviceStateIdle;
    silent.mvp_receiving_audio_ = false;
    silent.mvp_playback_request_id_.clear();
    silent.mvp_playback_started_ = false;
    silent.audio_service_.playback_idle = true;
    silent.HandleMvpPlaybackStop("silent-request");
    assert(!silent.mvp_waiting_response_ && !silent.mvp_stopping_);
    assert(silent.mvp_auto_mode_);
    InputFinished(silent);
    assert(silent.transport.completions == 0);
    now_us += 300001; // existing no-playback quiet guard, no cloud error pause
    silent.MaybeResumeMvpAutoListening();
    assert(silent.audio_service_.enables == 1 && silent.mvp_vad_monitoring_);
    assert(silent.transport.completions == 0);
    now_us = 1000000;

    // A delayed started callback also finishes the same handshake immediately.
    Application late_started;
    late_started.mvp_playback_started_ = false;
    Stop(late_started);
    Drain(late_started);
    InputFinished(late_started);
    assert(late_started.audio_service_.enables == 0);
    late_started.mvp_playback_started_ = true;
    late_started.MaybeConfirmMvpPlayback();
    assert(late_started.audio_service_.enables == 1);

    // Successful completion cannot shorten a cloud failure or daily quota pause.
    for (int pause_ms : {3000, 60000, 86400000}) {
        Application paused;
        paused.mvp_cloud_recovery_.PauseAfterError(now_us, true, pause_ms);
        Stop(paused);
        InputFinished(paused);
        Drain(paused);
        assert(paused.audio_service_.enables == 0);
        assert(!paused.mvp_cloud_recovery_.CanListen(now_us + pause_ms * 1000LL - 1));
    }

    // BOOT exit, disconnected transport, and a new active turn all block resume.
    for (int blocked = 0; blocked < 6; ++blocked) {
        Application app;
        Stop(app);
        InputFinished(app);
        if (blocked == 0) app.mvp_auto_mode_ = false;
        if (blocked == 1) app.mvp_network_ready_ = false;
        if (blocked == 2) app.mvp_connecting_ = true;
        if (blocked == 3) app.transport.opened = false;
        if (blocked == 4) app.mvp_waiting_response_ = true;
        if (blocked == 5) app.mvp_capture_started_ = true;
        Drain(app);
        assert(app.audio_service_.enables == 0);
    }

    // Error/reset/discarded audio has no successful playback acknowledgement.
    Application discarded;
    discarded.mvp_playback_request_id_.clear();
    Stop(discarded);
    InputFinished(discarded);
    Drain(discarded);
    assert(discarded.transport.completions == 0);
    assert(discarded.audio_service_.enables == 0);
    assert(discarded.mvp_auto_resume_at_us_ == now_us + 300000);

    Application reset_pending;
    reset_pending.mvp_playback_reset_pending_ = true;
    Stop(reset_pending);
    InputFinished(reset_pending);
    Drain(reset_pending);
    assert(reset_pending.transport.completions == 0);
    assert(reset_pending.audio_service_.enables == 0);
}
''')
            binary = tmp / "mvp-playback-resume-test"
            subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "main"), str(source), "-o", str(binary)],
                           check=True)
            subprocess.run([str(binary)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
