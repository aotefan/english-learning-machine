#include "application.h"
#include "assets/lang_config.h"
#include "button.h"
#include "codecs/no_audio_codec.h"
#include "config.h"
#include "settings.h"
#include "wifi_board.h"
#if CONFIG_ENGLISH_MVP_DUPLEX
#include "reference_audio_codec.h"
#endif

#include <esp_log.h>

#include <cmath>
#include <cstdint>
#include <vector>

#if CONFIG_ENGLISH_EDGE_ST7789
#include <driver/spi_common.h>
#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_panel_vendor.h>
#include "backlight.h"
#include "learning_display.h"
#if CONFIG_ENGLISH_EDGE_LCD_COLOR_TEST
#include "lcd_color_test.h"
#endif
#endif

#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
// The generic codec shifts 32-bit I2S words by 12 before clamping to 16 bits.
// That clips this microphone's idle recovery and makes WebRTC VAD unstable.
// Keep the proven button build unchanged while testing a lower conversion
// gain in the experimental cloud VAD variant.
class EnglishEdgeVadAudioCodec final : public NoAudioCodecSimplex {
public:
    using NoAudioCodecSimplex::NoAudioCodecSimplex;

protected:
    int64_t sample_sum_ = 0;
    uint64_t square_sum_ = 0;
    int sample_count_ = 0;
    int peak_ = 0;
    int32_t dc_estimate_q8_ = 0;

    int Read(int16_t* dest, int samples) override {
        std::vector<int32_t> words(samples);
        size_t bytes_read = 0;
        if (i2s_channel_read(rx_handle_, words.data(), words.size() * sizeof(int32_t), &bytes_read,
                             200) != ESP_OK) {
            return 0;
        }
        const int count = bytes_read / sizeof(int32_t);
        for (int i = 0; i < count; ++i) {
            const int32_t scaled = words[i] >> 14;
            const int32_t limited = scaled > INT16_MAX   ? INT16_MAX
                                    : scaled < INT16_MIN ? INT16_MIN
                                                         : scaled;
            // Track only very slow bias drift. The microphone shows
            // multi-second baseline swings after its startup transient.
            dc_estimate_q8_ += ((limited * 256) - dc_estimate_q8_) / 256;
            const int32_t highpassed = limited - (dc_estimate_q8_ >> 8);
            dest[i] = highpassed > INT16_MAX   ? INT16_MAX
                      : highpassed < INT16_MIN ? INT16_MIN
                                               : static_cast<int16_t>(highpassed);
            const int32_t value = dest[i];
            sample_sum_ += value;
            square_sum_ += static_cast<uint64_t>(value * value);
            const int magnitude = value < 0 ? -value : value;
            if (magnitude > peak_) {
                peak_ = magnitude;
            }
        }
        sample_count_ += count;
        if (sample_count_ >= input_sample_rate_) {
            const uint64_t dc_energy =
                static_cast<uint64_t>(sample_sum_ * sample_sum_ / sample_count_);
            const uint64_t ac_energy = square_sum_ > dc_energy ? square_sum_ - dc_energy : 0;
            ESP_LOGI("EnglishEdgeS3", "Mic 1 s: mean=%lld AC RMS=%d peak=%d",
                     static_cast<long long>(sample_sum_ / sample_count_),
                     static_cast<int>(std::sqrt(static_cast<double>(ac_energy) / sample_count_)),
                     peak_);
            sample_sum_ = 0;
            square_sum_ = 0;
            sample_count_ = 0;
            peak_ = 0;
        }
        return count;
    }
};
#endif

// Minimal glue around upstream Wi-Fi, button, codec and null display/LED helpers.
class EnglishEdgeS3Board : public WifiBoard {
private:
    Button button_;
#if CONFIG_ENGLISH_EDGE_ST7789
    LearningDisplay* display_ = nullptr;

    void InitializeDisplay() {
        spi_bus_config_t bus = {};
        bus.mosi_io_num = CONFIG_ENGLISH_EDGE_LCD_MOSI;
        bus.miso_io_num = GPIO_NUM_NC;
        bus.sclk_io_num = CONFIG_ENGLISH_EDGE_LCD_SCK;
        bus.quadwp_io_num = GPIO_NUM_NC;
        bus.quadhd_io_num = GPIO_NUM_NC;
        bus.max_transfer_sz = CONFIG_ENGLISH_EDGE_LCD_WIDTH * 20 * sizeof(uint16_t);
        ESP_ERROR_CHECK(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO));

        esp_lcd_panel_io_spi_config_t io_config = {};
        io_config.cs_gpio_num = static_cast<gpio_num_t>(CONFIG_ENGLISH_EDGE_LCD_CS);
        io_config.dc_gpio_num = static_cast<gpio_num_t>(CONFIG_ENGLISH_EDGE_LCD_DC);
        io_config.spi_mode = CONFIG_ENGLISH_EDGE_LCD_SPI_MODE;
#if CONFIG_ENGLISH_EDGE_LCD_SLOW_TEST
        io_config.pclk_hz = 1 * 1000 * 1000;
#else
        io_config.pclk_hz = 10 * 1000 * 1000;
#endif
        io_config.trans_queue_depth = 4;
        io_config.lcd_cmd_bits = 8;
        io_config.lcd_param_bits = 8;
        esp_lcd_panel_io_handle_t io = nullptr;
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(SPI2_HOST, &io_config, &io));

        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = static_cast<gpio_num_t>(CONFIG_ENGLISH_EDGE_LCD_RST);
        panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
        panel_config.bits_per_pixel = 16;
        esp_lcd_panel_handle_t panel = nullptr;
        ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(io, &panel_config, &panel));
        ESP_ERROR_CHECK(esp_lcd_panel_reset(panel));
        ESP_ERROR_CHECK(esp_lcd_panel_init(panel));
        ESP_ERROR_CHECK(esp_lcd_panel_invert_color(panel, EnglishEdgeConfig::kDisplayInvert));

#if CONFIG_ENGLISH_EDGE_LCD_COLOR_TEST
        ESP_LOGI("EnglishLcdTest", "SPI clock=%d Hz", io_config.pclk_hz);
        RunLcdColorTest(io, panel, CONFIG_ENGLISH_EDGE_LCD_WIDTH, CONFIG_ENGLISH_EDGE_LCD_HEIGHT);
#endif
        display_ = new LearningDisplay(
            io, panel, CONFIG_ENGLISH_EDGE_LCD_WIDTH, CONFIG_ENGLISH_EDGE_LCD_HEIGHT,
            CONFIG_ENGLISH_EDGE_LCD_OFFSET_X, CONFIG_ENGLISH_EDGE_LCD_OFFSET_Y,
            EnglishEdgeConfig::kDisplayMirrorX, EnglishEdgeConfig::kDisplayMirrorY,
            EnglishEdgeConfig::kDisplaySwapXY);
        if (auto* backlight = GetBacklight()) {
            backlight->RestoreBrightness();
        }
    }
#endif

public:
    EnglishEdgeS3Board()
        : button_(static_cast<gpio_num_t>(CONFIG_ENGLISH_EDGE_BUTTON),
                  CONFIG_ENGLISH_EDGE_BUTTON_ACTIVE_HIGH == 1, 2000) {
        // One-time migration of this prototype's original quiet default.
        // Preserve other volumes and any choice made after this migration.
        {
            Settings audio_settings("audio", true);
            if (audio_settings.GetInt("spk_check_v", 0) < 4) {
                const int previous = audio_settings.GetInt("output_volume", -1);
                if (previous == -1 || previous == 10 || previous == 40 || previous == 60 ||
                    previous == 90) {
                    audio_settings.SetInt("output_volume", 100);
                    ESP_LOGI("EnglishEdgeS3", "Speaker check volume migration: %d -> 100",
                             previous);
                }
                audio_settings.SetInt("spk_check_v", 4);
            }
        }
#if CONFIG_ENGLISH_EDGE_REFERENCE_BUILD
        ESP_LOGW("EnglishEdgeS3",
                 "REFERENCE ONLY: wiring and memory are not verified for the user's board");
#endif
#if CONFIG_ENGLISH_EDGE_ST7789
        InitializeDisplay();
#endif
        button_.OnClick([this]() {
            Application::GetInstance().Schedule([this]() {
                auto& app = Application::GetInstance();
                ESP_LOGI("EnglishEdgeS3", "Record button click: state=%d",
                         static_cast<int>(app.GetDeviceState()));
                if (app.GetDeviceState() == kDeviceStateStarting) {
#if CONFIG_ENGLISH_MVP_LOCAL
                    ESP_LOGI("EnglishEdgeS3", "Waiting for WiFi; recording is not ready");
#else
                    EnterWifiConfigMode();
#endif
                } else {
                    app.ToggleChatState();
                }
            });
        });
#if CONFIG_ENGLISH_MVP_LOCAL
        button_.OnDoubleClick([]() {
            Application::GetInstance().Schedule(
                []() { Application::GetInstance().ToggleMvpAutoMode(); });
        });
        // Explicit provisioning remains available without turning an ordinary
        // recording click during reconnection into a mode change.
        button_.OnMultipleClick(
            [this]() {
                Application::GetInstance().Schedule([this]() {
                    const auto state = Application::GetInstance().GetDeviceState();
                    if (state == kDeviceStateStarting || state == kDeviceStateIdle) {
                        EnterWifiConfigMode();
                    }
                });
            },
            3);
#endif
        // Reuse upstream playback to test the speaker without a server request.
        // Keep the current volume when testing; do not undo the calibrated level.
        button_.OnLongPress([this]() {
            Application::GetInstance().Schedule([this]() {
                auto& app = Application::GetInstance();
                const auto state = app.GetDeviceState();
                if (state != kDeviceStateIdle && state != kDeviceStateWifiConfiguring) {
                    ESP_LOGI("EnglishEdgeS3", "Speaker check skipped: device is busy");
                    return;
                }
                auto* codec = GetAudioCodec();
                ESP_LOGI("EnglishEdgeS3", "Local speaker check: volume=%d, output=48000 Hz",
                         codec->output_volume());
                app.PlaySound(Lang::Sounds::OGG_SUCCESS);
            });
        });
    }

    AudioCodec* GetAudioCodec() override {
        // MAX98357A Rev.16, p.19 excludes 24 kHz LRCLK. AudioService resamples
        // the unchanged 24 kHz downlink to 48 kHz; 48 kHz also supports Opus init.
#if CONFIG_ENGLISH_MVP_DUPLEX
        static EnglishEdgeReferenceCodec codec(
#elif CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
        static EnglishEdgeVadAudioCodec codec(
#else
        static NoAudioCodecSimplex codec(
#endif
            16000, 48000, static_cast<gpio_num_t>(CONFIG_ENGLISH_EDGE_SPK_BCLK),
            static_cast<gpio_num_t>(CONFIG_ENGLISH_EDGE_SPK_WS),
            static_cast<gpio_num_t>(CONFIG_ENGLISH_EDGE_SPK_DOUT),
            CONFIG_ENGLISH_EDGE_SPK_SLOT == 1 ? I2S_STD_SLOT_LEFT : I2S_STD_SLOT_RIGHT,
            static_cast<gpio_num_t>(CONFIG_ENGLISH_EDGE_MIC_SCK),
            static_cast<gpio_num_t>(CONFIG_ENGLISH_EDGE_MIC_WS),
            static_cast<gpio_num_t>(CONFIG_ENGLISH_EDGE_MIC_DIN),
            CONFIG_ENGLISH_EDGE_MIC_SLOT == 1 ? I2S_STD_SLOT_LEFT : I2S_STD_SLOT_RIGHT);
        return &codec;
    }

#if CONFIG_ENGLISH_EDGE_ST7789
    Display* GetDisplay() override { return display_; }

    Backlight* GetBacklight() override {
#if CONFIG_ENGLISH_EDGE_LCD_BL >= 0
        static PwmBacklight backlight(static_cast<gpio_num_t>(CONFIG_ENGLISH_EDGE_LCD_BL));
        return &backlight;
#else
        return nullptr;
#endif
    }
#endif
};

DECLARE_BOARD(EnglishEdgeS3Board);
