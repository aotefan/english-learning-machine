#pragma once

// Optional candidate: capture the real post-volume I2S output through IDF's
// internal DIN=DOUT loopback. It is not a pre-playback software FIFO. Both RX
// clocks run continuously; acoustic alignment still requires physical tests.
#include <esp_ae_rate_cvt.h>
#include <esp_log.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <vector>
#include "codecs/no_audio_codec.h"

class EnglishEdgeReferenceCodec final : public NoAudioCodec {
public:
    EnglishEdgeReferenceCodec(int input_rate, int output_rate, gpio_num_t spk_bclk,
                              gpio_num_t spk_ws, gpio_num_t spk_dout, i2s_std_slot_mask_t spk_slot,
                              gpio_num_t mic_sck, gpio_num_t mic_ws, gpio_num_t mic_din,
                              i2s_std_slot_mask_t mic_slot) {
        input_sample_rate_ = input_rate;
        output_sample_rate_ = output_rate;
        input_channels_ = 2;
        input_reference_ = true;
        // Reference RX is the slave of the speaker TX clock on I2S0. Keep
        // that shared clock alive during capture, including silent listening.
        duplex_ = true;
        i2s_chan_config_t channel =
            I2S_CHANNEL_DEFAULT_CONFIG(XIAOZHI_I2S_PORT(0), I2S_ROLE_MASTER);
        channel.dma_desc_num = 12;  // 60 ms; must exceed the AFE feed block.
        channel.dma_frame_num = 240;
        channel.auto_clear_after_cb = true;
        ESP_ERROR_CHECK(i2s_new_channel(&channel, &tx_handle_, &reference_rx_));
        i2s_std_config_t speaker = {};
        speaker.clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(static_cast<uint32_t>(output_rate));
        speaker.slot_cfg =
            I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO);
        speaker.slot_cfg.slot_mask = spk_slot;
        speaker.gpio_cfg.mclk = I2S_GPIO_UNUSED;
        speaker.gpio_cfg.bclk = spk_bclk;
        speaker.gpio_cfg.ws = spk_ws;
        speaker.gpio_cfg.dout = spk_dout;
        speaker.gpio_cfg.din = spk_dout;  // Official GPIO-matrix digital loopback.
        ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx_handle_, &speaker));
        ESP_ERROR_CHECK(i2s_channel_init_std_mode(reference_rx_, &speaker));
        i2s_event_callbacks_t callbacks = {};
        callbacks.on_recv_q_ovf = ReferenceOverflow;
        ESP_ERROR_CHECK(i2s_channel_register_event_callback(reference_rx_, &callbacks, this));

        channel.id = XIAOZHI_I2S_PORT(1);
        channel.dma_desc_num = AUDIO_CODEC_DMA_DESC_NUM;
        ESP_ERROR_CHECK(i2s_new_channel(&channel, nullptr, &rx_handle_));
        i2s_std_config_t microphone = speaker;
        microphone.clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(static_cast<uint32_t>(input_rate));
        microphone.slot_cfg.slot_mask = mic_slot;
        microphone.gpio_cfg.bclk = mic_sck;
        microphone.gpio_cfg.ws = mic_ws;
        microphone.gpio_cfg.dout = I2S_GPIO_UNUSED;
        microphone.gpio_cfg.din = mic_din;
        ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx_handle_, &microphone));
        esp_ae_rate_cvt_cfg_t converter = {};
        converter.src_rate = output_rate;
        converter.dest_rate = input_rate;
        converter.channel = 1;
        converter.bits_per_sample = 16;
        converter.complexity = 2;
        converter.perf_type = ESP_AE_RATE_CVT_PERF_TYPE_SPEED;
        ESP_ERROR_CHECK(esp_ae_rate_cvt_open(&converter, &resampler_));
        ESP_LOGI("EnglishEdgeAEC",
                 "Candidate I2S loopback reference: 48k -> 16k MR; acoustic verification required");
    }

    ~EnglishEdgeReferenceCodec() override {
        if (reference_enabled_)
            i2s_channel_disable(reference_rx_);
        if (reference_rx_)
            i2s_del_channel(reference_rx_);
        if (resampler_)
            esp_ae_rate_cvt_close(resampler_);
    }

    bool InputReferenceHealthy() const override { return !reference_fault_.load(); }

    void EnableInput(bool enable) override {
        if (enable == input_enabled_)
            return;
        // Start the two capture streams together; preserve sample order.
        if (enable) {
            // IDF makes the later-initialized reference RX a slave. Starting
            // RX alone gives no BCLK/WS while the speaker is powered down.
            // TX DMA auto-clear sends silence when no playback is queued.
            NoAudioCodec::EnableOutput(true);
            observed_overruns_ = overruns_.load();
            ESP_ERROR_CHECK(i2s_channel_enable(reference_rx_));
            reference_enabled_ = true;
            esp_ae_rate_cvt_reset(resampler_);
        }
        NoAudioCodec::EnableInput(enable);
        if (!enable && reference_enabled_) {
            ESP_ERROR_CHECK(i2s_channel_disable(reference_rx_));
            reference_enabled_ = false;
        }
    }

protected:
    i2s_chan_handle_t reference_rx_ = nullptr;
    esp_ae_rate_cvt_handle_t resampler_ = nullptr;
    bool reference_enabled_ = false;
    int32_t dc_q8_ = 0;
    std::atomic<uint32_t> overruns_{0};
    uint32_t observed_overruns_ = 0;
    std::atomic<bool> reference_fault_{false};
    std::vector<int32_t> microphone_, reference_;
    std::vector<int16_t> signal_, aligned_;

    static bool IRAM_ATTR ReferenceOverflow(i2s_chan_handle_t, i2s_event_data_t*, void* context) {
        static_cast<EnglishEdgeReferenceCodec*>(context)->overruns_.fetch_add(1);
        return false;
    }

    int Read(int16_t* destination, int samples) override {
        if (reference_fault_) {
            vTaskDelay(pdMS_TO_TICKS(20));
            return 0;
        }
        const int count = samples / 2;
        // Retain capacity across reads; no repeated heap allocation per block.
        microphone_.resize(count);
        reference_.resize(count * 3);
        size_t mic_bytes = 0, ref_bytes = 0;
        if (i2s_channel_read(rx_handle_, microphone_.data(), count * sizeof(int32_t), &mic_bytes,
                             200) != ESP_OK ||
            mic_bytes != count * sizeof(int32_t)) {
            reference_fault_ = true;
            ESP_LOGE("EnglishEdgeAEC", "Microphone read failed; reference alignment unknown");
            return 0;
        }
        const int mic_count = mic_bytes / sizeof(int32_t);
        const auto status = i2s_channel_read(reference_rx_, reference_.data(),
                                             mic_count * 3 * sizeof(int32_t), &ref_bytes, 200);
        const uint32_t overrun = overruns_.load();
        if (status != ESP_OK || ref_bytes != mic_count * 3 * sizeof(int32_t) ||
            overrun != observed_overruns_) {
            reference_fault_ = true;
            observed_overruns_ = overrun;
            ESP_LOGE("EnglishEdgeAEC",
                     "Reference alignment lost: status=%d mic=%u ref=%u overruns=%lu tx=%d; "
                     "duplex test FAILED",
                     static_cast<int>(status), static_cast<unsigned>(mic_bytes),
                     static_cast<unsigned>(ref_bytes), static_cast<unsigned long>(overrun),
                     static_cast<int>(output_enabled_));
            return 0;  // Explicit fault, never manufacture a valid reference.
        }
        signal_.resize(mic_count * 3);
        for (int i = 0; i < mic_count * 3; ++i)
            signal_[i] = static_cast<int16_t>(reference_[i] >> 16);
        uint32_t capacity = 0;
        esp_ae_rate_cvt_get_max_out_sample_num(resampler_, signal_.size(), &capacity);
        aligned_.resize(capacity);
        uint32_t produced = capacity;
        const auto converted = esp_ae_rate_cvt_process(resampler_, signal_.data(), signal_.size(),
                                                       aligned_.data(), &produced);
        if (converted != ESP_OK || produced != static_cast<uint32_t>(mic_count)) {
            reference_fault_ = true;
            ESP_LOGE("EnglishEdgeAEC", "Reference resampling count mismatch; duplex test FAILED");
            return 0;
        }
        for (int i = 0; i < mic_count; ++i) {
            const int32_t value =
                std::max<int32_t>(INT16_MIN, std::min<int32_t>(INT16_MAX, microphone_[i] >> 14));
            dc_q8_ += ((value * 256) - dc_q8_) / 256;
            destination[i * 2] = static_cast<int16_t>(
                std::max<int32_t>(INT16_MIN, std::min<int32_t>(INT16_MAX, value - (dc_q8_ >> 8))));
            destination[i * 2 + 1] = aligned_[i];
        }
        return mic_count * 2;
    }
};
