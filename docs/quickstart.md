# 英语学练机安装：先运行网页，再连接 ESP32-S3

本指南对应公开源码快照。先在本机用 STANDBY 检查配置和登录，再开启收费服务。硬件不是查看网页的前提。

## 1. 准备环境

使用 Git、Python 3.11 和 libopus。macOS 可执行 `brew install opus`；Debian/Ubuntu 可执行 `sudo apt-get install libopus0 python3-venv`。Python 3.11 是当前依赖锁的验证目标；这里不宣称所有 Python 版本均兼容。

```sh
git clone https://github.com/aotefan/english-learning-machine.git
cd english-learning-machine
python3 scripts/restore_forks.py
python3.11 -m venv .venv-cloud
.venv-cloud/bin/python -m pip install -r requirements-cloud.lock
```

恢复脚本只从两个公开上游克隆固定提交，再应用 `integrations/manifest.json` 中有 SHA-256 的源码覆盖文件。需要网络。已有目录有修改时脚本会拒绝覆盖。

## 2. 创建自己的私密配置

```sh
python3 scripts/init_private_config.py
```

命令创建权限为 700 的 `.secrets/`，内含权限为 600 的配置文件。设备令牌和管理密码随机生成，不显示在命令输出；读取 `.secrets/admin-login.txt` 获取本机登录密码。若目录已存在则拒绝覆盖。

默认身份 `local-test-device` 仅供本地检查。部署真实设备前，可在新目录使用 `--device-id <实际设备标识> --url wss://your-domain.example/xiaozhi/v1/`。设备端身份和服务器注册表必须对应，见[硬件配置](hardware.md)。

## 3. 本机启动，不调用付费模型

```sh
.venv-cloud/bin/python scripts/run_cloud.py \
  --env-file .secrets/cloud.env \
  --devices-file .secrets/devices.json \
  --admin-file .secrets/admin.json \
  --state-dir artifacts/local-state
```

访问 <http://127.0.0.1:18001/login>。健康检查为 <http://127.0.0.1:18000/healthz>。未连接设备时显示离线；STANDBY 拒绝语音生成属于预期行为。Ctrl+C 停止服务。

## 4. 开启真实语音

在 `.secrets/cloud.env` 中填写自己的语音 API Key、方舟 API Key、可用文本模型 ID、支持英文的标准 TTS 音色，以及实际账户费率、每日轮数和金额上限。字段说明见 [cloud.env.example](../cloud.env.example)。示例模型 ID 需以自己账户的可用产品为准。

确认语音上传和费用后，将 `DOUBAO_LIVE_ENABLED=1`，并在启动命令末尾添加 `--live`。这两个开关都启用时才会调用真实服务。此项目的预算保护不替代供应商账单；免费额度和其他程序调用不在本地控制范围内。

API Key 只配置在服务端。ESP32 存放自己专用的设备令牌，不存放供应商 API Key。

## 5. 接硬件或部署服务器

按[硬件说明](hardware.md)核对板型、接线、固件变体、备份与独立设备凭据。公网使用自己的 HTTPS/WSS 域名，并配置管理页同源校验，见[部署说明](deployment.md)。

## 常见故障

- 缺少 libopus：安装系统库，再运行服务；不要仅安装 Python 包。
- 私密文件权限错误：本机执行 `chmod 600 .secrets/*`。
- STANDBY 没有声音：默认禁止推理，先完成第 4 步。
- 设备离线：检查设备 URL、TLS、网络、设备 ID 和令牌哈希是否一致。
- 供应商拒绝请求：检查当前账户权限、模型、资源和音色；不自动改用其他供应商。
- 首次恢复失败：查看 Git 网络错误；不要把原来的私人 Git bundle 放进公开仓库。

## 本地测试

```sh
.venv-cloud/bin/python scripts/run_tests.py
PATH="$PWD/.venv-cloud/bin:$PATH" python -m unittest discover -s upstream/firmware/scripts/tests -v
```

云端测试使用模拟模型和临时数据，不连接你的供应商账户；部分历史本地模型测试可能需要可选依赖。实际发布时执行的测试范围见[验证说明](validation.md)。
