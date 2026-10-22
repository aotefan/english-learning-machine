"""Check that the real boot diagnostic cannot reuse/free an active DMA buffer."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class LcdColorDiagnosticTests(unittest.TestCase):
    def test_dma_lifetime_color_bytes_and_partial_final_strip(self):
        compiler = shutil.which('c++')
        if not compiler:
            self.skipTest('C++ compiler unavailable')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            for name in ('esp_heap_caps.h', 'esp_lcd_panel_io.h', 'esp_lcd_panel_ops.h',
                         'esp_log.h', 'freertos/FreeRTOS.h', 'freertos/task.h'):
                stub = path / name
                stub.parent.mkdir(parents=True, exist_ok=True)
                stub.touch()
            source = path / 'case.cc'
            source.write_text(r'''
#include <cassert>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <vector>
using esp_lcd_panel_io_handle_t = void*;
using esp_lcd_panel_handle_t = void*;
constexpr int ESP_OK=0, ESP_ERR_NO_MEM=1, MALLOC_CAP_INTERNAL=1, MALLOC_CAP_DMA=2;
#define ESP_ERROR_CHECK(result) assert((result)==ESP_OK)
#define ESP_LOGI(...) ((void)0)
#define pdMS_TO_TICKS(ms) (ms)
uint8_t* allocated=nullptr;
const uint8_t* pending=nullptr;
bool on=false;
int expected_width=0, expected_height=0, color=0, next_y=0, draws=0, delays=0;
const uint16_t colors[]={0xF800,0x07E0,0x001F};
std::vector<uint8_t> snapshot;
void* heap_caps_malloc(size_t size,int flags) {
    assert(flags==(MALLOC_CAP_INTERNAL|MALLOC_CAP_DMA));
    assert(size==static_cast<size_t>(expected_width*20*2));
    allocated=static_cast<uint8_t*>(std::malloc(size));return allocated;
}
void heap_caps_free(void* buffer) {
    assert(!pending && buffer==allocated);std::free(buffer);allocated=nullptr;
}
int esp_lcd_panel_disp_on_off(void*,bool value) { on=value;return ESP_OK; }
int esp_lcd_panel_draw_bitmap(void*,int x,int y,int x_end,int y_end,const void* buffer) {
    assert(on && !pending && x==0 && x_end==expected_width && y==next_y);
    assert(y_end<=expected_height && y_end-y<=20 && y_end>y);
    assert(buffer==allocated);
    pending=static_cast<const uint8_t*>(buffer);
    for(int pixel=0;pixel<expected_width*(y_end-y);++pixel) {
        assert(pending[2*pixel]==(colors[color]>>8));
        assert(pending[2*pixel+1]==(colors[color]&0xff));
    }
    snapshot.assign(pending,pending+expected_width*(y_end-y)*2);
    next_y=y_end;++draws;return ESP_OK;
}
int esp_lcd_panel_io_tx_param(void*,int command,const void* parameters,size_t size) {
    assert(command==-1 && !parameters && size==0 && pending);
    assert(!std::memcmp(pending,snapshot.data(),snapshot.size()));
    pending=nullptr;return ESP_OK;
}
void vTaskDelay(int ticks) {
    assert(ticks==2000 && !pending && next_y==expected_height);
    ++color;next_y=0;++delays;
}
#include "lcd_color_test.h"
int main() {
    for(auto height : {23,240}) {
        expected_width=height==23?4:240;expected_height=height;
        color=next_y=draws=delays=0;on=false;
        RunLcdColorTest(nullptr,nullptr,expected_width,expected_height);
        assert(!allocated && !pending && color==3 && delays==3);
        assert(draws==3*((height+19)/20));
    }
}
''')
            command = [compiler, '-std=c++17', '-I', str(path), '-I',
                       str(ROOT / 'main/boards/english-edge-s3'), str(source),
                       '-o', str(path / 'case')]
            built = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(path / 'case')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
