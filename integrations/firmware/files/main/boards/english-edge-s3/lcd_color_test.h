#pragma once

#include <esp_heap_caps.h>
#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <esp_log.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <algorithm>
#include <cstdint>

// Called once before LVGL, audio and networking own any tasks or panel callbacks.
inline void RunLcdColorTest(esp_lcd_panel_io_handle_t io, esp_lcd_panel_handle_t panel, int width,
                            int height) {
    constexpr int strip_height = 20;
    auto* buffer = static_cast<uint8_t*>(
        heap_caps_malloc(width * strip_height * 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA));
    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel, true));
    const uint16_t colors[] = {0xF800, 0x07E0, 0x001F};
    const char* names[] = {"red", "green", "blue"};
    for (int color = 0; color < 3; ++color) {
        // The ST7789 accepts RGB565 in big-endian byte order.
        for (int pixel = 0; pixel < width * strip_height; ++pixel) {
            buffer[pixel * 2] = colors[color] >> 8;
            buffer[pixel * 2 + 1] = colors[color] & 0xFF;
        }
        for (int y = 0; y < height; y += strip_height) {
            ESP_ERROR_CHECK(esp_lcd_panel_draw_bitmap(panel, 0, y, width,
                                                      std::min(y + strip_height, height), buffer));
            // tx_param(-1) drains pending DMA without sending a command. Do not
            // reuse or free the strip until its queued color transfer completes.
            ESP_ERROR_CHECK(esp_lcd_panel_io_tx_param(io, -1, nullptr, 0));
        }
        ESP_LOGI("EnglishLcdTest", "Pattern %s sent; visible output requires confirmation",
                 names[color]);
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
    heap_caps_free(buffer);
    ESP_LOGI("EnglishLcdTest", "Color diagnostic finished; starting normal display");
}
