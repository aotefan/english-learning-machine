# Third-party notices and source provenance

Original integration code is provided under the root MIT license. That license does not replace upstream licenses or grant rights to hosted services, model weights, speech assets or trademarks.

| Component | Source / pinned revision | License and scope |
| --- | --- | --- |
| XiaoZhi ESP32 firmware | [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32), `0d576d3d4c049c6f55eaf879725dc23e516511b4` | MIT; original notice in `integrations/firmware/UPSTREAM_LICENSE` |
| XiaoZhi Python server | [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server), `87c6df77b0220ddbd8fc804f984f83f69f3225e5` | MIT; original notice in `integrations/backend/UPSTREAM_LICENSE` |
| ESP-IDF and downloaded firmware components | Restored upstream dependency manifests | Each component's own LICENSE/NOTICE applies; review the actual build |
| Doubao / Volcengine services | Configured by the deployer | Hosted APIs and voice assets are not licensed by this repository |
| Python dependencies | `requirements-cloud.lock` | Each package retains its own license |
| Legacy optional local models | Local model utilities in `scripts/` | Model weights and transitive runtimes have separate licenses; no weights bundled |

Files under `integrations/*/files/` are modified or added sources based on the pinned upstream revision. They are distributed as transparent source overlays rather than private Git bundles. `integrations/manifest.json` records each file's SHA-256. The restore script downloads original public sources and their license notices.

Some optional speech/model runtimes can introduce GPL or other obligations. Firmware assets, fonts, ESP-specific libraries and downloaded models must be reviewed before redistributing a compiled device image. This source release does not claim that a complete commercial hardware distribution has been cleared.
