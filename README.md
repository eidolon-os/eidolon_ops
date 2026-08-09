# eidolon-ops

`eidolon-pi` 是 Mac 上管理 Eidolon OS Raspberry Pi 的唯一入口。对一台刚刷好系统、已开放 SSH 的
新 Pi，准备一次严格配置后，下面一条命令会完成基础环境检测/安装、精确提交发布、Data V2 初始化、
14 个产品服务启动，并要求 Host 达到手机 App commissioning 门禁：

```bash
uv run eidolon-pi --config config/eidolon-pi.toml \
  install --release-id 20260807-product-1 --apply
```

“新 Pi”从 Raspberry Pi OS 已刷盘、SSH host key 已可信且操作账号具备 non-interactive sudo 开始；本工具
不写 SD 卡镜像，也不自动制造云端 provider credential。Host identity、service token 和产品 settings
来自 Mac 上 14 个 mode-0600 输入文件，值不会进入 TOML、argv、bundle、receipt 或诊断元数据。

## 完整产品范围

正式后端是 14 个 systemd unit：Bootstrap、eidolond、Data、Data Workspace、Hub、Kernel、Local API、
Admin、NATS、LiveKit、Memory Supervisor、Memory Discovery、Agent、Channel。发布输入固定为 8 个完整
Git commit；7 个运行 component 一起切换，SDK 只作构建输入。

这套后端支持手机 App 对 Host 的 BLE/Wi-Fi/Host proof/pinned HTTPS/claim/Workspace onboarding 管理路径。
`app-ready` 是 Host 侧门禁，不是假装跑过真实手机。`client-web`、Audit worker、Vision 和手机安装包本身
不是 Pi 产品 unit；它们不属于“手机 App 可管理 Host”所需的后端范围。

## 基础环境 profile

`raspberry-pi-os-debian-arm64-v1` 会先只读检测，再在 `--apply` 时安装：

- Debian/Raspberry Pi OS 12/13、aarch64、真实 Raspberry Pi model、systemd PID 1；
- 8 GiB-class RAM 与至少 12 GiB 可用磁盘；
- BlueZ、NetworkManager、Avahi、FFmpeg、Git LFS、SQLite、编译与音频/运行库；
- SHA-256 固定的 NATS Server 2.14.0、LiveKit Server 1.11.0、Node 22.23.2；
- hash-pinned uv 0.11.15（避开 0.11.14 已公开的 entry-point path traversal 漏洞）；
- `bluetooth.service`、`NetworkManager.service`、`avahi-daemon.service` enabled + active。

Python 缺失时，CLI 通过受限 shell bootstrap 先验证同一硬件/OS/容量门禁，再安装 Python。下载使用固定
URL/digest、原子缓存和版本目录；不会用 rsync 覆盖任何工作树。Foundation 每个阶段与失败原因写入
`/var/lib/eidolon-ops/foundation-v1.json`，可诊断、可幂等重试。

## 安装与配置

```bash
cd /Users/manson/ai/eidolon/eidolon_ops
uv sync --all-extras
cp config/eidolon-pi.example.toml config/eidolon-pi.toml
chmod 600 config/eidolon-pi.toml
```

Mac 还必须安装 `git-lfs`；bundle 只从 exact commit pointer 导出 Channel 模型并验证 LFS object digest，
不会读取 Channel working tree 中的 hydrated 文件。

配置显式固定 foundation profile、目标/SSH、8 个 repo/commit、14 个 unit、authority 数据路径和 14 个
私密输入文件。SSH 强制 BatchMode、独立 key、`StrictHostKeyChecking=yes` 和显式 known_hosts。
示例见 [`config/eidolon-pi.example.toml`](config/eidolon-pi.example.toml)。

## 唯一入口与操作

```text
eidolon-pi status
eidolon-pi doctor [--release-id ID]
eidolon-pi provision [--apply]
eidolon-pi install --release-id ID [--resume] [--apply]
eidolon-pi deploy|update --release-id ID [--resume] [--activate]
eidolon-pi start|stop|restart [--dry-run]
eidolon-pi app-ready
eidolon-pi rollback --release-id ID --snapshot /var/lib/eidolon/deployments/... [--apply]
eidolon-pi logs [--unit UNIT] [--lines N] [--since TEXT]
eidolon-pi diagnose --output /absolute/path/to/report.tar.gz
```

所有有破坏性的入口默认计划/dry-run；`install --apply` 是明确的一键首次安装授权，`deploy/update` 必须
追加 `--activate` 才切换，`rollback` 必须追加 `--apply` 才恢复。详细状态机见
[`docs/runbook.md`](docs/runbook.md)，代码证据与方案选择见
[`docs/architecture-audit.md`](docs/architecture-audit.md)，真实验证结果见
[`docs/verification.md`](docs/verification.md)。
