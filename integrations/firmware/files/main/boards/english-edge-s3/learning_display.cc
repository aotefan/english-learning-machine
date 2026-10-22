#include <sdkconfig.h>

#if CONFIG_ENGLISH_EDGE_ST7789
#include "application.h"
#include "learning_display.h"
#include "learning_display_labels.h"
#include "learning_display_text.h"
#include "lvgl_font.h"
#include "lvgl_theme.h"

#include <algorithm>
#include <cstring>

extern const uint8_t chinese_font_data[] asm("_binary_font_noto_sans_common_16_4_bin_start");

LearningDisplay::~LearningDisplay() {
    // LvglDisplay's timer and header labels must be disposed before the LCD
    // base deletes the LVGL display that owns their screen.
    if (notification_timer_) {
        esp_timer_stop(notification_timer_);
        esp_timer_delete(notification_timer_);
        notification_timer_ = nullptr;
    }
    DisplayLockGuard lock(this);
    for (auto** label : {&status_label_, &notification_label_, &footer_label_, &mode_label_}) {
        if (*label) {
            lv_obj_delete(*label);
            *label = nullptr;
        }
    }
    if (content_) {
        lv_obj_delete(content_);
        content_ = chat_message_label_ = source_label_ = nullptr;
    }
}

void LearningDisplay::SetupUI() {
    if (setup_ui_called_ || display_ == nullptr) {
        return;
    }
    DisplayLockGuard lock(this);
    if (!lock) {
        return;
    }
    Display::SetupUI();
    auto* screen = lv_display_get_screen_active(display_);
    auto* font = current_theme_->GetTextFont()->font();
    chinese_font_ = std::make_unique<LvglCBinFont>(const_cast<uint8_t*>(chinese_font_data));
    if (!chinese_font_->font()) {
        ESP_LOGE("LearningDisplay", "Chinese font failed to load");
        return;
    }
    chinese_font_->SetFallback(font);
    lv_obj_set_style_bg_color(screen, lv_color_hex(0xF5F7FA), 0);
    lv_obj_set_style_text_color(screen, lv_color_hex(0x263238), 0);
    lv_obj_set_style_text_font(screen, font, 0);
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);

    status_label_ = lv_label_create(screen);
    lv_obj_set_pos(status_label_, 12, 6);
    lv_obj_set_width(status_label_, width_ - 24);
    lv_label_set_long_mode(status_label_, LV_LABEL_LONG_DOT);
    lv_obj_set_style_text_font(status_label_, chinese_font_->font(), 0);
    lv_label_set_text(status_label_, "正在启动");
    lv_obj_set_style_text_color(status_label_, lv_color_hex(0x3988AC), 0);

    notification_label_ = lv_label_create(screen);
    lv_obj_set_pos(notification_label_, 12, height_ - 30);
    lv_obj_set_width(notification_label_, width_ - 24);
    lv_label_set_long_mode(notification_label_, LV_LABEL_LONG_DOT);
    lv_obj_add_flag(notification_label_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_style_text_font(notification_label_, chinese_font_->font(), 0);
    lv_obj_set_style_bg_color(notification_label_, lv_color_hex(0xF5F7FA), 0);
    lv_obj_set_style_bg_opa(notification_label_, LV_OPA_COVER, 0);

    mode_label_ = lv_label_create(screen);
    lv_obj_set_pos(mode_label_, 12, 32);
    lv_obj_set_width(mode_label_, width_ - 24);
    lv_obj_set_style_text_font(mode_label_, chinese_font_->font(), 0);
    lv_label_set_text(mode_label_, "模式同步中");

    content_ = lv_obj_create(screen);
    lv_obj_set_pos(content_, 8, 62);
    lv_obj_set_size(content_, width_ - 16, height_ - 100);
    lv_obj_set_style_bg_color(content_, lv_color_white(), 0);
    lv_obj_set_style_border_color(content_, lv_color_hex(0xDFE7EC), 0);
    lv_obj_set_style_border_width(content_, 1, 0);
    lv_obj_set_style_radius(content_, 12, 0);
    lv_obj_set_style_pad_all(content_, 12, 0);
    lv_obj_set_scroll_dir(content_, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(content_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_flex_flow(content_, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(content_, 8, 0);

    source_label_ = lv_label_create(content_);
    lv_obj_set_width(source_label_, width_ - 42);
    lv_label_set_long_mode(source_label_, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_font(source_label_, chinese_font_->font(), 0);
    lv_obj_set_style_text_color(source_label_, lv_color_hex(0x75838D), 0);
    lv_label_set_text(source_label_, "");
    lv_obj_add_flag(source_label_, LV_OBJ_FLAG_HIDDEN);

    chat_message_label_ = lv_label_create(content_);
    lv_obj_set_size(chat_message_label_, width_ - 42, LV_SIZE_CONTENT);
    lv_label_set_long_mode(chat_message_label_, LV_LABEL_LONG_WRAP);
    lv_label_set_text(chat_message_label_, "Speak, then listen.");

    footer_label_ = lv_label_create(screen);
    lv_obj_set_pos(footer_label_, 12, height_ - 30);
    lv_obj_set_width(footer_label_, width_ - 24);
    lv_label_set_long_mode(footer_label_, LV_LABEL_LONG_DOT);
    lv_obj_set_style_text_font(footer_label_, chinese_font_->font(), 0);
    lv_label_set_text(footer_label_, "双击开启监听");
    lv_obj_set_style_text_color(footer_label_, lv_color_hex(0x75838D), 0);
}

void LearningDisplay::SetChatMessage(const char* role, const char* content) {
    // Keep both sides of the latest exchange across idle; ignore startup diagnostics.
    if (!role || (std::strcmp(role, "assistant") != 0 && std::strcmp(role, "user") != 0)) {
        return;
    }
    auto text = LearningDisplayText::Bounded(content);
    const bool source = std::strcmp(role, "user") == 0;
    if (text.empty()) {
        text = "Speak, then listen.";
    }
    DisplayLockGuard lock(this);
    auto* label = source ? source_label_ : chat_message_label_;
    if (lock && label) {
        lv_obj_remove_flag(label, LV_OBJ_FLAG_HIDDEN);
        if (source && chat_message_label_) {
            // Do not pair a new Chinese input with the previous turn's English.
            lv_label_set_text(chat_message_label_, "…");
        }
        lv_obj_set_style_text_font(label,
                                   source || LearningDisplayText::ContainsCjk(text)
                                       ? chinese_font_->font()
                                       : current_theme_->GetTextFont()->font(),
                                   0);
        lv_label_set_text(label, text.c_str());
        StartSubtitleScroll();
    }
}

void LearningDisplay::ScrollSubtitle(void* object, int32_t y) {
    lv_obj_scroll_to_y(static_cast<lv_obj_t*>(object), y, LV_ANIM_OFF);
}

void LearningDisplay::StartSubtitleScroll() {
    if (!content_ || !chat_message_label_) {
        return;
    }
    lv_anim_delete(content_, ScrollSubtitle);
    lv_obj_update_layout(content_);
    lv_obj_scroll_to_y(content_, 0, LV_ANIM_OFF);
    const int overflow = lv_obj_get_scroll_bottom(content_);
    if (overflow <= 0) {
        return;
    }
    // Label SCROLL mode expands text horizontally. Wrap first, then animate
    // the vertical viewport so a whole sentence stays readable on this screen.
    lv_anim_t animation;
    lv_anim_init(&animation);
    lv_anim_set_var(&animation, content_);
    lv_anim_set_exec_cb(&animation, ScrollSubtitle);
    lv_anim_set_values(&animation, 0, overflow);
    const uint32_t duration = std::clamp(overflow * 40, 3000, 60000);
    lv_anim_set_duration(&animation, duration);
    lv_anim_set_reverse_duration(&animation, duration);
    lv_anim_set_delay(&animation, 2000);
    lv_anim_set_reverse_delay(&animation, 2000);
    lv_anim_set_repeat_delay(&animation, 2000);
    lv_anim_set_repeat_count(&animation, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&animation);
}

void LearningDisplay::ClearChatMessages() {
    // The generic idle handler clears chat history after playback. Keep this
    // screen's one English demonstration; an explicit empty assistant message
    // can still clear it when required.
}

void LearningDisplay::SetHideSubtitle(bool hide) {
    DisplayLockGuard lock(this);
    if (lock && chat_message_label_) {
        if (hide) {
            lv_obj_add_flag(chat_message_label_, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_remove_flag(chat_message_label_, LV_OBJ_FLAG_HIDDEN);
        }
    }
}

void LearningDisplay::SetStatus(const char* status) {
    auto text = LearningDisplayText::Bounded(status);
    if (text.empty() || LearningDisplayText::ContainsCjk(text)) {
        return;
    }
    DisplayLockGuard lock(this);
    if (lock && status_label_) {
        lv_label_set_text(status_label_, text.c_str());
    }
}

void LearningDisplay::ShowNotification(const char* notification, int duration_ms) {
    auto text = LearningDisplayText::Bounded(notification);
    if (!text.empty()) {
        LvglDisplay::ShowNotification(text.c_str(), duration_ms);
        DisplayLockGuard lock(this);
        if (lock && status_label_) {
            // Temporary connection notices live in the footer, never hide listening status.
            lv_obj_remove_flag(status_label_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_move_foreground(notification_label_);
        }
    }
}

void LearningDisplay::ShowNotification(const std::string& notification, int duration_ms) {
    ShowNotification(notification.c_str(), duration_ms);
}

void LearningDisplay::UpdateStatusBar(bool update_all) {
    auto& app = Application::GetInstance();
    const char* status = app.GetMvpDisplayStatus();
    const bool automatic = app.IsMvpAutoMode();
    const char* mode = app.GetMvpTeachingMode();
    DisplayLockGuard lock(this);
    if (!lock || !status_label_ || !footer_label_) {
        return;
    }
    if (update_all || displayed_status_ != status) {
        displayed_status_ = status;
        lv_label_set_text(status_label_, LearningDisplayLabels::Status(status));
    }
    if (mode_label_ && (update_all || displayed_mode_ != mode)) {
        displayed_mode_ = mode;
        lv_label_set_text(mode_label_, LearningDisplayLabels::Mode(mode));
    }
    if (update_all || displayed_auto_mode_ != automatic) {
        displayed_auto_mode_ = automatic;
        lv_label_set_text(footer_label_, automatic ? "单击退出监听" : "双击开启监听");
    }
}

void LearningDisplay::SetTheme(Theme* theme) {
    if (!theme) {
        return;
    }
    DisplayLockGuard lock(this);
    if (!lock) {
        return;
    }
    current_theme_ = theme;
    if (display_ && setup_ui_called_) {
        auto* screen = lv_display_get_screen_active(display_);
        auto font = theme->GetTextFont();
        if (font && font->font()) {
            lv_obj_set_style_text_font(screen, font->font(), 0);
            if (chinese_font_) {
                chinese_font_->SetFallback(font->font());
            }
        }
    }
}
#endif
