# 自托管部署与 TLS

先完成[本机快速开始](quickstart.md)。部署需自己的 Linux 服务器、域名、TLS 和供应商账户。示例域名 `english.example.com` 不提供公共服务。

## 服务和配置

`deploy/compose.yaml` 只把 18000（设备语音）和 18001（管理页）绑定到服务器 loopback。容器为非 root、只读根目录；状态目录需允许 UID/GID 10001 写入。`deploy/Caddyfile` 代理 WSS 与已鉴权的管理路由。

部署前替换 `deploy/Caddyfile` 的域名，并同步修改 `deploy/compose.yaml` 的 `ADMIN_PUBLIC_ORIGIN`。使用相同的 HTTPS 源，否则网页写操作会被同源保护拒绝。

在服务器 `/opt/english-edge/secrets/` 放置自己的 `cloud.env`、`devices.json`、`admin.json`，按容器读取身份设置最小权限；使用 `/opt/english-edge/state/` 保存私有状态。不要把这些文件加入镜像或仓库。

## 构建与现有部署工具

```sh
.venv-cloud/bin/python scripts/prepare_cloud_wheels.py
.venv-cloud/bin/python scripts/package_cloud.py artifacts/release-source.tar.gz
```

打包程序采用文件白名单并校验 Python wheel 哈希。Linux wheel 目标当前为 x86_64 / CPython 3.11。部署 Dockerfile 使用固定镜像摘要；上线前应重新核对镜像和依赖安全更新，不把固定摘要视为永久安全承诺。

`deploy_ecs.py` 和 `deploy_gateway.py` 面向既有 `/opt/english-edge` 目录约定，需显式提供 `--host`，并预先准备自己的 SSH 私钥和 known_hosts。使用 `--help` 查看参数；先阅读脚本再用于自己的服务器。它们不是通用的一键云资源购买工具。

当前仓库开源发布仅发布源码和文档，不变更任何私人线上实例，不上传服务密钥，也不替部署者开启真实收费模式。
