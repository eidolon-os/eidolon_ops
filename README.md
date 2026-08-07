# eidolon-ops

`eidolon-pi` 是 Mac 上管理已审阅 Eidolon OS Raspberry Pi 节点的唯一运维入口。它不复制
Kernel 的发布事务：精确提交归档、target-native 环境、sealed descriptor、切换、健康门禁和回滚
继续由 `eidolon_kernel/eidolon_deploy` 执行；本项目只拥有工作站编排、首次 Eidolon 装机、远程
生命周期、日志和脱敏诊断。

当前固定产品拓扑是 Data、Data Workspace、Hub、Kernel、eidolond、Admin、Bootstrap 和 Local API。
Agent、Channel、Memory、NATS、LiveKit 和 Web client 尚无已审阅的产品 systemd/release contract，
因此本 CLI 不把 macOS supervisord 开发拓扑伪装成 Pi 产品拓扑。

## 安装与入口

```bash
cd /Users/manson/ai/eidolon/eidolon_ops
uv sync --all-extras
uv run eidolon-pi --config config/eidolon-pi.toml doctor
```

配置从严格 TOML 读取。SSH host key、目标、repo path、完整 40-hex commit、服务集合、远端数据目录和
secret 文件来源都必须显式声明；secret 值不进入配置、argv、bundle、receipt 或日志。示例见
[`config/eidolon-pi.example.toml`](config/eidolon-pi.example.toml)。

## 命令

```text
eidolon-pi status
eidolon-pi doctor [--release-id ID]
eidolon-pi install --release-id ID [--resume] [--apply]
eidolon-pi deploy  --release-id ID [--resume] [--activate]
eidolon-pi update  --release-id ID [--resume] [--activate]
eidolon-pi start|stop|restart [--dry-run]
eidolon-pi rollback --release-id ID --snapshot /var/lib/eidolon/deployments/... [--apply]
eidolon-pi logs [--unit UNIT] [--lines N] [--since TEXT]
eidolon-pi diagnose --output /absolute/path/to/report.tar.gz
```

- `install` 默认只输出计划；`--apply` 才会安装到一台没有 Eidolon authority 的主机。它要求 Raspberry
  Pi OS 已有 SSH、systemd、Python 3 和固定路径的 `uv`，但会创建 Eidolon 用户/目录、注入前置文件、
  建立全新 Data V2 baseline、安装系统资产并启动正式拓扑。
- `deploy/update` 默认完成 bundle、传输、Pi 原生 prepare/seal 和 activation dry-run；只有
  `--activate` 才切换服务。`--resume --activate` 复用已准备 release。
- `rollback` 只恢复 Kernel descriptor 允许的系统资产和 component links，不恢复 secret 或数据库。
- `diagnose` 只采集主机、unit、link、receipt 和 journal 元数据；不读取 env 内容、Host private key 或
  authority 数据库内容。journal 仍可能含业务日志或用户标识，生成的压缩包必须按运维敏感资料保存。

首次装机和日常更新详见 [`docs/runbook.md`](docs/runbook.md)。当前验证结论及真实 Pi 限制见
[`docs/architecture-audit.md`](docs/architecture-audit.md) 与
[`docs/verification.md`](docs/verification.md)。
