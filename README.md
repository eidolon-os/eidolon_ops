# eidolon-ops

`eidolon-ops` 是 Mac 开发 Host 与 Raspberry Pi 产品 Host 的统一管理入口。两端使用相同的路径角色、
生命周期命令和诊断模型；区别只在 Host profile 与执行适配器（Mac 是本地 supervisord，Pi 是远程
systemd）。`eidolon-pi` 暂时保留为 Pi 发布底层兼容入口，不再作为顶层操作界面。

对一台刚刷好系统、已开放 SSH 的新 Pi，下面一条命令会完成基础环境检测/安装、精确提交发布、
Data V2 初始化、15 个产品服务启动，并要求 Host 达到手机 App commissioning 门禁：

```bash
uv run eidolon-ops --config config/hosts/pi5.toml \
  install --release-id 20260807-product-1 --apply
```

“新 Pi”从 Raspberry Pi OS 已刷盘、SSH host key 已可信且操作账号具备 non-interactive sudo 开始；本工具
不写 SD 卡镜像，也不自动制造云端 provider credential。Host identity、service token 和产品 settings
来自 Mac 上 14 个 mode-0600 输入文件，值不会进入 TOML、argv、bundle、receipt 或诊断元数据。

首次使用先从三个组件现有的本机 provider `.env` 白名单导入外部 LLM/STT/TTS key，并生成其余内部
credential、32-byte raw Ed25519 Host identity 和精确提交派生的 Pi settings：

```bash
uv run eidolon-ops --config config/hosts/pi5.toml init-inputs
```

该命令只读 Agent/Channel/Memory 的固定 provider key 名，不复制它们已有的内部 token；Data/Kernel、
Admin/Local API、Agent/Channel、Memory 与 LiveKit 的共享 token 在一次本地事务中重新生成。14 个文件
原子写入同一 mode-0700 目录，文件为 mode-0600，已有完整目录只验证不读取，partial/extra 文件或模板
漂移一律拒绝，永不覆盖。

## 完整产品范围

正式后端是 15 个 systemd unit：Bootstrap、eidolond、Data、Data Workspace、Hub、Kernel、Local API、
Admin、NATS、LiveKit、Memory Supervisor、Memory Discovery、Agent、Channel Provider、Channel。发布输入固定为 8 个完整
Git commit；7 个运行 component 一起切换，SDK 只作构建输入。

这套后端支持手机 App 对 Host 的 BLE/Wi-Fi/Host proof/pinned HTTPS/claim/Workspace onboarding 管理路径。
`app-ready` 是 Host 侧门禁，不是假装跑过真实手机。`client-web`、Audit worker、Vision 和手机安装包本身
不是 Pi 产品 unit；它们不属于“手机 App 可管理 Host”所需的后端范围。

当前矩阵已纳入正式 `eidolon-channel-provider`（8767）以及 Admin 的 Mobile onboarding target/admission
契约。剩余产品级硬门禁是 LAN transport：Hub 还只有 loopback 8082 明文监听，却会宣告 HTTPS
endpoint；LiveKit 的设备地址也必须是设备可验证的 WSS。release 尚未拥有 TLS 终止器和对应证书，
因此 15 个 unit 即使全部健康，也只能报告为后端运行，不能报告为“所有设备可对话”。Ops 不会关闭
证书校验、从 mDNS 建立信任或用临时兼容服务掩盖缺口。

## 基础环境 profile

`raspberry-pi-os-debian-arm64-v2` 会先只读检测，再在 `--apply` 时通过受限的 Debian 官方登记 HTTPS 镜像安装：

- Debian/Raspberry Pi OS 13、aarch64、真实 Raspberry Pi model、systemd PID 1；
- 8 GiB-class RAM 与至少 12 GiB 可用磁盘；
- BlueZ、NetworkManager、Avahi、FFmpeg、Git LFS、SQLite、编译与音频/运行库；
- SHA-256 固定的 NATS Server 2.14.0、LiveKit Server 1.11.0、Node 22.23.2；
- hash-pinned uv 0.11.15（避开 0.11.14 已公开的 entry-point path traversal 漏洞）；
- `bluetooth.service`、`NetworkManager.service`、`avahi-daemon.service` enabled + active。

Python 缺失时，CLI 通过受限 shell bootstrap 先验证同一硬件/OS/容量门禁，再安装 Python。下载使用固定
URL/digest、原子缓存和版本目录。`rsync` 只用于带 release digest marker 的 `/var/tmp` 不可变发布暂存，
支持断线续传且不会覆盖任何工作树。Foundation 每个阶段与失败原因写入
`/var/lib/eidolon-ops/foundation-v2.json`，与产品 authority namespace 隔离，可诊断、可幂等重试。

## 安装与配置

```bash
cd /path/to/eidolon/eidolon_ops
uv sync --all-extras
cp config/eidolon-pi.example.toml config/eidolon-pi.toml
cp config/hosts/pi5.example.toml config/hosts/pi5.toml
cp config/hosts/mac.example.toml config/hosts/mac.toml
chmod 600 config/eidolon-pi.toml
```

Mac 还必须安装 `git-lfs`；bundle 只从 exact commit pointer 导出 Channel 模型并验证 LFS object digest，
不会读取 Channel working tree 中的 hydrated 文件。

配置显式固定 foundation profile、目标/SSH、8 个 repo/commit、15 个 unit、authority 数据路径、14 个
私密输入文件，以及 target-native Python 索引/超时/重试/并发策略。Python 依赖仍由各仓库的 frozen
`uv.lock` 精确约束；替代 HTTPS 索引不能改写 lock 中已有的 direct artifact URL，也不允许重新解析
版本。SSH 强制 BatchMode、独立 key、`StrictHostKeyChecking=yes` 和显式 known_hosts。
示例见 [`config/eidolon-pi.example.toml`](config/eidolon-pi.example.toml)。

## 统一路径契约

| 生命周期 | Mac profile | Pi profile |
|---|---|---|
| 代码/发布 | `~/ai/eidolon` 工作树 | `/opt/eidolon/{releases,current}` |
| Host 配置 | `eidolon_ops/config` | `/etc/eidolon` |
| 持久状态 | `~/eidolon/data` | `/var/lib/eidolon` |
| 临时运行态 | `~/eidolon/run` | `/run/eidolon` |
| 日志 | `~/eidolon/logs` | `/var/log/eidolon`（并保留 journal） |
| 缓存/诊断原始件 | `~/eidolon/cache` | `/var/cache/eidolon` |
| Bootstrap 状态 | `~/eidolon/bootstrap` | `/var/lib/eidolon-bootstrap` |
| Bootstrap 临时运行态 | `~/eidolon/run/bootstrap` | `/run/eidolon-bootstrap` |

根目录下面按 authority/组件继续隔离，不能自行再发明路径：

| 相对 `$EIDOLON_STATE_ROOT` | Owner / 内容 |
|---|---|
| `eidolon-system.sqlite3`、`objects/` | Data 的系统权威库与对象存储 |
| `hub/` | Hub 独占 SQLite |
| `agent/` | Agent 独占状态与 SQLite |
| `memory/` | Memory palace、ledger 与组件状态 |
| `nats/` | NATS JetStream |
| `voiceprints/` | Channel/语音身份资产 |
| `admin/`、`audit/` | Admin 控制面状态与可重建审计索引 |
| `registry/` | SDK registry |
| `ops/`、`deployments/` | Ops/foundation 与 Kernel release 事务证据 |

`$EIDOLON_RUNTIME_ROOT`、`$EIDOLON_LOG_ROOT` 和 `$EIDOLON_CACHE_ROOT` 同样使用组件子目录。Mac
开发所需的组件 settings/secret 仍由各组件工作树持有，Ops 只拥有 Host profile、端口和启用集合；Pi
发布则把经过声明的私密输入物化为 `/etc/eidolon/*.env|*.yaml`。两者由同一 CLI 和路径角色驱动，不把
开发工作树布局伪装成产品 FHS 布局。

`/opt` 只放不可变产品代码与 active symlink；业务状态不进入 `/opt`。`/srv` 不再使用，因为这里没有
由机器对外提供、需要独立管理的 service data tree。组件不得再从 `HOME` 拼接产品路径；Ops 将 profile
解析为同一组 `EIDOLON_*_ROOT` 环境变量。Bootstrap 单独保留状态域，以维持首次初始化、reset、换网和
Owner 变更的权限边界；它的 state/runtime 目录均不与产品主进程共享 ownership。

## 唯一入口与操作

```text
eidolon-ops --config HOST.toml status|doctor
eidolon-ops --config HOST.toml migrate-paths [--apply]  # Mac one-time state cutover
eidolon-ops --config HOST.toml start|stop|restart [--dry-run]
eidolon-ops --config HOST.toml logs [--service SERVICE] [--lines N] [--since TEXT]

# Mac-only isolated profiles
eidolon-ops --config HOST.toml core-contract start|stop|restart|status
eidolon-ops --config HOST.toml os-control-plane prepare|validate|start|stop|restart|status

# Pi release/install capabilities
eidolon-ops --config HOST.toml provision [--apply]
eidolon-ops --config HOST.toml init-inputs
eidolon-ops --config HOST.toml install --release-id ID [--resume] [--apply]
eidolon-ops --config HOST.toml reset [--wipe-authority-data] [--apply]
eidolon-ops --config HOST.toml install --release-id ID \
  --reset-existing --wipe-authority-data [--apply]
eidolon-ops --config HOST.toml deploy|update --release-id ID [--resume] [--activate]
eidolon-ops --config HOST.toml app-ready
eidolon-ops --config HOST.toml rollback --release-id ID --snapshot /var/lib/eidolon/deployments/... [--apply]
eidolon-ops --config HOST.toml diagnose --output /absolute/path/to/report.tar.gz
```

所有有破坏性的入口默认计划/dry-run；`install --apply` 只接受全新 Eidolon namespace。旧部署不再走
`/srv` 兼容迁移：先用 `reset` 查看精确删除范围；需要一条命令全新重装时，显式同时传入
`--reset-existing --wipe-authority-data --apply`。这会永久删除 Eidolon/Bootstrap 权威数据，基础系统包、
固定版本 NATS/LiveKit/Node/uv 和 service identity 保留并重新门禁。单独 `reset --apply` 默认只删除代码、
unit、配置和运行态，保留 `/var/lib`；它不会让已有数据自动兼容新 schema。`deploy/update` 必须追加
`--activate` 才切换，`rollback` 必须追加 `--apply` 才恢复。详细状态机见
[`docs/runbook.md`](docs/runbook.md)，代码证据与方案选择见
[`docs/architecture-audit.md`](docs/architecture-audit.md)，真实验证结果见
[`docs/verification.md`](docs/verification.md)。
