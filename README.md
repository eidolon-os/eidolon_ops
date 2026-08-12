# eidolon-ops

`eidolon-ops` 是 Mac 开发 Host 与 Raspberry Pi 产品 Host 的统一管理入口。两端使用相同的路径角色、
生命周期命令和诊断模型；区别只在 Host profile 与执行适配器（Mac 是本地 supervisord，Pi 是远程
systemd）。`eidolon-ops` 是唯一入口；实现级诊断收敛在 `eidolon-ops debug` 之下。

## 运维权威的三层分工

Ops 是唯一入口，不是唯一实现。三层各自拥有不可替代的事实，越界即是重复实现：

| 层 | 谁 | 回答的问题 | 形态 |
|---|---|---|---|
| Operator 侧 | `eidolon-ops` | 这台 Host 应该是什么 | 工作站上按需运行，**永不常驻产品机** |
| Host 侧 | `eidolond`、`eidolon-bootstrapd` | 现在应该跑什么、Host 身份与认领状态 | 产品机常驻 |
| 组件 | Data / Hub / Kernel / Agent / Memory / Channel | 我自己怎么配置、迁移、备份、清除 | 各自权威 |

具体到发布事务与运行时：

| 动作 | Ops | `eidolon-release` | `eidolond` |
|---|---|---|---|
| 选定 release matrix（8 commit） | 唯一 | — | — |
| 封印 bundle、target prepare、原子激活、代码回滚 | 编排 | 执行原语 | — |
| 服务 enable/disable/restart、desired state、ready directory | 请求 / 消费 | — | 唯一 |
| 健康门禁判定（`app-ready`） | 唯一 | — | 提供 observed state |
| Controller Reset | 编排 | — | Bootstrap 执行 |

`eidolon-release` 与 `eidolond` 留在 `eidolon_kernel` 仓：前者随 release 分发并在目标机上执行
（回滚旧 release 用的就是那个 release 自带的激活器），后者是 15 个产品 unit 之一。Ops 只依赖
它们的契约——通过 `eidolon-release contract` 校验格式版本，通过 release 发布的
`.release/bin/{eidolon-release,python}` 寻址——不依赖任何组件目录名或 Git 工作树。

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

### `app-ready` 是一份 capability 清单，不是两套检查

`src/eidolon_ops/readiness.py` 里的 `READINESS_CONTRACT` 是**唯一**的检查集定义：一组具名事实，加上
“哪种 Host 负责作证哪一条”的表。Mac source-run 在本机求值，Pi 由下发的 agent 求值——探针必须在各自能
跑的地方，契约不必。事实集随 payload 下发（与 `port_registry` 同一模式）；agent 只允许作证它收到的那
一组，多一条少一条都 fail closed，因为“门禁悄悄少查了一项”比“门禁报红”更危险：它正是 release 回滚
所依据的那份报告。

其中三条 Channel 事实的存在是因为端口探活、unit `active` 与旧 `app-ready` 曾同时全绿，而 worker 对
LiveKit 已经不可接活：

| 事实 | 读的是什么 |
|---|---|
| `channel_worker_healthy` | worker 自己发布的 `/`：inference 进程在、且它没有放弃 LiveKit 连接。它与 LiveKit 的 job 请求走同一个事件循环，所以事件循环卡死时这一条会红，而监听 socket 仍然 accept |
| `channel_worker_dispatch_identity` | worker 自己发布的 `/worker`：它注册的 agent 名就是产品 dispatch 的那个名字 |
| `channel_worker_livekit_link` | 这台机器自己的进程与 socket 表：`eidolon-channel.service` cgroup 里确有进程持有到 LiveKit 信令端口的连接——即 worker 注册所依托的那条链路 |

Ops 只消费组件自报的健康事实和本机可观测状态，不去重建 `livekit-agents` 的内部状态。**LiveKit 自己
对该注册的看法目前没有任何组件发布**，那一条应当由 Channel 的 Component Ops Contract（`[[units]].ready`）
补齐，而不是在 Ops 里猜。

当前矩阵已纳入正式 `eidolon-channel-provider`（8767）以及 Admin 的 Mobile onboarding target/admission
契约。Ops 从 Bootstrap 使用的 Ed25519 Host identity 按与 Admin 完全相同的算法派生唯一 `ehost-*`，
再生成 `eidolon-hub-<Host suffix>` 与对应 `.local` 名称。Mac 与 Pi 使用同一规则，不再共享
`eidolon-hub-local`/`eidolon-hub.local`，IP 变化也不改变身份。

两端都由 Ops 管理稳定的 P-256 Hub 叶子证书和 8443 TLS ingress，Local API 固定证书并计算 SPKI，
ingress 转发到 loopback Hub 8082。Pi 的 15 个 release unit 不被改写；Ops 另行安装一个 Host 配置 unit
`eidolon-hub-ingress.service` 与 Hub systemd 配置 overlay。LiveKit 可在显式开发 opt-in 后使用私网
`ws://`；产品级 WSS 仍是独立门禁。这不是关闭 Mobile 的 Hub 证书校验，也不从 mDNS 学习信任。

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

配置显式固定 foundation profile、目标/SSH、8 个 repo/commit、15 个 release unit、authority 数据路径、
14 个私密基础输入文件、统一 `[app]` LAN 契约、`workspace.release_cli` 与 `workspace.uv` 两个工作站工具，
以及 Python 索引/超时/重试/并发策略。Host-bound Hub
配置、证书、ingress 与 systemd overlay 由 Ops 从 Host identity 原子生成，不进入 Git 或 release bundle。
Python 依赖仍由各仓库的 frozen `uv.lock` 精确
约束，但 Linux/aarch64 artifact 在 Mac 上预取、压缩、哈希后随 bundle 传输；Pi prepare 强制 offline，
不会因弱网重复拉包。HTTPS index URL 与 uv/build-tool 版本进入 bundle 门禁，不能在目标端漂移。SSH
强制 BatchMode、独立 key、`StrictHostKeyChecking=yes` 和显式 known_hosts。
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

每个操作先产出一份 `Plan`（`operation`/`steps`/`destructive`/`requires_flags`/`touches`），再返回
`Evidence`：Host 报告原样保留在顶层，旁边多出 `plan`、`outcome` 与 `steps`。退出码取自 `Outcome` 枚举，
不再取自 CLI 里的一张“成功词”白名单——那张表让 `commissioning-code` 与 `backup` 在 Host 上成功、在这里
退出非零。`doctor` 会列出该 Host 的 capability 集合（由 platform + transport/supervisor/packages 三个
port 的组合推导），所以“这台 Host 能做什么”是问出来的，不是从源码里读出来的。

```text
eidolon-ops --config HOST.toml status|doctor
eidolon-ops --config HOST.toml commissioning-code [--ttl-seconds 600]  # Mac Debug Host
eidolon-ops --config HOST.toml start|stop|restart [--dry-run]
eidolon-ops --config HOST.toml logs [--service SERVICE] [--lines N] [--since TEXT]

# Mac implementation diagnostics (normal lifecycle uses top-level status/start/stop/restart)
eidolon-ops --config HOST.toml debug prepare|validate|status|web-start|web-stop|web-restart|web-status

# Pi release/install capabilities
eidolon-ops --config HOST.toml provision [--apply]
eidolon-ops --config HOST.toml init-inputs
eidolon-ops --config HOST.toml install --release-id ID [--resume] [--apply]
eidolon-ops --config HOST.toml reset [--wipe-authority-data] [--apply]
eidolon-ops --config HOST.toml controller-reset [--apply]  # lost every managing phone
eidolon-ops --config HOST.toml install --release-id ID \
  --reset-existing --wipe-authority-data [--apply]
eidolon-ops --config HOST.toml deploy|update --release-id ID [--resume] [--activate]
eidolon-ops --config HOST.toml app-ready
eidolon-ops --config HOST.toml rollback --release-id ID --snapshot /var/lib/eidolon/deployments/... [--apply]
eidolon-ops --config HOST.toml diagnose --output /absolute/path/to/report.tar.gz
```

Mac 的 Host profile 可用严格的 `source_overrides.<source>` 指向一个干净、精确提交的本地 worktree；这只
改变该 Mac source-run，不改变共用的 Pi release matrix。当前开发接入使用此机制运行 Admin，生产/Pi
仍使用各自的 commit-pinned 发布输入。

Mac product-source 在 canonical 7880 上直接管理 LiveKit 进程；`external` 表示复用已验证的二进制和
凭据配置，不表示继续依赖旧 Admin supervisor。NATS 仍由 external foundation 提供并在启动前受健康门禁。

所有有破坏性的入口默认计划/dry-run；`install --apply` 只接受全新 Eidolon namespace。旧部署不再走
`/srv` 兼容迁移：先用 `reset` 查看精确删除范围；需要一条命令全新重装时，显式同时传入
`--reset-existing --wipe-authority-data --apply`。这会永久删除 Eidolon/Bootstrap 权威数据，基础系统包、
固定版本 NATS/LiveKit/Node/uv 和 service identity 保留并重新门禁。单独 `reset --apply` 默认只删除代码、
unit、配置和运行态，保留 `/var/lib`；它不会让已有数据自动兼容新 schema。`deploy/update` 必须追加
`--activate` 才切换，`rollback` 必须追加 `--apply` 才恢复。详细状态机见
[`docs/runbook.md`](docs/runbook.md)，代码证据与方案选择见
[`docs/architecture-audit.md`](docs/architecture-audit.md)，真实验证结果见
[`docs/verification.md`](docs/verification.md)。
