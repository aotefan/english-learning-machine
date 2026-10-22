# 英语学练机工作原理与源码导航

英语学练机把“表达一个意思”和“参与英语对话”作为两个不同模式。表达模式关注保留用户意图的英语生成；陪练模式允许模型回应内容。云端服务负责识别、生成与合成，ESP32-S3 负责采集、显示和播放。

## 一轮语音的数据流

1. 设备按键或 VAD 收音，经 Opus/WebSocket 向服务器发送音频。
2. 服务端验证设备令牌，并在有效语音开始时保守预留本轮预算。
3. 音频在录音期间上传至 Seed-ASR，收到完整回合结束信号后提交生成。
4. 方舟模型根据模式生成英文。自然正确的纯英文在表达模式下允许静默结束。
5. 英文按完整语义句进入双向 TTS，PCM 转 Opus 后返回设备。
6. 设备播放并回报状态，服务器据此保存可复习的表达，避免把未播放的内容当成已听过。

## 数据与控制

`cloud_service.py` 管理鉴权、预算、设备状态和学习事件；`cloud_learning.py` 管理复习数据；`device_control.py` 经已有 MCP 通道发出控制，音量和输入模式以真实设备回执为准。

`debug_page.py` 提供登录、学习、费用与监听页面，页面源文件位于 `debug/`。默认 SQLite 数据留在本机指定的状态目录，不随代码发布。

## 源码位置

| 关注点 | 源码 |
| --- | --- |
| 云端启动及配置 | `scripts/run_cloud.py`、`scripts/cloud_setup.py` |
| 预算及学习 | `scripts/cloud_budget.py`、`scripts/cloud_learning.py` |
| 单轮语音逻辑 | `integrations/backend/files/main/xiaozhi-server/core/handle/englishMvp.py` |
| 豆包识别及合成 | 覆盖文件中的 `core/providers/asr/doubao_seed.py` 和 `core/providers/tts/doubao_seed.py` |
| 英语生成 | 覆盖文件中的 `core/providers/llm/doubao_english/doubao_english.py` |
| 板级与屏幕 | `integrations/firmware/files/main/boards/english-edge-s3/` |
| 播放与监听状态 | 固件覆盖文件中的 `main/application.cc`、`main/audio/` |

## 功能边界

正常配置为半双工；播放结束后恢复监听。包含全双工候选代码不代表回声消除和双讲已经可靠。当前学习记录是最近使用情况与用户自评，不是语言能力测评。延迟要区分服务端首包、客户端首包和真实扬声器出声，不能互相替代。
