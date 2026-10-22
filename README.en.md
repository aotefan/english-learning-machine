# English Learning Machine: Open-Source ESP32-S3 AI English Speaking Practice

**Turn Chinese or mixed Chinese–English speech into natural English, hear an example, and practice speaking with a DIY voice device.**

English Learning Machine (英语学练机, also called English Edge in the source) combines ESP32-S3 hardware, a Python voice server, Doubao / Volcengine speech services, and a browser dashboard. It builds on the XiaoZhi open-source firmware and server.

[中文](README.md) · [Project website](https://aotefan.github.io/english-learning-machine/en.html) · [Setup](docs/quickstart.md) · [Hardware](docs/hardware.md) · [FAQ](docs/faq.md)

## What does it do?

- **Expression mode:** turn the meaning of a Chinese or mixed-language utterance into English, preserving the speaker's intent.
- **Conversation practice:** switch modes to receive a conversational reply.
- **English correction:** provide a corrected example when needed; a model judgment that the English is already natural can end the turn silently.
- **Listen and review:** replay a recently played example, browse recent expressions and record self-assessments.
- **Device management:** control volume and input mode, and limit daily turns and estimated spending.

For example, “我想再喝一点水” could become “I'd like some more water.” This illustrates the intended behavior; it is not a fixed output or benchmark claim.

## Quick start

Install Git, Python 3.11 and system libopus first (`brew install opus` on macOS; `sudo apt-get install libopus0` on Debian/Ubuntu).

```sh
git clone https://github.com/aotefan/english-learning-machine.git
cd english-learning-machine
python3 scripts/restore_forks.py
python3.11 -m venv .venv-cloud
.venv-cloud/bin/python -m pip install -r requirements-cloud.lock
python3 scripts/init_private_config.py
.venv-cloud/bin/python scripts/run_cloud.py \
  --env-file .secrets/cloud.env \
  --devices-file .secrets/devices.json \
  --admin-file .secrets/admin.json \
  --state-dir artifacts/local-state
```

Open <http://127.0.0.1:18001/login>. Read the generated login locally in `.secrets/admin-login.txt`. Default STANDBY mode makes no paid inference calls. Clone and install commands download public software. Follow the [setup guide](docs/quickstart.md) before enabling live speech or connecting hardware.

## Hardware and architecture

ESP32-S3 → I2S microphone → Opus over secure WebSocket → Python server → Seed-ASR → Ark text model → Seed-TTS → device speaker. The reference board configuration uses 16 MB Flash and PSRAM, a MAX98357A amplifier, and an optional 240 × 240 ST7789 display. GPIOs are board-specific; follow the [wiring guide](docs/hardware.md).

The browser dashboard uses plain HTML/CSS/JavaScript and SQLite. The cloud path requires no Node.js, PHP, local GPU or downloaded model weights. `scripts/restore_forks.py` restores pinned public upstream commits and applies the reviewed source files in `integrations/`.

## Status, cost and privacy

This is a developer prototype, not a validated educational product. The recommended mode is half-duplex: listen, generate, play, then listen again. Full-duplex, AEC and simultaneous speaking remain experimental. There is no validated claim of improved proficiency, fixed response time or speech-recognition accuracy.

Cloud speech and language services require your own account and may incur charges. Voice goes to the speech provider; transcripts go to the language model. Raw audio is not saved by default; opt-in diagnostic recording is an exception. Learning text and event records expire after seven days; logs, backups and provider retention require separate management. See [privacy](docs/privacy.md) and [security](SECURITY.md).

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md), [validation scope](docs/validation.md), and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Original integration code is MIT. Upstream components, optional model weights and voice assets retain their respective licenses.

Built on [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) and [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server). No private Git history, deployment credentials, recordings, device identities or production databases are included.
