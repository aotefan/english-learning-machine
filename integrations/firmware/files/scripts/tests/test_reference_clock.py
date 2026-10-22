"""Exercise actual codec/power methods; hardware/AEC quality is a separate test."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ReferenceClockTests(unittest.TestCase):
    def test_listening_starts_master_clock_before_reference_and_keeps_it_during_idle(self):
        compiler = shutil.which('c++')
        if not compiler:
            self.skipTest('C++ compiler unavailable')
        source = (ROOT/'main/boards/english-edge-s3/reference_audio_codec.h').read_text()
        start = source.index('    void EnableInput(bool enable) override {')
        method = source[start:source.index('\n    }', start)+6]
        start = source.index('        input_reference_ = true;')
        setup = source[start:source.index('        i2s_chan_config_t channel', start)]
        base = (ROOT/'main/audio/codecs/no_audio_codec.cc').read_text()
        methods = []
        for name in ('EnableInput', 'EnableOutput'):
            start = base.index(f'void NoAudioCodec::{name}(')
            methods.append(base[start:base.index('\n}', start)+2])
        service = (ROOT/'main/audio/audio_service.cc').read_text()
        start = service.index('void AudioService::CheckAndUpdateAudioPowerState()')
        power = service[start:service.index('\n}', start)+2]
        harness = r'''
#include <cassert>
#include <atomic>
#include <chrono>
#include <mutex>
#include <vector>
#define ESP_ERROR_CHECK(x) assert((x)==0)
#define ESP_LOGI(...) ((void)0)
#define TAG "fixture"
#define AUDIO_POWER_TIMEOUT_MS 1000
#define AS_EVENT_AUDIO_INPUT_STOP_REQUEST 1
bool enabled[4]={};std::vector<int> starts;
int i2s_channel_enable(int channel){if(channel==3)assert(enabled[1]);enabled[channel]=true;starts.push_back(channel);return 0;}
int i2s_channel_disable(int channel){enabled[channel]=false;return 0;}
void esp_ae_rate_cvt_reset(int){}
void xEventGroupSetBits(int,int){}
void esp_timer_stop(int){}
class AudioCodec {public:bool input_enabled_=false,output_enabled_=false,input_reference_=false,duplex_=false;
 virtual void EnableInput(bool value){input_enabled_=value;}virtual void EnableOutput(bool value){output_enabled_=value;}
 bool input_enabled(){return input_enabled_;}bool output_enabled(){return output_enabled_;}bool duplex(){return duplex_;}};
class NoAudioCodec:public AudioCodec {protected:std::mutex data_if_mutex_;int rx_handle_=2,tx_handle_=1;
 public:void EnableInput(bool) override;void EnableOutput(bool) override;};
class Reference:public NoAudioCodec {public:int reference_rx_=3,resampler_=0;bool reference_enabled_=false;
 std::atomic<unsigned> overruns_{0};unsigned observed_overruns_=0;
 Reference(){SETUP}METHOD};
class AudioService {public:AudioCodec* codec_;int event_group_=0,audio_power_timer_=0;
 std::mutex audio_queue_mutex_;bool playback_drained_=true;
 bool IsPlaybackDrainedLocked(){return playback_drained_;}
 std::chrono::steady_clock::time_point last_input_time_,last_output_time_;
 void CheckAndUpdateAudioPowerState();};
int main(){Reference codec;assert(codec.duplex()&&codec.input_reference_);
 codec.EnableInput(true);assert((starts==std::vector<int>{1,3,2}));
 AudioService service;service.codec_=&codec;service.last_input_time_=std::chrono::steady_clock::now();service.last_output_time_=service.last_input_time_-std::chrono::seconds(10);
 service.CheckAndUpdateAudioPowerState();assert(enabled[1]&&enabled[2]&&enabled[3]);
 codec.EnableInput(true);assert(starts.size()==3);
 codec.EnableInput(false);assert(!enabled[2]&&!enabled[3]);service.CheckAndUpdateAudioPowerState();assert(!enabled[1]);
 // Exercise the real idle-power method with an old timestamp but active
 // output: queued/ongoing playback must retain its TX clock.
 codec.EnableOutput(true);service.playback_drained_=false;
 service.CheckAndUpdateAudioPowerState();assert(enabled[1]);
 service.playback_drained_=true;service.CheckAndUpdateAudioPowerState();assert(!enabled[1]);
 starts.clear();codec.EnableInput(true);assert((starts==std::vector<int>{1,3,2}));
}
'''.replace('SETUP', setup).replace('METHOD', method)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder); cpp = path/'clock.cc'; binary = path/'clock'
            # Definitions must follow declarations, and precede the test entry.
            before, main = harness.split('int main()', 1)
            cpp.write_text(before+'\n'+'\n'.join(methods)+'\n'+power+'\nint main()'+main)
            subprocess.run([compiler,'-std=c++17',str(cpp),'-o',str(binary)],check=True,capture_output=True)
            subprocess.run([str(binary)],check=True,capture_output=True)
