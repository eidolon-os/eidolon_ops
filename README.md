# eidolon-ops

仓库根目录的 `./eidolon` 是 Mac 开发 Host 与 Raspberry Pi 产品 Host 的统一操作入口。两端使用相同的
路径角色、生命周期命令和诊断模型；区别只在 Host profile 与执行适配器（Mac 是本地 supervisord，Pi
是远程 systemd）。脚本负责选择 Host 和展示该 Host 可用的命令，底层契约仍由 `eidolon-ops` CLI
唯一实现；实现级诊断收敛在 `debug` 之下。

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
Data V2 初始化、产品服务启动与 release doctor 校验，并记录手机 App commissioning 的就绪状态：

```bash
./eidolon pi5 install --release-id 20260807-product-1 --apply
```

“新 Pi”从 Raspberry Pi OS 已刷盘开始。那之后到“可以被 ops 操作”之间的每一件事——Host 的名字、操作
账号、免密 sudo、部署公钥、以及一条不在等一个不可能存在的 DHCP 服务器的有线链路——都由
[`bring-up`](#从刷好的盘到可以被操作) 从 Host profile 渲染出来，而不是留给谁去记得。这台 Host 的
host key 由 [`trust-host-key`](#换板子换的是信任) 记录，记在 profile 自己的 known_hosts 里。

本工具不写 SD 卡镜像，也不自动制造云端 provider credential。Host identity、service token 和产品
settings 来自 Mac 上 14 个 mode-0600 输入文件，值不会进入 TOML、argv、bundle、receipt 或诊断元数据。

首次使用先从三个组件现有的本机 provider `.env` 白名单导入外部 LLM/STT/TTS key，并生成其余内部
credential、32-byte raw Ed25519 Host identity 和精确提交派生的 Pi settings：

```bash
./eidolon pi5 init-inputs
```

该命令只读 Agent/Channel/Memory 的固定 provider key 名，不复制它们已有的内部 token；Data/Kernel、
Admin/Local API、Agent/Channel、Memory 与 LiveKit 的共享 token 在一次本地事务中重新生成。14 个文件
原子写入同一 mode-0700 目录，文件为 mode-0600，已有完整目录只验证不读取，partial/extra 文件或模板
漂移一律拒绝，永不覆盖。

## 普通更新保留身份与授权

`deploy/update` 从已安装 Host 读取数据库 marker、lineage anchor 和签名目录，确认目标自身一致；
然后用这套现有 Owner ID/generation 渲染本次精确提交的 Hub 配置。工作站 Owner 材料的 generation
不参与普通代码更新，更新也不会读取本地 issuer 来生成另一份描述。

普通更新只写 Hub/Agent/Channel/Memory 业务配置及 ingress 程序和 unit；Host identity、TLS 私钥和
证书、Owner 签名描述和根证书、配对码、`local-api.env`/`channel.env` 等凭据保持原样。预检与实际
配置写入之间再次比较保留文件摘要，发现身份变化时重试操作。Host identity 或签名入口 URI 不同
属于迁移，不能靠普通更新隐式换身份。安装、显式 authority reset/restore 继续使用对应的授权材料流程。

模型文件按本次源码提交中的 `ops/component.toml` 逐文件校验，工作站缓存和目标缓存都重算内容摘要；
已有目标目录内容冲突或损坏时不自动覆盖。新制品在目标目录旁完成校验后才发布。显式重装先完成
本地封装和模型准备，再改变 Owner 世代或 wipe；这不代表目标依赖安装和服务启动已经验证成功。

历史配置归档放在 `config/backups/`，不进入 `config/hosts/*.toml` 活动列表。OPi 本地模型备份入口为
[host.toml](config/backups/20260910/opi5max/host.toml)，它引用同目录的备份 operations 配置。

## 完整产品范围

正式后端 unit 集合由 Host capabilities 与组件契约决定，包括 Bootstrap、eidolond、Data、Hub、Kernel、
Local API、Admin、NATS、LiveKit、Memory、Agent 和 Channel 及其配套服务。发布输入固定为 8 个完整
Git commit；7 个运行 component 一起切换，SDK 只作构建输入。

这套后端支持手机 App 对 Host 的 BLE/Wi-Fi/Host proof/pinned HTTPS/claim/Workspace onboarding 管理路径。
`app-ready` 是 Host 侧门禁，不是假装跑过真实手机。`client-web`、Audit worker、Vision 和手机安装包本身
不是 Pi 产品 unit；它们不属于“手机 App 可管理 Host”所需的后端范围。

### `app-ready` 是一份 capability 清单，不是两套检查

`src/eidolon_ops/readiness.py` 里的 `READINESS_CONTRACT` 是**唯一**的检查集定义：一组具名事实，加上
“哪种 Host 负责作证哪一条”的表。Mac source-run 在本机求值，Pi 由下发的 agent 求值——探针必须在各自能
跑的地方，契约不必。事实集随 payload 下发（与 `port_registry` 同一模式）；agent 只允许作证它收到的那
一组，多一条少一条都 fail closed。普通 install/deploy 将 App-ready 作为观测，不因返回 degraded 而
回滚；发布健康由服务门禁和 release doctor 判断。authority-restore 则要求恢复后 App-ready，并在
写入恢复状态前检查当前 eidolond 能否报告所需的 LiveKit 网络事实。

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

## 从刷好的盘到可以被操作

Ops 能操作一台 Host 之前，那台 Host 必须已经有几件事成立。它们过去只写在
`docs/runbook.md` 的一句 Preconditions 里，然后交给上次做这件事的人——于是被敲进了某一台
笔记本的 imager 对话框，板子交给别人就一件都不剩。

它们全都能从 Host profile 推导，profile 自己也一直在用同一批值。所以它们是一份**声明**，
`src/eidolon_ops/bring_up.py` 里的 `BringUp`：

| 要求 | 谁需要它 |
|---|---|
| hostname 是 profile 里的短名 | `endpoints.py` 按名字解析；`transport.py` 的 `HostKeyAlias` 按名字信任 host key |
| `[host].user` 账号存在 | transport 以它连接 |
| 每个被声明的操作者公钥都在它的 authorized_keys 里 | transport 是 `BatchMode=yes` 加指定 identity，板子没被给过的 key 就是那个操作者连不上的板子 |
| 该账号免密 sudo | transport 提权用 `sudo --non-interactive` |
| sshd 与 mDNS 在跑 | transport 本身，以及名字解析 |
| 有线口在点对点线上持有地址、在真网络上取租约 | 有线上传门禁，以及 `provision` 需要的出网路径 |

这张表里**没有一条提到文件名、分区、网口名或命令**。那是刻意的：要求是产品决定，机制不是。

### 变化的只有通道，而且由板子自己的状态决定

```text
从未启动过   ── 它的启动介质，从别处写入
已经在运行   ── 它上面的一个 shell，怎么拿到的都行
```

只有这两种，因为在 ops 拥有账号之前，板子只有这两个表面。哪种介质（SD 卡、NVMe SSD、eMMC）
和哪种 shell（SSH、HDMI 键盘、串口）都到不了代码里——那是操作者的事，而把设计钉在其中一种
上，就是让“板子从 NVMe 启动”或“板子是别人烧的”变成没有出路的情况。

```bash
./eidolon pi5 bring-up --via boot-medium --output /Volumes/bootfs --apply
./eidolon pi5 bring-up --via shell       --output ~/tmp --apply
```

输出目录是**指名的，不是探测的**。指向挂载的启动介质就是准备那个介质，指向别处就得到几个
文件、用任何方式搬过去。去猜机器上哪个卷是板子的，是一个在每个人机器上会以不同方式出错的
启发式，而猜错的失败是静默的。

两种形态都是平台自己的约定——同一个 OS 既决定它的首启钩子，也决定配置一台运行中系统的命令
——所以一个平台把两者**一起**声明，按 `foundation.profile` 索引，和 `foundation.py` 给包和固
定制品用的是同一个键、同一种“查不到就带着已知选项拒绝”的写法。那个 id 里带着 OS 和修订号，
所以 OS 换了约定是新 foundation 的新声明，老板子照旧能用。目前只声明了
`raspberry-pi-os-debian-arm64-v2`：rk3588 走到这条命令会被明确拒绝，而不是拿到一份给
`eth0` 写的配置——那块板子的网口叫 `enP3p49s0`。

### 两件是问真机问出来的，不是照文档写的

**首启钩子不是 `firstrun.sh`。** Raspberry Pi OS 13 从 boot 分区 seed cloud-init，载荷是
`user-data` / `meta-data` / `network-config` 三个文件，而且正是 imager 写的那三个——所以写它
们是替换，不是冲突。种子路径由镜像自带的 `rpi-cloud-init-mods` 声明
（`/etc/cloud/cloud.cfg.d/99_raspberry-pi.cfg` 里的 `seedfrom: file:///boot/firmware`），所以
不填任何 imager customisation 也生效。

**有线口只改两个属性，两个都是量出来的。** `ipv4.link-local: "4"`（NM 的 fallback）：点对点线
上没人应答 DHCP，默认行为是 NM 让连接失败并重试，那个拆建循环正是“板子一阵能连一阵不能、
会话随时断”。`ipv6.method: "link-local"`：只改这一个键，设备状态从持续的
`connecting (getting IP configuration)` 变成 `connected`——点对点线上没有 RA 也没有 DHCPv6 供
`auto` 等待。IPv4 在两种设置下都能用，所以这正是只有真板子能告诉你的那类事实。

其余都是镜像自己的默认值，照原样写出来，好让 ops 改动的那两处是可见的而不是埋在一次重写
里。也不设 `never-default`：link-local 没有网关、抢不到默认路由，而设上它会让网线在真网络上
无法成为出网路径。运行时零代价——出厂 Host 没有网线、没有 carrier，`optional: true` 之下这
条连接根本不会激活。

### shell 形态刻意不由 ops 执行

transport 是 `BatchMode=yes` 加指定 identity，它**登不进一块还没有那把 key 的板子**——正是这
个场景。补上这个缺口意味着 ops 持有口令，而第一次打开一块板子的凭据是操作者的，该由操作者
使用。所以 ops 渲染脚本，人来跑。

脚本可以重复执行：已存在的账号不动；Ops 账号的 `authorized_keys` 收敛到入库声明，新增和撤销
操作者都会生效。网络放最后，因为 `nmcli con up` 可能掐掉当前会话。激活失败返回 75，表示配置
已写入但需要重连验证；其余检查失败返回非零。成功前检查目标账号能无交互提权到 root、SSH/mDNS
服务活跃，以及有线口确实有 IPv4 地址。写出脚本不等于板子已通过这些检查。

### 那份要求不渲染 Wi-Fi，也不渲染控制台口令

Host 自己的 Wi-Fi 由手机的 BLE 路径配置，那是产品自带的能力；ops 再渲染一份就是同一个事实的
第二条路，还要为此持有不属于它的凭据。所以 `provision` 之前的出网路径由操作者插线的地方决
定——把网线接到有 DHCP 的网络上就有（`link-local` 是 fallback，真网络上正常取租约）。

也不渲染控制台口令：`lock_passwd: true` 加 `ssh_pwauth: false`，公钥是唯一入口。代价是一块
失去全部链路的板子只能重烧而不能在键盘前救回来，`bring-up` 的报告会明说这一点，而不是留给
一块登不进去的板子来告知。

## 谁可以操作这台 Host

`host.identity_file` 曾经兼着两件事：**这台机器用哪把私钥连**，和**板子该信谁**。
只要它们是同一个字段，板子就只可能信一把 key——于是第二个操作者必须被交付某个人的
私钥。那是最不该拷的东西，而且它把审计一起带走了：板子只看得见一个身份，撤销一个人
等于换掉那把 key 再同步每台机器。

拆开之后，`identity_file` 仍然只是这台机器自己的私钥，而这台 Host 接受哪些操作者
是一份**入库的声明**，authorized_keys 格式、一人一行：

```toml
identity_file = "~/.ssh/id_ed25519_eidolon_pi"     # 这台机器的，本机的
operator_keys_file = "eidolon-pi.operators"        # 板子接受谁，入库的
```

入库是刻意的：公钥是公开的，而"谁能操作这块板子"正该是 Git 里看得见、评审得到的事
实，而不是某台笔记本 `~/.ssh` 里恰好有什么。加人是加一行，撤人是删一行，**谁都不碰
任何私钥**。

`bring-up` 从此只读这份声明，完全不读 `identity_file`——所以一个新的操作者可以在没有
任何人私钥的情况下给板子做 bring-up。它也不为缺失的声明回落到 `identity_file` 的公钥
一半：那会把刚拆开的两件事重新粘上，所以没有声明就明确拒绝。

板子上那个 `authorized_keys` 被**对账成等于声明**，不是往里追加。只追加在只有一把 key
时是安全的，但它让撤人变得不可能——声明里删掉的一行永远到不了板子，那声明就不是决定
谁能登录的东西了。所以那个文件归 ops：它等于声明，而脚本会打印它删掉了哪一行。有人
往 ops 账号的 `authorized_keys` 里留的私人 key 从来没有被声明批准过，失去它正是有声明
的意义。

实测过（同一块板子）：把第二个操作者的公钥加进声明、跑一次 `bring-up --via shell`，
他用**自己的**私钥连上并且免密 sudo 可用；从声明里删掉那一行再跑一次，脚本报
`removing key not in the declaration`，他随即被 `Permission denied (publickey)` 拒绝，
而我自己不受影响。

## 换板子换的是信任

host key 是按 Host 的**名字**信任的（`HostKeyAlias`），这正是“换链路不是信任决定”的由来。它同
时意味着两块发布同一个名字的板子无法都被信任：换掉 `eidolon-pi5.local` 背后的板子，就是在一
个已有 key 的名字下出示另一把，而在那之前每个操作都正确地拒绝。

那个文件原来是操作者自己的 `~/.ssh/known_hosts`，改动不入库、和另外一百台主机混在一起，而且
容易朝最糟的方向错：删错一行会当场报错，粘一把没核对过的 key 则永远静默。所以 profile 持有
自己的文件，和它已有的私密输入放在一起：

```bash
./eidolon pi5 trust-host-key                                   # 打印指纹和当前信任的是什么
./eidolon pi5 trust-host-key --apply                           # 此前没信任过任何 key
./eidolon pi5 trust-host-key --apply --replace SHA256:...       # 已信任另一把时
```

它绕过严格 transport 直接 `ssh-keyscan`，因为这个操作存在的场景正是“transport 正确地拒绝了这
台 Host”。只记 ed25519 一种：首次连接会存下三把，但那样操作者要核对的“那个指纹”就是三个，他
比对的是随手读到的哪个。

已信任另一把时，报告直接给出在板子本机读指纹的命令，并要求把那个指纹作为 `--replace` 的值指
名出来。指名一把**不是**正在被出示的 key 会被拒绝——确认必须是关于这台 Host 的，否则就什么都
没确认。换板子和机器在中间从这里看是同一幅画，而工作站上没有任何东西能分辨它们。

## 基础环境 profile

### 部署链路

Pi5 默认允许通过普通局域网部署（`require_wired_release_upload = false`），有可达的有线端点时仍优先
使用它。USB 直连是可选加速/起机通道，不是发布前提；需要强制有线的 Host 可以显式将该字段设为 true。
OPi 的既有显式策略不随 Pi5 一起修改。发布开始时报告选中端点，不能将历史带宽当成当前速度。
若 hostname 显式填写 IP，则固定使用该端点，不再探测并替换成另一条接口；填主机名时保留有线优先发现。

日常使用可先选择已有局域网：Wi-Fi 最省接线；Pi 接路由器或交换机的网口可获得常规 DHCP 地址，避免
USB 点对点连接的 link-local 地址和接口绑定。Mac 仍走 Wi-Fi 时，整条链路不会因为 Pi 插了网线就变成
全程千兆。首次大制品传输和重复更新分别测量；缓存命中后还需关注 prepare 和 activate 的耗时。

无默认路由的隔离 LAN 仍可使用；存在多个候选地址时需用 profile 的 `app.lan_ipv4` 明确设备接入地址，
不能通过 IP 数字排序猜测手机所在网络。这个字段与 SSH 部署端点分开。

### 安装要求

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
```

Host profile 和 operations config 都在仓库里（`config/hosts/*.toml`、
`config/eidolon-*.toml`）——一块板子是什么、能做什么、跑哪些 unit，都是评审过的
产品决定，不该只存在一台机器上。里面唯一与本机有关的值是 SSH 私钥，写成
`~/.ssh/...`，各自的机器自己解析——**而"这台 Host 接受哪些操作者"是另一件事**，
见[谁可以操作这台 Host](#谁可以操作这台-host)。

`known_hosts` 曾经也在那一行里，现在指向 `.eidolon-ops/<host>/known_hosts`——profile
自己的，不是操作者的。理由见[换板子换的是信任](#换板子换的是信任)：文件仍然只在本机
（那个目录一直是 gitignore 的），变的只是它的位置成了评审过的决定。

要接一块新板子，从对应的 `*.example.toml` 复制一份改名，再把它加进仓库。

**secret 不在这些文件里。** 它们只写相对路径指向 `.eidolon-ops/<host>/inputs/`，
那个目录一直是 gitignore 的。出厂配对码也在那里（`factory_setup_code`），profile
里只写 `setup_code_file` 指向它：入库文件里的码就是所有人都有的码。

```bash
chmod 600 config/eidolon-*.toml
```

Mac 还必须安装 `git-lfs`；bundle 只从 exact commit pointer 导出 Channel 模型并验证 LFS object digest，
不会读取 Channel working tree 中的 hydrated 文件。模型与 Linux/aarch64 依赖缓存进入 Mac/Pi 两端的
SHA-256 内容寻址存储；新 release 先查询 Pi，只上传缺失对象。

配置显式固定 foundation profile、目标/SSH、8 个 repo/commit、15 个 release unit、authority 数据路径、
14 个私密基础输入文件、统一 `[app]` LAN 契约、`workspace.release_cli` 与 `workspace.uv` 两个工作站工具，
以及 Python 索引/超时/重试/并发策略。Host-bound Hub
配置、证书、ingress 与 systemd overlay 由 Ops 从 Host identity 原子生成，不进入 Git 或 release bundle。
Python 依赖仍由各仓库的 frozen `uv.lock` 精确约束，但 Linux/aarch64 artifact 在 Mac 上预取、确定性
压缩并哈希；只有 Pi CAS 缺失的 digest 才通过 USB Ethernet 传输。Pi prepare 强制 offline，并为每个
release 新建独立 venv，不复用旧 release 环境。HTTPS index URL 与 uv/build-tool 版本进入 bundle 门禁，
不能在目标端漂移。`host.require_wired_release_upload = true` 会在当前 endpoint 不是有线时于 seal/upload
之前失败，避免悄悄回退到 Wi-Fi/VPN。SSH
强制 BatchMode、独立 key、`StrictHostKeyChecking=yes` 和显式 known_hosts。

工作站 bundle 只服务于封装、断点续传和目标端 prepare：dry-run、失败和未完成事务会保留本地输出供
`--resume` 使用；成功安装或激活在 Host 健康门禁和 release reclaim 提交后删除本次输出，并顺带清理
超过 48 小时的旧输出。清理失败作为操作证据报告，但不会把已经成功的部署改判为失败。可复用的 uv
cache 与 release artifact CAS 位于持久 toolchain root，不参与 bundle 清理。

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

日常操作不再手写 `uv run eidolon-ops --config ...`。入口脚本自动发现 `config/hosts/*.toml`（忽略
`*.example.toml`），以配置文件名作为短主机名，并在执行前拒绝该 Host 不支持的命令：

```text
./eidolon hosts                         # 所有已配置 Host
./eidolon commands mac                  # Mac 支持的命令
./eidolon help pi5 install              # 指定 Host、指定命令的完整参数帮助
./eidolon mac start|stop|restart|status
./eidolon pi5 start|stop|restart|status
```

`status` 默认展示 Host LAN IP、全部非 loopback IPv4、每个服务的监听地址/端口、服务状态和异常建议；自动化需要完整 Evidence 时使用
`./eidolon HOST status --json`。

`deploy/dev/run_all.sh` 仍是 Mac supervisord 的内部生命周期适配器，Host profile 会调用它；它不是操作员
入口，直接删除会破坏 Mac 生命周期。所有人工操作都从 `./eidolon` 进入。

每个操作先产出一份 `Plan`（`operation`/`steps`/`destructive`/`requires_flags`/`touches`），再返回
`Evidence`：Host 报告原样保留在顶层，旁边多出 `plan`、`outcome` 与 `steps`。退出码取自 `Outcome` 枚举，
不再取自 CLI 里的一张“成功词”白名单——那张表让 `commissioning-code` 与 `backup` 在 Host 上成功、在这里
退出非零。`doctor` 会列出该 Host 的 capability 集合（由 platform + transport/supervisor/packages 三个
port 的组合推导），所以“这台 Host 能做什么”是问出来的，不是从源码里读出来的。

`commissioning-code` 可以**指名**要签的码，而不是让 Host 自己抽：命令行 `--code`，或者在 profile 里
写 `app.setup_code`（`pi5.toml` 现在钉的是 `99999990`）。钉住的只有取值——Host 照样开一个普通
session：只能用一次，并把之前的窗口作废。它**不会自己过期**：认领窗口在 ADR-0007 之后没有时钟，
关掉它的只有“被消费”和“被下一次签发顶掉”。所以这条命令不收时限——`--ttl-seconds` 已经删掉了，
因为 **ops 不提供 Host 不会执行的输入**，这是“ops 不编造 Host 没报的事实”的另一半。Evidence 里的
`expires_at` 恒为 `null`，那就是“这个窗口没有时钟”本身，不是取不到值。错码也不再吊销窗口（对一个
无期限的行，那等于开箱即砖），只把 `failed_attempts` 加一留作证据。它买到的是
**不用再查码**：命令变成一条不必读输出的命令，手机上敲的永远是同一串数字。Host 仍然是权威，会拒绝
一个它自己不会抽出来的码（八位数字、不能全同、不能是顺子或倒顺子），profile 解析时也先按同一条规则
挡一遍，好让错误出现在写下这个值的地方而不是三跳之外。

这条路径上**没有任何 mode 判断**，这是有意的：出厂 Host 将来要把印在盒子上的码写进自己，那正是同一个
动作。参见 `eidolon_admin` 的 ADR-0006，以及收窄它的 ADR-0007。

```text
./eidolon HOST status|doctor
./eidolon HOST commissioning-code [--code DIGITS]
./eidolon HOST start|stop|restart [--dry-run]
./eidolon HOST logs [--service SERVICE] [--lines N] [--since TEXT]

# Mac implementation diagnostics (normal lifecycle uses top-level status/start/stop/restart)
./eidolon mac debug prepare|validate|status|web-start|web-stop|web-restart|web-status

# Pi release/install capabilities
./eidolon pi5 bring-up --via boot-medium|shell --output DIR [--apply]
./eidolon pi5 trust-host-key [--apply] [--replace SHA256:...]
./eidolon pi5 provision [--apply]
./eidolon pi5 init-inputs
./eidolon pi5 install --release-id ID [--resume] [--apply]
./eidolon pi5 reset [--wipe-authority-data] [--apply]
./eidolon pi5 controller-reset [--apply]  # lost every managing phone
./eidolon pi5 install --release-id ID \
  --reset-existing --wipe-authority-data [--apply]
./eidolon pi5 deploy|update --release-id ID [--resume] [--activate]
./eidolon pi5 app-ready
./eidolon pi5 rollback --release-id ID --snapshot /var/lib/eidolon/deployments/... [--apply]
./eidolon pi5 diagnose --output /absolute/path/to/report.tar.gz
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

## 同一份契约的第二个前端

`eidolon-ops-console` 在工作站上开一个只监听 `127.0.0.1` 的控制台，管同一批 `config/hosts/*.toml`。它
构造同一个 `HostController`、声明同一份 `Plan`、渲染同一份 `Evidence`，不是第二个 Ops：

```bash
uv sync --all-extras
cd web && npm install && npm run build   # 界面是构建产物，不进 Git
uv run eidolon-ops-console               # http://127.0.0.1:9010
```

它只补终端给不了的两件事。一是**进度**：长操作本就维护着一份 phase 列表（`steps_from_phases` 用来和
计划配对的那一份），`progress.py` 把它换成一个会播报自己条目的 `Journal`——没有 sink 时它就是原来那个
list，有 sink 时同一批条目在发生的当下到达，并且 phase 会在开始时也说一声。计划的 step id 就是 phase
名，所以界面在动工前就画得出骨架。二是**确认梯度**：`plans.py` 从第一天就写着它是给 console 用的。
`status` 什么都不问；`deploy --activate` 要一次显式勾选；`reset --apply`、`controller-reset`、
`install --wipe-authority-data`、`init-inputs --new-identity` 要手输这台 Host 的 id。强制在服务端，
浏览器只负责展示这个要求。

操作面板提供什么由 Host 自己回答——`doctor` 早就发布 `adapter.capabilities`，所以 Mac 上没有 `install`，
Pi 上没有 `debug`。没有绑定地址开关、没有用户体系：这个进程能永久删除权威数据，这套按钮属于操作者自己
的机器。run 只活在进程内存里，Host 的收据才是权威。细节见 [`docs/console.md`](docs/console.md)。

## License

Copyright © 2026 Li Jinsong.

本项目允许依据 [PolyForm Noncommercial License 1.0.0](LICENSE) 进行许可范围内的
非商业使用。商业使用需要另行取得书面授权，请联系
[lijinsong@aimanthor.com](mailto:lijinsong@aimanthor.com)。

许可范围、第三方例外和必要声明见 [LICENSING.md](LICENSING.md) 与 [NOTICE](NOTICE)。
