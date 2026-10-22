#pragma once

#include "lcd_display.h"
#include "lvgl_font.h"

class LearningDisplay final : public SpiLcdDisplay {
public:
    using SpiLcdDisplay::SpiLcdDisplay;
    ~LearningDisplay() override;
    void SetupUI() override;
    void SetChatMessage(const char* role, const char* content) override;
    void ClearChatMessages() override;
    void SetEmotion(const char* emotion) override {}
    void SetStatus(const char* status) override;
    void ShowNotification(const char* notification, int duration_ms = 3000) override;
    void ShowNotification(const std::string& notification, int duration_ms = 3000) override;
    void UpdateStatusBar(bool update_all = false) override;
    void SetTheme(Theme* theme) override;
    void SetHideSubtitle(bool hide) override;

private:
    static void ScrollSubtitle(void* object, int32_t y);
    void StartSubtitleScroll();
    lv_obj_t* footer_label_ = nullptr;
    lv_obj_t* mode_label_ = nullptr;
    lv_obj_t* source_label_ = nullptr;
    std::unique_ptr<LvglFont> chinese_font_;
    std::string displayed_status_;
    std::string displayed_mode_;
    bool displayed_auto_mode_ = false;
};
