#pragma once

#include <driver/gpio.h>
#include <sdkconfig.h>

// No hardware pin defaults: select each value in the board build configuration.
namespace EnglishEdgeConfig {
constexpr int kPins[] = {CONFIG_ENGLISH_EDGE_MIC_SCK, CONFIG_ENGLISH_EDGE_MIC_WS,
                         CONFIG_ENGLISH_EDGE_MIC_DIN, CONFIG_ENGLISH_EDGE_SPK_BCLK,
                         CONFIG_ENGLISH_EDGE_SPK_WS,  CONFIG_ENGLISH_EDGE_SPK_DOUT,
                         CONFIG_ENGLISH_EDGE_BUTTON};

constexpr bool ValidPins() {
    for (unsigned i = 0; i < sizeof(kPins) / sizeof(kPins[0]); ++i) {
        if (kPins[i] < 0 || !GPIO_IS_VALID_GPIO(kPins[i])) {
            return false;
        }
        // Only mic data and button are inputs. Separate I2S clocks are required.
        if (i != 2 && i != 6 && !GPIO_IS_VALID_OUTPUT_GPIO(kPins[i])) {
            return false;
        }
        for (unsigned j = 0; j < i; ++j) {
            if (kPins[i] == kPins[j]) {
                return false;
            }
        }
    }
    return true;
}
static_assert(
    ValidPins(),
    "Set seven distinct valid English Edge GPIOs; review board memory/USB routing separately");
static_assert(CONFIG_ENGLISH_EDGE_BUTTON_ACTIVE_HIGH == 0 ||
                  CONFIG_ENGLISH_EDGE_BUTTON_ACTIVE_HIGH == 1,
              "Set the verified button active level");
static_assert(CONFIG_ENGLISH_EDGE_MIC_SLOT == 1 || CONFIG_ENGLISH_EDGE_MIC_SLOT == 2,
              "Select microphone left/right slot to match L/R wiring");
static_assert(CONFIG_ENGLISH_EDGE_SPK_SLOT == 1 || CONFIG_ENGLISH_EDGE_SPK_SLOT == 2,
              "Select amplifier left/right slot to match module configuration");

#if CONFIG_ENGLISH_EDGE_ST7789
#if CONFIG_ENGLISH_EDGE_LCD_INVERT
constexpr bool kDisplayInvert = true;
#else
constexpr bool kDisplayInvert = false;
#endif
#if CONFIG_ENGLISH_EDGE_LCD_MIRROR_X
constexpr bool kDisplayMirrorX = true;
#else
constexpr bool kDisplayMirrorX = false;
#endif
#if CONFIG_ENGLISH_EDGE_LCD_MIRROR_Y
constexpr bool kDisplayMirrorY = true;
#else
constexpr bool kDisplayMirrorY = false;
#endif
#if CONFIG_ENGLISH_EDGE_LCD_SWAP_XY
constexpr bool kDisplaySwapXY = true;
#else
constexpr bool kDisplaySwapXY = false;
#endif
constexpr int kDisplayPins[] = {CONFIG_ENGLISH_EDGE_LCD_SCK, CONFIG_ENGLISH_EDGE_LCD_MOSI,
                                CONFIG_ENGLISH_EDGE_LCD_RST, CONFIG_ENGLISH_EDGE_LCD_DC,
                                CONFIG_ENGLISH_EDGE_LCD_CS,  CONFIG_ENGLISH_EDGE_LCD_BL};
constexpr bool ValidDisplayPins() {
    for (unsigned i = 0; i < sizeof(kDisplayPins) / sizeof(kDisplayPins[0]); ++i) {
        const int pin = kDisplayPins[i];
        if (i == 5 && pin == -1) {
            continue;
        }
        // Keep native USB, UART console, strapping and the verified octal
        // memory interface available. Board routing still needs verification.
        if (pin < 0 || !GPIO_IS_VALID_OUTPUT_GPIO(pin) || pin == 0 || pin == 3 || pin == 19 ||
            pin == 20 || (pin >= 26 && pin <= 37) || pin == 43 || pin == 44 || pin == 45 ||
            pin == 46) {
            return false;
        }
        for (int audio_pin : kPins) {
            if (pin == audio_pin) {
                return false;
            }
        }
        for (unsigned j = 0; j < i; ++j) {
            if (pin == kDisplayPins[j]) {
                return false;
            }
        }
    }
    return true;
}
static_assert(ValidDisplayPins(), "LCD pins must be distinct and avoid audio/BOOT/memory/USB/UART");
static_assert(CONFIG_ENGLISH_EDGE_LCD_OFFSET_X + CONFIG_ENGLISH_EDGE_LCD_WIDTH <= 240 &&
                  CONFIG_ENGLISH_EDGE_LCD_OFFSET_Y + CONFIG_ENGLISH_EDGE_LCD_HEIGHT <= 320,
              "LCD visible area and offset exceed the ST7789 RAM window");
#endif
}  // namespace EnglishEdgeConfig
