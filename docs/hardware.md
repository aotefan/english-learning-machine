# ESP32-S3 英语学练机硬件、接线与固件

适用：参考固件 `english-edge-s3-evb-cloud-vad-st7789`，ESP32-S3 开发板、六针 I2S 麦克风、MAX98357A 功放和 ST7789 显示屏。

**先拔掉 USB、充电宝等所有供电，再拆线和接线。GPIO 数字按开发板丝印找，不是排针的顺序编号。**

## 一、电源和地线

开发板通过 **USB-UART 接口**供电。面包板分别设置三条电源线：

| 开发板引脚 | 分配用途 |
| --- | --- |
| **3V3** | 麦克风 VDD、屏幕 VCC、屏幕 BL |
| **5V** | 功放 VIN、功放 SD |
| **GND** | 麦克风 GND、麦克风 L/R、屏幕 GND、功放 GND、功放 GAIN |

**3.3V 和 5V 必须使用不同的电源轨，不能连在一起。所有模块共用 GND。**

## 二、麦克风

按 I2S 麦克风模块实际引脚标识连接：

| 麦克风引脚 | 接到开发板 |
| --- | --- |
| VDD | **3V3** |
| GND | **GND** |
| L/R | **GND** |
| SCK | **GPIO2** |
| WS | **GPIO1** |
| SD | **GPIO42** |

麦克风的 **SD 是音频数据输出**，接 GPIO42。

## 三、MAX98357A 功放和扬声器

### 功放接线

| 功放引脚 | 接到 |
| --- | --- |
| VIN | 开发板 **5V** |
| GND | 开发板 **GND** |
| BCLK | 开发板 **GPIO40** |
| LRC / LRCLK | 开发板 **GPIO41** |
| DIN | 开发板 **GPIO39** |
| SD / SD_MODE | 功放 **VIN**，即同一条 **5V** 电源 |
| GAIN | **GND** |

功放 SD 接 VIN 选择左声道，GAIN 接 GND 沿用当前增益设置。[MAX98357A 官方手册](https://www.analog.com/media/en/technical-documentation/data-sheets/max98357a-max98357b.pdf)

**功放的 SD 和麦克风的 SD 用途不同，按各自表格连接。麦克风与功放的时钟线也分别连接，不要合并。**

### 扬声器接线

| 扬声器导线 | 功放绿色接线端子 |
| --- | --- |
| 红线 / 正极 | **＋** |
| 黑线 / 负极 | **－** |

**扬声器负极接功放的“－”端子，不能接开发板 GND。** 功放的两个扬声器输出都是驱动端。[功放输出说明](https://learn.adafruit.com/adafruit-max98357-i2s-class-d-mono-amp/pinouts)

## 四、ST7789 显示屏

| 屏幕引脚 | 接到开发板 |
| --- | --- |
| GND | **GND** |
| VCC | **3V3** |
| SCL | **GPIO14** |
| SDA | **GPIO38** |
| RST | **GPIO21** |
| DC | **GPIO47** |
| CS | **GPIO48** |
| BL | **3V3** |

屏幕沿用当前的 **3.3V 供电**。SCL、SDA 是 SPI 时钟和数据接口。[同类屏幕资料](https://www.lcdwiki.com/1.54inch_IPS_Module)

逐条检查 GPIO 到模块的实际导通，不要只根据面包板孔位推断。

## 五、面包板连接规则

- 常见面包板同一编号排的 **a–e 五孔相通**，**f–j 五孔相通**。
- 中间沟槽两边不相通。
- 不同信号线分别占用不同的连通孔组。
- 长电源轨可能在中间断开；两块面包板拼在一起不会自动连通，需要跳线连接。

红、蓝标线只是标记。接入电源前，先用断电蜂鸣档确认电源轨的实际连通范围，再分别标好 **3.3V、5V、GND**。

## 六、重新连接的顺序

1. 先接电源分配和屏幕。
2. 通电检查是否显示文字，再断电。
3. 接麦克风、功放和扬声器。
4. 检查完整接线后再通电。
5. 设备待机后，长按 **BOOT 两秒**测试本地喇叭提示音；双击 BOOT 开启自动监听。

**BOOT、EN/RST 都是板载按钮，不需要外接线。摄像头当前固件未启用，先留空。**


## 固件构建与设备身份

此表只对应 `english-edge-s3` 参考板型；其他开发板不能直接照搬引脚。先核对 16 MB Flash、PSRAM 和电源条件。使用公开上游要求的 ESP-IDF 6.1（最低 6.0.1；不使用 5.x），从已安装的 SDK 环境构建：

```sh
python3 scripts/restore_forks.py
cd upstream/firmware
python3 scripts/build.py english-edge-s3 --name english-edge-s3-evb-cloud-vad-st7789 --wake-word disabled
```

无屏可选 `english-edge-s3-evb-cloud-vad`，按键版可选 `english-edge-s3-evb-cloud-button`。`duplex` 和 `diagnostic` 名称的变体供实验使用，不是默认日常配置。

固件从单独的 NVS 配置读取设备 ID、令牌和 WSS 地址。`scripts/provision_cloud_device.py` 需要实际设备 MAC、指定布局的已验证备份、ESP-IDF 路径和自己的 `--url`；它拒绝覆盖非空保留区，仅生成私密文件，不会刷机或注册服务器。

```sh
.venv-cloud/bin/python scripts/provision_cloud_device.py --help
```

此预配脚本针对已有参考板分区迁移，不能直接用于任意出厂开发板。先核对分区和备份，按官方 ESP-IDF/esptool 流程烧录与配置。不要在不明白 Flash 偏移的情况下运行写入命令。

预配生成的 `credential.json` 含设备明文令牌，只供自己的设备使用；将该设备 ID 对应的令牌 SHA-256 加到私有 `devices.json`。每台设备独立令牌，供应商 Key 不进入固件。生成的 NVS 镜像和备份不能上传到 GitHub。
