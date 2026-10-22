# 公开源码验证范围与已知问题

验证日期：2026-10-22。此页说明公开源码快照的验证结果，不引用私人家庭录音或生产日志作为公开证据。

## 本次通过的检查

- 两个固定上游提交均可在公开 GitHub 仓库找到；源码覆盖文件的 SHA-256 校验及独立目录恢复通过。
- Python 3.11 环境下，35 个测试模块全部通过，共 283 个测试执行；包含新增私密配置初始化检查。跨模块有继承用例，这不是 283 个独立产品能力。
- ESP32 固件主机测试：114 项通过。使用 Python 3.11 环境的 `python` 可执行文件；系统 Python 环境缺少 `python` 别名时，上游 CI 选择器测试会报环境错误。
- 独立临时配置的 STANDBY 启动、健康检查、登录、监听 / 复习 / 费用页面及设备接口通过；零次真实供应商推理调用。
- 公开源码凭据扫描与已知私密值匹配检查。只发布新建的公开 Git 历史，不发布原工作仓库历史或 Git bundle。

## 尚未全部通过的测试

完整 `scripts/run_tests.py` 会如实返回失败，而不是跳过以下问题：

| 模块 | 当前问题 |
| --- | --- |
| `test_teaching_budget.py` | 两个长输入 / 多轮澄清测试的方法在子用例中触发 `english_input_exceeds_budget_bound`。当前提示词及结构化输出预算可能提前拒绝长输入，未在此次开源整理中修改运行时行为。 |
| `test_protocol.py`、`test_input_gate.py`、`test_vad.py`、`test_self_check_protocol.py` | 旧本地协议测试依赖未随公开源码分发的生成配置 `data/.config.yaml`，部分路径另依赖合成音频夹具。不能把真实私人配置或录音复制进仓库来使测试通过。 |

测试模块曾与 `scripts/` 中同名辅助脚本发生导入冲突，因此新增逐模块进程隔离的 `scripts/run_tests.py`。同时补全了旧测试中模拟 SDK 响应的 `id` / `usage` 字段，并按两种模式分别核对引述语句提示词。没有以删除断言的方式掩盖长输入预算问题。

## 本次没有验证

未重新烧录硬件、未执行真实付费语音测试、未重新构建完整固件，也没有长期噪声、真实双讲、学习成效或固定延迟结论。已保留当前半双工配置和实验全双工候选的区分。静态项目介绍页与本机 STANDBY 检查不代表所有生产部署环境均已验收。

## 复现命令

```sh
.venv-cloud/bin/python scripts/run_tests.py
PATH="$PWD/.venv-cloud/bin:$PATH" python -m unittest discover -s upstream/firmware/scripts/tests -v
```

查看单个模块可以使用 `scripts/run_tests.py --pattern test_cloud_service.py`。使用自己的隔离环境和合成数据，勿在测试时读取生产密钥。
