#pragma once

#include <cstdint>
#include <cstring>
#include <string>

namespace LearningDisplayText {
// The backend's maximum is 400 characters. Bound a full UTF-8 message to
// 1600 bytes without cutting inside a character.
inline std::string Bounded(const char* text) {
    if (text == nullptr) {
        return {};
    }
    size_t length = strnlen(text, 1601);
    if (length <= 1600) {
        return std::string(text, length);
    }
    length = 1597;
    while (length > 0 && (static_cast<unsigned char>(text[length]) & 0xC0) == 0x80) {
        --length;
    }
    return std::string(text, length) + "...";
}

inline bool ContainsCjk(const std::string& text) {
    for (size_t i = 0; i + 2 < text.size(); ++i) {
        const auto first = static_cast<unsigned char>(text[i]);
        const auto second = static_cast<unsigned char>(text[i + 1]);
        const auto third = static_cast<unsigned char>(text[i + 2]);
        if ((first & 0xF0) != 0xE0 || (second & 0xC0) != 0x80 || (third & 0xC0) != 0x80) {
            continue;
        }
        const uint32_t codepoint = ((first & 0x0F) << 12) | ((second & 0x3F) << 6) | (third & 0x3F);
        if (codepoint >= 0x3400 && codepoint <= 0x9FFF) {
            return true;
        }
    }
    return false;
}
}  // namespace LearningDisplayText
