"""Compile actual remote-mode code with stand-ins; no hardware acceptance."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class RemoteControlsTest(unittest.TestCase):
    def test_cloud_registers_tools_and_returns_mcp_replies_but_local_mac_stays_disabled(self):
        compiler = shutil.which('c++')
        if not compiler:self.skipTest('C++ compiler unavailable')
        text=(ROOT/'main/application.cc').read_text()
        start=text.index('void Application::SendMcpMessage(')
        method=text[start:text.index('\n}\n',start)+3]
        start=text.index('    // Add MCP common tools')
        end=text.index('\n',text.index('\n#endif',start)+len('\n#endif'))
        # There are two nested guards now; retain the outer closing guard too.
        end=text.index('\n#endif',end)+len('\n#endif')
        registration=text[start:end]
        harness=r'''
#include <cassert>
#include <functional>
#include <string>
struct Transport { int sent=0; void SendMcpMessage(const std::string&){++sent;} };
struct McpServer { int common=0,user=0;static McpServer& GetInstance(){static McpServer server;return server;}void AddCommonTools(){++common;}void AddUserOnlyTools(){++user;} };
class Application {public:Transport transport;Transport* protocol_=&transport;std::function<void(const std::string&)> mcp_broadcast_callback_;void Schedule(std::function<void()> callback){callback();}void SendMcpMessage(const std::string&);};
'''
        main=r'''
int main(){Application app;app.SendMcpMessage("fixture");Register();auto& server=McpServer::GetInstance();
#if CONFIG_ENGLISH_MVP_LOCAL && !CONFIG_ENGLISH_MVP_CLOUD
assert(app.transport.sent==0&&server.common==0&&server.user==0);
#else
assert(app.transport.sent==1&&server.common==1);
#if CONFIG_ENGLISH_MVP_LOCAL
assert(server.user==0);
#else
assert(server.user==1);
#endif
#endif
}
'''
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);cpp=path/'mcp.cc';cpp.write_text(harness+method+'\nvoid Register(){\n'+registration+'\n}\n'+main)
            for local,cloud in ((1,1),(1,0),(0,0)):
                with self.subTest(local=local,cloud=cloud):
                    output=path/f'mcp-{local}-{cloud}'
                    subprocess.run([compiler,'-std=c++17',f'-DCONFIG_ENGLISH_MVP_LOCAL={local}',f'-DCONFIG_ENGLISH_MVP_CLOUD={cloud}',str(cpp),'-o',str(output)],check=True,capture_output=True)
                    subprocess.run([str(output)],check=True,capture_output=True)

    def test_mode_changes_preserve_connection_and_refuse_active_input(self):
        compiler = shutil.which('c++')
        if not compiler:self.skipTest('C++ compiler unavailable')
        source = (ROOT / 'main/application.cc').read_text()
        start = source.index('bool Application::SetRemoteMvpAutoMode(')
        method = source[start:source.index('\n}\n', start)+3]
        harness = r'''
#include <cassert>
#include <cstdint>
#define CONFIG_ENGLISH_MVP_CLOUD 1
enum State { kDeviceStateIdle, kDeviceStateSpeaking };
struct Audio { bool idle=true; int finishes=0; bool IsPlaybackIdle(){return idle;} void FinishRawCapture(){++finishes;} };
struct Recovery { void Clear(){} };
struct Display { void UpdateStatusBar(bool){} };
struct Board { static Board& GetInstance(){static Board board;return board;} Display* GetDisplay(){static Display display;return &display;} };
class Application {
public:
 State state=kDeviceStateIdle; Audio audio_service_; Recovery mvp_cloud_recovery_;
 bool mvp_capture_started_=false,mvp_waiting_response_=false,mvp_stopping_=false,mvp_auto_mode_=false;
 bool connection_open=true; int arms=0,resets=0; int64_t mvp_auto_resume_at_us_=99;
 State GetDeviceState(){return state;} void ArmDeviceVad(){++arms;} void ResetDeviceVad(){++resets;}
 bool SetRemoteMvpAutoMode(bool);
};
'''
        main = r'''
int main(){
 Application app;
#if CONFIG_USE_AUDIO_PROCESSOR
 assert(app.SetRemoteMvpAutoMode(true));assert(app.mvp_auto_mode_&&app.arms==1&&app.connection_open);
 app.mvp_capture_started_=true;assert(!app.SetRemoteMvpAutoMode(false));assert(app.mvp_auto_mode_&&app.audio_service_.finishes==0);
 app.mvp_capture_started_=false;app.state=kDeviceStateSpeaking;assert(!app.SetRemoteMvpAutoMode(false));assert(app.mvp_auto_mode_);
 app.state=kDeviceStateIdle;assert(app.SetRemoteMvpAutoMode(false));assert(!app.mvp_auto_mode_&&app.resets==1&&app.connection_open);
#else
 assert(!app.SetRemoteMvpAutoMode(true));assert(!app.mvp_auto_mode_);
 assert(app.SetRemoteMvpAutoMode(false));assert(app.connection_open);
#endif
}
'''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder);cpp=path/'controls.cc';cpp.write_text(harness+method+main)
            for enabled in (0,1):
                with self.subTest(audio_processor=enabled):
                    output=path/f'controls-{enabled}'
                    subprocess.run([compiler,'-std=c++17',f'-DCONFIG_USE_AUDIO_PROCESSOR={enabled}',str(cpp),'-o',str(output)],check=True,capture_output=True)
                    subprocess.run([str(output)],check=True,capture_output=True)


if __name__ == '__main__':unittest.main()
