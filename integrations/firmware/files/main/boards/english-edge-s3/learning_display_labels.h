#pragma once

#include <cstring>

namespace LearningDisplayLabels {
inline const char* Status(const char* state) {
    if (!state)
        return "正在启动";
    if (!std::strcmp(state, "Listening"))
        return "正在监听";
    if (!std::strcmp(state, "Recording"))
        return "正在录音";
    if (!std::strcmp(state, "Ready"))
        return "未在监听";
    if (!std::strcmp(state, "Uploading"))
        return "上传中";
    if (!std::strcmp(state, "Processing"))
        return "思考中";
    if (!std::strcmp(state, "Playing"))
        return "播放中";
    if (!std::strcmp(state, "Playing + listening"))
        return "播放/监听中";
    if (!std::strcmp(state, "Audio error"))
        return "音频异常";
    if (!std::strcmp(state, "Connecting Wi-Fi"))
        return "连接网络";
    if (!std::strcmp(state, "Connecting cloud"))
        return "连接云端";
    if (!std::strcmp(state, "Wi-Fi setup"))
        return "网络配置";
    if (!std::strcmp(state, "Paused"))
        return "等待恢复";
    return "正在启动";
}
inline const char* Mode(const char* mode) {
    if (mode && !std::strcmp(mode, "chat"))
        return "陪练模式";
    if (mode && !std::strcmp(mode, "express"))
        return "表达模式";
    return "模式同步中";
}
}  // namespace LearningDisplayLabels
