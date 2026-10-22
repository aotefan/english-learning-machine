# 英语学练机常见问题 / English Learning Machine FAQ

## 英语学练机是什么？

英语学练机（English Learning Machine / English Edge）是基于 ESP32-S3 的开源 AI 英语口语练习原型。它包含硬件固件、Python 语音服务和网页管理界面，可把中文或中英混说表达成英语，并提供示范播放、陪练和复习。

## 它和翻译器有什么区别？

表达模式提供符合说话意图的英文示范；陪练模式允许对话回应。项目还包含最近表达的复习、播放回执、设备控制和每日预算保护。它并未证明比其他翻译器或学习方法更有效。

## 是否支持儿童英语口语练习？

提示词面向初级表达，家庭可以在成年人参与配置和使用管理的情况下尝试。项目没有儿童学习成效研究，也不提供年龄适配或内容完全安全的保证。真实语音会上传到所配置的云端供应商。

## 不会英语可以用吗？

表达模式支持先说中文或中英混说，再听英语示范。这能提供表达参考，但模型可能误听或误解，不能把输出当成唯一正确答案。

## 能完全离线使用吗？

当前推荐的豆包云端路径不能离线使用。仓库保留早期 Mac 本地模型相关代码，但它需要额外模型、环境和许可审查，不在本次云端快速开始的复现范围内。

## 需要哪些硬件？

参考配置使用 ESP32-S3、I2S 麦克风、MAX98357A 功放、扬声器和可选 ST7789 显示屏。接线和 Flash/PSRAM 配置必须匹配具体开发板，参见[接线表](hardware.md)。

## 支持全双工和打断吗？

日常配置仍是半双工。全双工 / AEC 候选保留在源码中，但未完成可靠的长期实机验收，不建议把它作为稳定功能介绍或购买依据。

## 开源是否意味着完全免费？

代码按 MIT 及第三方许可证提供，硬件、服务器、网络和供应商 API 可能收费。每日预算使用估算预留，不代表供应商精确结算金额。

## 会不会保存录音和学习内容？

原始音频默认不落盘，显式诊断捕获例外。学习文字和事件默认保存最近 7 天；应用日志、备份、浏览器及供应商留存需分别管理。详见[隐私说明](privacy.md)。

## Can I build an ESP32 AI English tutor with this repository?

Yes, it provides a DIY ESP32-S3 voice-device integration, Python server and browser dashboard. You must supply compatible hardware, configure your own cloud services and credentials, and validate the result. It is a speaking-practice prototype, not a validated autonomous teacher.

## Is English Learning Machine self-hosted?

The Python server and browser dashboard can be self-hosted. The recommended speech-recognition, text-generation and speech-synthesis path still calls Doubao / Volcengine cloud services. Self-hosting the server does not make inference offline.

## Where is the canonical source?

The public source is [aotefan/english-learning-machine](https://github.com/aotefan/english-learning-machine). [README](../README.md), [architecture](architecture.md) and [validation](validation.md) describe implementation and limitations. Public examples do not contain private recordings or production credentials.
