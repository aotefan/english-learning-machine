"""Exercise real read-only status and subtitle code with local hardware stand-ins."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOARD = ROOT / 'main/boards/english-edge-s3'


def method(path, signature):
    source = path.read_text()
    start = source.index(signature)
    end = source.index('\n}\n', start) + 3
    return source[start:end]


class LearningDisplayTests(unittest.TestCase):
    def test_notifications_do_not_hide_listening_status(self):
        actual = method(BOARD / 'learning_display.cc', 'void LearningDisplay::ShowNotification(const char*')
        self.compile_run(r'''#include <cassert>
#include <string>
#include "learning_display_text.h"
struct Label { bool hidden=false; bool foreground=false; };
constexpr int LV_OBJ_FLAG_HIDDEN=1;
void lv_obj_remove_flag(Label* label, int) { label->hidden=false; }
void lv_obj_move_foreground(Label* label) { label->foreground=true; }
struct LvglDisplay { Label status, notice; Label* status_label_=&status;
    Label* notification_label_=&notice; std::string message;
    void ShowNotification(const char* s,int) { message=s; status.hidden=true; }
};
struct LearningDisplay : LvglDisplay { void ShowNotification(const char*,int); };
struct DisplayLockGuard { DisplayLockGuard(LearningDisplay*) {} operator bool() const { return true; } };
''' + actual + r'''int main() {
    LearningDisplay display;
    display.ShowNotification("正在连接网络",30000);
    assert(!display.status.hidden && display.notice.foreground);
    assert(display.message=="正在连接网络");
}
''', (BOARD,))

    def test_labels_distinguish_listening_and_confirmed_teaching_modes(self):
        self.compile_run(r'''#include <cassert>
#include <cstring>
#include "learning_display_labels.h"
int main() {
    using namespace LearningDisplayLabels;
    assert(!strcmp(Status("Listening"), "正在监听"));
    assert(!strcmp(Status("Recording"), "正在录音"));
    assert(!strcmp(Status("Ready"), "未在监听"));
    assert(!strcmp(Status("Paused"), "等待恢复"));
    assert(!strcmp(Status("Playing"), "播放中"));
    assert(!strcmp(Mode("chat"), "陪练模式"));
    assert(!strcmp(Mode("express"), "表达模式"));
    assert(!strcmp(Mode(nullptr), "模式同步中"));
    assert(!strcmp(Mode("unknown"), "模式同步中"));
}
''', (BOARD,))

    def test_server_mode_update_rejects_invalid_values_without_changing_audio(self):
        actual = method(ROOT / 'main/application.cc', 'void Application::UpdateMvpTeachingMode(')
        self.compile_run(r'''#include <cassert>
#include <cstring>
struct Display { int refreshed=0; void UpdateStatusBar(bool) { ++refreshed; } };
struct Board { Display display;
    static Board& GetInstance() { static Board b; return b; }
    Display* GetDisplay() { return &display; }
};
struct Application { const char* mvp_teaching_mode_="unknown";
    bool mvp_auto_mode_=true, mvp_capture_started_=true;
    void UpdateMvpTeachingMode(const char*);
};
''' + actual + r'''int main() {
    Application app;
    app.UpdateMvpTeachingMode("chat"); assert(!strcmp(app.mvp_teaching_mode_,"chat"));
    const char* invalid_values[] = {nullptr, "CHAT", "", "chat plus text"};
    for (const char* invalid : invalid_values) {
        app.UpdateMvpTeachingMode(invalid);
        assert(!strcmp(app.mvp_teaching_mode_,"chat"));
    }
    assert(Board::GetInstance().display.refreshed==1);
    app.UpdateMvpTeachingMode("express"); assert(!strcmp(app.mvp_teaching_mode_,"express"));
    assert(app.mvp_auto_mode_ && app.mvp_capture_started_);
}
''')

    def compile_run(self, source, includes=()):
        compiler = shutil.which('c++')
        if not compiler:
            self.skipTest('C++ compiler unavailable')
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / 'case.cc'; path.write_text(source)
            command = [compiler, '-std=c++17', str(path), '-o', str(folder / 'case')]
            for include in includes:
                command.extend(['-I', str(include)])
            built = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(folder / 'case')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_status_reflects_real_capture_processing_playback_and_recovery(self):
        actual = method(ROOT / 'main/application.cc',
                        'const char* Application::GetMvpDisplayStatus() const')
        self.compile_run(r'''#include <cassert>
#include <cstring>
#include "cloud_turn_recovery.h"
#define CONFIG_ENGLISH_MVP_CLOUD 1
#define CONFIG_USE_AUDIO_PROCESSOR 1
int64_t esp_timer_get_time() { return 1000000; }
enum DeviceState { kDeviceStateIdle, kDeviceStateWifiConfiguring, kDeviceStateSpeaking };
struct Protocol { bool opened = true; bool IsAudioChannelOpened() { return opened; } };
struct Application {
    Protocol transport; Protocol* protocol_ = &transport;
    DeviceState state = kDeviceStateIdle;
    bool mvp_network_ready_ = false, mvp_connecting_ = false;
    bool mvp_receiving_audio_ = false, mvp_waiting_response_ = false;
    bool mvp_stopping_ = false, mvp_capture_started_ = false;
    bool mvp_auto_mode_ = false, mvp_vad_monitoring_ = false;
    CloudTurnRecovery mvp_cloud_recovery_;
    DeviceState GetDeviceState() const { return state; }
    const char* GetMvpDisplayStatus() const;
};
''' + actual + r'''int main() {
    Application a;
    auto status = [&](const char* expected) { assert(!strcmp(a.GetMvpDisplayStatus(), expected)); };
    status("Connecting Wi-Fi");
    a.state = kDeviceStateWifiConfiguring; status("Wi-Fi setup");
    a.state = kDeviceStateIdle; a.mvp_network_ready_ = true;
    a.transport.opened = false; status("Connecting cloud");
    a.transport.opened = true; status("Ready");
    a.mvp_auto_mode_ = true; a.mvp_vad_monitoring_ = true; status("Listening");
    a.mvp_capture_started_ = true; status("Recording");
    a.mvp_stopping_ = true; status("Uploading");
    a.mvp_waiting_response_ = true; status("Processing");
    a.mvp_receiving_audio_ = true; status("Playing");
    // Reading UI status cannot stop captures or change automatic mode.
    assert(a.mvp_auto_mode_ && a.mvp_capture_started_ && a.mvp_waiting_response_);
    a.mvp_receiving_audio_ = a.mvp_waiting_response_ = a.mvp_stopping_ = a.mvp_capture_started_ = false;
    a.mvp_cloud_recovery_.PauseAfterError(1000000, true, 3000);
    status("Paused"); assert(a.mvp_auto_mode_);
    a.mvp_auto_mode_ = false; status("Paused"); a.mvp_auto_mode_ = true;
    a.mvp_cloud_recovery_.Clear(); status("Listening");
    a.mvp_vad_monitoring_ = false; a.mvp_auto_mode_ = false; status("Ready");
}
''', (ROOT / 'main',))

    def test_real_subtitle_method_keeps_english_during_input_and_bounds_utf8(self):
        actual = method(BOARD / 'learning_display.cc', 'void LearningDisplay::SetChatMessage(')
        actual += method(BOARD / 'learning_display.cc', 'void LearningDisplay::ClearChatMessages()')
        self.compile_run(r'''#include <cassert>
#include <cstring>
#include <string>
#include "learning_display_text.h"
struct Font { const void* font() { return nullptr; } };
struct Theme { Font font; Font* GetTextFont() { return &font; } };
constexpr int LV_OBJ_FLAG_HIDDEN = 1;
struct Label { std::string text; };
void lv_obj_remove_flag(Label*, int) {}
void lv_obj_set_style_text_font(Label*, const void*, int) {}
void lv_label_set_text(Label* label, const char* text) { label->text = text; }
struct LearningDisplay { Label label, source; Label* chat_message_label_ = &label;
    Label* source_label_ = &source; Font chinese; Font* chinese_font_ = &chinese;
    Theme theme; Theme* current_theme_ = &theme;
    void SetChatMessage(const char* role, const char* content); void ClearChatMessages(); void StartSubtitleScroll() {} };
struct DisplayLockGuard { DisplayLockGuard(LearningDisplay*) {} operator bool() const { return true; } };
''' + actual + r'''int main() {
    LearningDisplay d;
    d.SetChatMessage("assistant", "Can I have some more juice?");
    d.SetChatMessage("user", "我想喝水");
    d.SetChatMessage("system", "");
    d.ClearChatMessages(); // The real application calls this on idle after playback.
    assert(d.label.text == "…");
    assert(d.source.text == "我想喝水");
    d.SetChatMessage("assistant", "Can I have some more juice?");
    assert(d.label.text == "Can I have some more juice?");
    d.ClearChatMessages(); assert(d.label.text == "Can I have some more juice?");
    d.SetChatMessage("assistant", "José’s bottle.");
    assert(d.label.text == "José’s bottle.");
    d.SetChatMessage("assistant", "就是再多一点。");
    assert(d.label.text == "就是再多一点。");
    d.SetChatMessage("assistant", nullptr);
    assert(d.label.text == "Speak, then listen.");
    std::string long_text(1596, 'x'); long_text += "éééééé";
    auto bounded = LearningDisplayText::Bounded(long_text.c_str());
    assert(bounded.size() <= 1600 && bounded.substr(bounded.size()-3) == "...");
    assert(bounded == std::string(1596, 'x') + "...");
    long_text = std::string(1595, 'x') + "éééééé";
    assert(LearningDisplayText::Bounded(long_text.c_str()) == std::string(1595, 'x') + "é...");
    assert(!LearningDisplayText::ContainsCjk("José’s bottle."));
    assert(LearningDisplayText::ContainsCjk("前面 English 后面"));
}
''', (BOARD,))

    def test_long_subtitle_scrolls_with_wrap_and_short_replacement_stops_animation(self):
        actual = method(BOARD / 'learning_display.cc', 'void LearningDisplay::ScrollSubtitle(')
        actual += method(BOARD / 'learning_display.cc', 'void LearningDisplay::StartSubtitleScroll()')
        self.compile_run(r'''#include <cassert>
#include <algorithm>
#include <cstdint>
struct lv_obj_t { int height = 0, content_height = 0, y = 0; };
constexpr int LV_ANIM_OFF = 0, LV_ANIM_REPEAT_INFINITE = -1;
struct lv_anim_t { void* var; void (*exec)(void*, int32_t); int start, end;
    uint32_t duration, reverse_duration; int repeat; };
lv_anim_t active{}; bool running = false;
void lv_anim_delete(lv_obj_t*, void (*)(void*, int32_t)) { running = false; }
void lv_obj_update_layout(lv_obj_t*) {}
void lv_obj_scroll_to_y(lv_obj_t* obj, int y, int) { obj->y = y; }
int lv_obj_get_height(lv_obj_t* obj) { return obj->height; }
int lv_obj_get_content_height(lv_obj_t* obj) { return obj->content_height; }
lv_obj_t* subtitle;
int source_height = 0;
int lv_obj_get_scroll_bottom(lv_obj_t* obj) { return std::max(0, subtitle->height + source_height - obj->content_height); }
void lv_anim_init(lv_anim_t* a) { *a = {}; }
void lv_anim_set_var(lv_anim_t* a, void* p) { a->var = p; }
void lv_anim_set_exec_cb(lv_anim_t* a, void (*fn)(void*, int32_t)) { a->exec = fn; }
void lv_anim_set_values(lv_anim_t* a, int start, int end) { a->start=start; a->end=end; }
void lv_anim_set_duration(lv_anim_t* a, uint32_t d) { a->duration=d; }
void lv_anim_set_reverse_duration(lv_anim_t* a, uint32_t d) { a->reverse_duration=d; }
void lv_anim_set_delay(lv_anim_t*, uint32_t) {}
void lv_anim_set_reverse_delay(lv_anim_t*, uint32_t) {}
void lv_anim_set_repeat_delay(lv_anim_t*, uint32_t) {}
void lv_anim_set_repeat_count(lv_anim_t* a, int repeat) { a->repeat=repeat; }
void lv_anim_start(lv_anim_t* a) { active=*a; running=true; }
struct LearningDisplay { lv_obj_t* content_; lv_obj_t* chat_message_label_;
    static void ScrollSubtitle(void*,int32_t); void StartSubtitleScroll(); };
''' + actual + r'''int main() {
    lv_obj_t card{0,128,0}, label{72,0,0};
    LearningDisplay display{&card,&label}; subtitle=&label;
    display.StartSubtitleScroll(); assert(!running && card.y==0);
    label.height=400; display.StartSubtitleScroll();
    assert(running && active.start==0 && active.end==272);
    assert(active.duration>=3000 && active.duration<=60000);
    assert(active.reverse_duration==active.duration);
    active.exec(active.var,active.end); assert(card.y==272);
    // A new short response starts at its first line with no stale animation.
    label.height=28; display.StartSubtitleScroll();
    assert(!running && card.y==0);
    source_height=100; label.height=72; display.StartSubtitleScroll();
    assert(running && active.end==44); // Both Chinese input and English output contribute.
    label.height=5000; display.StartSubtitleScroll();
    assert(running && active.duration<=60000);
}
''')

    def test_actual_pin_guards_reject_collisions_and_reserved_interfaces(self):
        compiler = shutil.which('c++')
        if not compiler:
            self.skipTest('C++ compiler unavailable')
        base = {'MIC_SCK':2, 'MIC_WS':1, 'MIC_DIN':42, 'SPK_BCLK':40,
                'SPK_WS':41, 'SPK_DOUT':39, 'BUTTON':0, 'BUTTON_ACTIVE_HIGH':0,
                'MIC_SLOT':1, 'SPK_SLOT':1, 'ST7789':1,
                'LCD_SCK':10, 'LCD_MOSI':11, 'LCD_RST':12, 'LCD_DC':13,
                'LCD_CS':14, 'LCD_BL':-1, 'LCD_WIDTH':240, 'LCD_HEIGHT':240,
                'LCD_OFFSET_X':0, 'LCD_OFFSET_Y':0}
        cases = [(None, None, True), ('LCD_BL',15,True), ('LCD_CS',11,False),
                 ('LCD_CS',39,False), ('LCD_CS',19,False), ('LCD_CS',43,False),
                 ('LCD_CS',35,False), ('LCD_CS',45,False), ('LCD_CS',-1,False),
                 ('LCD_OFFSET_Y',81,False)]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); (folder/'driver').mkdir()
            (folder/'driver/gpio.h').write_text('#define GPIO_IS_VALID_GPIO(p) ((p)>=0 && (p)<=48)\n'
                '#define GPIO_IS_VALID_OUTPUT_GPIO(p) ((p)>=0 && (p)<=48 && (p)!=46)\n')
            (folder/'case.cc').write_text('#include "config.h"\nint main() {}\n')
            for key, value, expected in cases:
                config = dict(base)
                if key: config[key] = value
                (folder/'sdkconfig.h').write_text(''.join(f'#define CONFIG_ENGLISH_EDGE_{k} {v}\n' for k,v in config.items()))
                result = subprocess.run([compiler,'-std=c++17','-fsyntax-only','-I',str(folder),
                    '-I',str(BOARD),str(folder/'case.cc')], capture_output=True,text=True)
                with self.subTest(key=key, value=value):
                    self.assertEqual(result.returncode == 0, expected, result.stderr)
                    if not expected: self.assertIn('static assertion', result.stderr)


if __name__ == '__main__':
    unittest.main()
