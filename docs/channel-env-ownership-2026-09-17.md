# channel.env 的归属模型：一个种子、五处声明、一个从不被使用的字面值

> 复核日期：2026-09-17
> 触发：`converge-inputs` 报告里的 `not_repairable: ["channel.env"]`，以及
> `EIDOLON_LIVEKIT_CLIENT_URL` 疑似"两个作者、两个值"。
> 范围：第一至六部分写于纯分析阶段（未改代码），行号是**改动前**的状态；
> 第七部分是机主 review 后按结论执行的落地记录。行号是本次复核时的状态
> （ops `3690895`）。实机取证在 opi5max（`10.42.0.2`），只读，未在板上改动任何文件。
> 报告只写键名，不写任何 secret 的值。

---

## 摘要

| 起点假设 | 复核结论 |
|---|---|
| `channel.env` 的每个键都不属于它自己 | **部分推翻。** `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` 是 ops 铸出来、以 `channel.env` 为权威侧、再拷给 `livekit.env` 的。这个文件既是拷贝的接收方，也是两个密钥的产地 |
| 一共五对共享密钥 | **更正为六对。** 漏了 `LIVEKIT_API_SECRET`（`install_inputs.py:223`） |
| `EIDOLON_LIVEKIT_CLIENT_URL` 是"两个写入路径，先后顺序决定谁赢" | **推翻。** 不是竞态，是管道：seed → render → 上传，render 无条件覆盖。**但**它不是被构造保护的，是被一个 `if self.app` 条件保护的，而那个条件有一条被板端明确接受的 false 分支 |
| `channel.env` 每次部署都会被重写 | **推翻。** `deploy` 根本不携带 `channel.env`。板上写它的只有 `install` 和 `authority-restore` 两条路，没有第三条 |
| refresh 是"从对面那一半重新推导"还是"原样拷贝" | **原样拷贝。** 除了两个 Host 绑定键被重新 render，其余每个值都是工作站那份文件的字节 |
| `not_repairable` 是硬编码、将来会忘记改 | **属实，而且它说的话是反的**：`channel.env` 恰恰是十个 env 文件里唯一整文件可替换的那个 |
| `channel.env` 退出账目的理由是"key set 开放，missing 不可判定" | **理由不成立。** 同一个文件的必需键集在 `install_inputs.py:502` 被完整声明了，只是藏在一个函数局部变量里 |

**按严重度排序的真实问题：**

1. **seed 里那个 loopback 字面值是一颗哑弹，而且没有任何一层会拒绝它**（§2）。
   它今天打不响，靠的是一个条件而不是构造；一旦逃逸，channel provider 会干净启动、
   把 `ws://127.0.0.1:7880` 发给每台设备——`docs/跨系统/Host地址事实的收敛.md` §6.2
   记的"比枚举更糟，它指向设备自己"。
2. **六对共享密钥里有四对漂移后完全静默**（§4），其中 `PAIRING_JWT_SECRET` 的第一个
   症状发生在"有人对着设备说话"的那一刻。
3. **工作站 → 板子，`channel.env` 只有两条通路，且都不是日常操作**（§3）。
   操作者在 `eidolon_channel/config/.env` 里轮换供应商密钥，工作站会跟上，板子永远拿不到；
   而下一次 `install --apply` 会因为"文件不一致"直接拒绝。这是一条死路。
4. **同一个事实被写了四遍**（§5.4），其中两处是逐字重述——正是这个文件的注释反复说
   不可以有的东西。

---

# 第一部分：先说结构，后面所有结论都从这里出来

## 1.1 同名的是两个文档，不是一个

```
工作站  eidolon_ops/.eidolon-ops/opi5max/inputs/channel.env    9 键   Sep  6 14:08
板子    /etc/eidolon/channel.env                              10 键   Sep 14 22:59
```

中间隔着一步 render（`host_application.py:159`）。两边**键集不同、值不同**，而且这是设计，
不是漂移：

```
# 只有板上有的键（render 现造，工作站那份从来没有过）
EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL=1

# 同一个键，两个值
工作站  EIDOLON_LIVEKIT_CLIENT_URL=ws://127.0.0.1:7880
板子    EIDOLON_LIVEKIT_CLIENT_URL=ws://:7880
```

**工作站那份是种子，不是部署件。** 这件事代码里没有一处明说，而它是理解其余一切的前提：
`install_inputs.py` 生成的 `channel.env` 是一个**永远不会被任何进程直接读的模板**。
Mac 走 `local_product.py:451`，板子走 `host_application.py:159`，两条路都在把它 render 成
另一个文件之后才交给组件。

## 1.2 每个键的真实归属（查证结果）

工作站种子，9 个必需键 + 2 个可选键：

| 键 | 谁是作者 | 值从哪来 | 同步机制 | 谁证明它一致 |
|---|---|---|---|---|
| `OPENAI_LLM_API_KEY` | 操作者 | `eidolon_channel/config/.env` | `refresh_provider_credentials`（拉取覆盖） | `validate_install_input_contract` |
| `BAILIAN_STT_API_KEY` | 操作者 | 同上 | 同上 | 同上 |
| `BAILIAN_TTS_API_KEY` | 操作者 | 同上 | 同上 | 同上 |
| `SENSETIME_STT_API_KEY`（可选） | 操作者 | 同上 | 同上，**存在性也要一致** | 同上 |
| `SENSETIME_TTS_API_KEY`（可选） | 操作者 | 同上 | 同上 | 同上 |
| `LIVEKIT_API_KEY` | **ops 铸**（`install_inputs.py:85`） | **本文件即权威侧** | `SHARED_CREDENTIALS` → `livekit.env` | 同上 |
| `LIVEKIT_API_SECRET` | **ops 铸**（`:86`） | **本文件即权威侧** | 同上 | 同上 |
| `EIDOLON_CHANNEL_PROVIDER_TOKEN` | ops 铸（`hub_provider_token`，`:77`） | 权威侧是 `hub.env` | ↔ `hub.env` 且 ↔ `admin.env` | 同上 |
| `PAIRING_JWT_SECRET` | ops 铸（`pairing_token`，`:78`） | 权威侧是 `agent.env` | ↔ `agent.env` | 同上 |
| `EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN` | ops 铸（`data_token`，`:71`） | 权威侧是 `data.env` | ↔ `data.env` | 同上 |
| `EIDOLON_LIVEKIT_CLIENT_URL` | **两处都写**（见 §2） | seed 写死 loopback | 无（render 覆盖） | `:571` 断言 seed 必须是 loopback |

> 最后一列有一个共同的限定，重要到必须写在这里：`validate_install_input_contract`
> **只在 `install --apply` 时跑**（`release_preflight.py:156` → `require_install_files=True`）。
> `deploy` 走的是 `refresh_product_settings()`，只碰三个 yaml；`doctor` 有意绕开它。
> 也就是说，表里这一整列在首次安装之后基本不再发生。见 §3.3、§4.3。

板上部署件额外多一个 `EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL`，
由 `host_application.py:162` 从 profile 的 `app.allow_insecure_livekit` 现造，
**任何一处 ops 声明表里都没有它**。

所以"每个键都不属于它自己"这句话要改：`channel.env` 里有 **2 个键是它自己生的**
（LiveKit 那一对），另外 4 个是拷贝，5 个是外部凭证的本地副本，1 个是占位。

## 1.3 实机核验：六对今天全都对得上

在 opi5max 上逐对比较（只输出布尔，不输出值）：

```
data.env:EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN  <-> channel.env:同名                      MATCH
hub.env:EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN       <-> channel.env:EIDOLON_CHANNEL_PROVIDER_TOKEN  MATCH
agent.env:PAIRING_JWT_SECRET                     <-> channel.env:同名                      MATCH
admin.env:EIDOLON_CHANNEL_PROVIDER_TOKEN         <-> channel.env:同名                      MATCH
channel.env:LIVEKIT_API_KEY                      <-> livekit.env:同名                      MATCH
channel.env:LIVEKIT_API_SECRET                   <-> livekit.env:同名                      MATCH
```

这块板子现在是健康的。下面讨论的都是"当它不健康时会怎样"，不是"它现在坏了"。

---

# 第二部分：`EIDOLON_LIVEKIT_CLIENT_URL` 的定性

## 2.1 不是竞态，是管道

```
install_inputs.py:161   seed 写入      "ws://127.0.0.1:7880"
        │
        ├── 板子：host_layer.py:331 → host_application.py:159 → environment.merge(...)
        └── Mac ：local_product.py:451             → environment.merge(...)
                                                          │
                                                  _livekit_client_url() → "ws://:7880"
```

`environment.merge` 的语义是"设置值，seed 没有的名字就加上"（`environment.py:56`），
**无条件**。两条 render 路径都在无条件设置这个键。没有先后，没有谁赢——seed 的值
在离开工作站的那一刻就被替换掉了。

板上实测 `ws://:7880`，与 `livekit_client_url()` 在无 `lan_ipv4` 声明时的产出一致
（`host_identity.py:116`）。管道是通的。

## 2.2 但保护它的是一个条件，不是构造

`host_layer.py:285`：

```python
application = self.prepare() if self.app else None
...
if application is not None and name in HOST_BOUND_INPUTS:
    rendered = self.materializer().render_environment(...)
```

**profile 没有 `[app]` 表时，`channel.env` 原样上传。** 而这不是一条死路：

- `paths.py:493` 明确允许 `app` 缺省（`if value is None: return None`）；
- 板端 `install.py:189` 接受的暂存文件集有三种，其中一种就是
  `frozenset(contract.SECRET_INPUTS)`——**没有 Host application 资产的那一种**。
  也就是说"不带 `[app]` 的安装"是板子明确支持的形状，不是没人走过的分支。

今天仓里六个 profile 全都声明了 `[app]`（`config/hosts/*.toml`），所以哑弹没响。
但"没响"的理由是配置，不是代码。

## 2.3 逃逸之后，没有任何一层会拒绝

这是最值得记下来的一条。channel provider 的 URL 校验
（`eidolon_channel/eidolon/channel_provider/config.py:155`）：

```python
if (parsed.scheme in {"http", "ws"}
        and parsed.hostname not in _LOOPBACK_HOSTS
        and not (client and allow_insecure_lan)):
    raise ValueError(f"insecure {name} is allowed only on loopback")
```

`127.0.0.1` **就在** `_LOOPBACK_HOSTS` 里。那条"明文只准 loopback"的规则，
对这个值而言不是关卡，是通行证。provider 会干净启动。

然后 `adapter.py:274`：

```python
parsed = urlparse(self._config.client_url)
if parsed.hostname is not None:
    return [self._config.client_url]      # 原样发给设备
```

有主机名就原样下发。于是每台设备拿到 `ws://127.0.0.1:7880`——指向它自己。
这正是 `docs/跨系统/Host地址事实的收敛.md` §6.2 的原话：「比枚举更糟，它指向设备自己」。

同时，能发现这件事的那条 readiness 事实 `LIVEKIT_CLIENT_ORIGIN`
（`probe.py:511`）读的是 `app["livekit_client_url"]`，而 `app` 这一段只在
`self.app is not None` 时才进 payload（`host_layer.py:200`）。
**能出事的那种配置，恰好也是检查跑不起来的那种配置。**

## 2.4 还有两件事让这个键更难读

- `FIXED_ENV_VALUES["EIDOLON_LIVEKIT_CLIENT_URL"]`（`:309`）是**死条目**。
  唯一读 `FIXED_ENV_VALUES` 的修复循环（`:412`）只遍历 `DECLARED_ENV_KEYS`，
  而 `channel.env` 不在里面。已用脚本核实：这个键不属于任何一个已声明的键集。
- `:571` 的断言 `channel["EIDOLON_LIVEKIT_CLIENT_URL"] != "ws://127.0.0.1:7880"` → 报错
  "LiveKit client origin drifted from the backend-only contract"。
  它**要求** seed 里必须是那个哑弹值。一个读到这一行的人会合理地认为
  "Host 上跑的就是 loopback"——而这是错的。

## 2.5 定性

**不是现存 bug。是一个被条件保护的脆弱设计，而且脆弱的方向正好指向已知的最坏形态。**

具体地说，这个设计有三个可以独立修好的性质，目前一个都不满足：

1. seed 的值如果逃逸，应当是无害的或响亮的 —— 现在是**静默且灾难性**的；
2. 一个键应当只有一个作者 —— 现在有两个，并且**契约检查站在错的那一边**；
3. 能检测它的那条 readiness 不应当和"能出事的配置"互斥 —— 现在恰好互斥。

---

# 第三部分：六个拷贝靠什么保持同步

## 3.1 refresh 是原样拷贝

`host_layer.py:358 refresh()` 上传 `host_identity` + `HOST_BOUND_INPUTS` + 三个 settings，
每个 Host 绑定文件过一遍 `render_environment`。render 只动两个键
（`EIDOLON_LIVEKIT_CLIENT_URL`、`..._ALLOW_INSECURE_...`），**其余每个字节都是工作站那份**。
板端 `host_application.py:170` 整文件原子替换。

所以：**不是重新推导，是原样拷贝。** 工作站的 `channel.env` 是板子上那份的唯一上游。

## 3.2 但 deploy 根本不带它——这是最容易被误读的一点

`REFRESHABLE_HOST_BOUND_INPUTS`（`contract.py:337`）确实包含 `channel.env`，但它只进了
`REFRESHABLE_HOST_LAYER_INPUTS`（`:343`），而板端选择哪一套是这样分的
（`hostagent/host_application.py:169`）：

```python
selected_inputs = (
    contract.RELEASE_CONFIGURATION_INPUTS if preserved_context is not None
    else contract.REFRESHABLE_HOST_LAYER_INPUTS
)
```

- `preserved_context is not None` ← `refresh-release-configuration`，**deploy 走这条**。
  `RELEASE_CONFIGURATION_INPUTS`（`:339`）= hub 设置 + ingress 三件 + 三个 yaml。
  **没有 `channel.env`。** `refresh_release()`（`host_layer.py:231`）连暂存都不暂存它。
- `preserved_context is None` ← `refresh-host-application`，**全仓只有一个调用者**：
  `controller.py:1609`，在 `authority_restore()` 里面。CLI 里没有对应的动词
  （`host_cli.py:50` 的 `OPERATIONS` 表可查）。

板上的时间戳与之完全吻合：

```
Sep  8 14:55   data/hub/kernel/admin/agent/memory/livekit/bootstrap.env   ← install
Sep 14 22:59   channel.env, local-api.env, tls/, owner-domain/,
               generated/hub.yaml, eidolon-hub-ingress.service, agent.yaml ← 一次完整 refresh
Sep 15 23:32   channel.yaml                                              ← deploy
Sep 17 00:02   generated/ports.yaml, host.env, eidolond.yaml, ...        ← deploy
```

两次 deploy 之后 `channel.env` 纹丝不动。（代码是结论，时间戳只是旁证——
`host_application.py:191` 在字节相同时会跳过写入，所以 mtime 不动不能单独证明什么。）

**结论：板上的 `channel.env` 只有两个写入者——`install`，和 `authority-restore`。**

## 3.3 "对面轮换了而本地没同步会怎样"

分两种，答案不一样。

**(a) 操作者在 `eidolon_channel/config/.env` 里换了 BAILIAN/OPENAI 密钥。**

- 工作站：`refresh_provider_credentials`（`private_inputs.py:133`）会覆盖 seed。
  但它只在 `validate_install_input_contract(refresh_derived=True, verify_provider_sources=True)`
  里被调用，而这条路只有 `preflight.run(require_install_files=True)` 会走——
  也就是**只有 `install --apply`**。`deploy` 走的是 `refresh_product_settings()`
  （`release_preflight.py:400`），它只碰三个 yaml。
- 板子：**拿不到。** deploy 不带 `channel.env`；`converge-inputs` 明确跳过它；
  而 `install --apply` 会在板端撞上 `install.py:445`：
  `existing prerequisite differs during resume: /etc/eidolon/channel.env`，直接失败。

这是一条**死路**：唯一能更新板上 `channel.env` 的日常动词不存在，而能带它的动词
会因为它变了而拒绝。`release_preflight.py:402` 的注释说
"the installed Host already owns and separately proves those credentials"——
**这句话不成立**，见 §4.3。

**(b) ops 自己铸的那六个拷贝之一被改了。**

`install` 时两侧由同一个变量写出，天然相等。之后：

- 工作站侧：`validate_install_input_contract`（`:562`）逐对比较，不等即拒。
  但这也只在 `install --apply` 时跑。
- 板子侧：**没有任何东西比较过它们。** `doctor-host`（`lifecycle.py:169`）查的是
  系统、systemctl、`host.env`、端口表，不查凭证关系。
  `converge-secret-inputs` 里有一个轮换检测（`secret_inputs.py:120`），但它
  ①只对 `declared` 里的文件生效，而 `channel.env` 不在里面；
  ②只在该文件**同时还缺键**时才会被触发。

---

# 第四部分：漂移的失败形态，以及它是否可见

## 4.1 缺失和不一致是两回事

channel provider 启动时会 `_required_secret("EIDOLON_CHANNEL_PROVIDER_TOKEN", minimum=32)`
（`config.py:260`）——**缺了会响亮地拒绝启动**。
但一个"长度合格、只是不对"的值会让进程干净启动。这个仓里反复出现的那一族问题
（`docs/跨系统/Audit链路部署归属与静默失效.md`：「故障存在，但只存在于没人读的地方」）
正是后者。

## 4.2 六对逐一

| 键对 | 谁在用 | 不一致时的表现 | 什么时候第一次显形 | readiness 会红吗 |
|---|---|---|---|---|
| `LIVEKIT_API_KEY` ↔ `livekit.env` | worker 向 LiveKit 注册、签房间令牌 | 注册失败 / 令牌被拒 | 服务起来几秒内 | **会**：`CHANNEL_WORKER_LIVEKIT_LINK`、`CHANNEL_WORKER_HEALTHY` |
| `LIVEKIT_API_SECRET` ↔ `livekit.env` | 同上 | 同上 | 同上 | **会**，同上 |
| `EIDOLON_CHANNEL_PROVIDER_TOKEN` ↔ `hub.env` | Hub → provider 的 channel 重对账 | provider 401 | 下一次设备拉配置或输出策略变更时 | **不会** |
| `EIDOLON_CHANNEL_PROVIDER_TOKEN` ↔ `admin.env` | Admin 读"谁在这条 channel 上" | 401，在场页空 | 有人打开那个页面时 | **不会** |
| `EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN` ↔ `data.env` | channel 运行时读 System Data | 401 | 一次会话解析身份时 | **不会** |
| `PAIRING_JWT_SECRET` ↔ `agent.env` | channel 签运行时令牌，Agent 验签 | 验签失败 | **有人对着设备说话的那一刻**（`resolver.py:325`，在 `chat()` 里惰性求值） | **不会** |

## 4.3 结论

六对里**只有两对**（LiveKit 那一对密钥）会让 readiness 变红，而且是因为 worker
连不上 LiveKit 这个副作用，不是因为有谁比较过这两个文件。

另外四对**全部静默**。其中 `PAIRING_JWT_SECRET` 最差：它的失败点在最深处，
第一个受害者是一个正在跟设备说话的人，而 deploy、`doctor`、`app-ready` 三道关全绿。

`release_preflight.py:402` 那句 "the installed Host already owns and separately proves
those credentials"，字面上指的应该是 `_require_declared_credentials`
（`release_transaction.py:80`）这道闸。但那道闸问的是 **declared 键在不在**，
不问值对不对，而且 `declared` 里**根本没有 `channel.env`**。
这句注释应当被改掉或收窄——它现在给读者的信心是没有依据的。

---

# 第五部分：会让后人费解的地方

## 5.1 `not_repairable: ["channel.env"]` 说的话是反的

`install_inputs.py:437` 的注释很好：
「Named rather than silently skipped: a reader should not have to work out why
one file is not in the accounting.」但这个字段传达的信息是错的。

`channel.env` 是十个 env 文件里**唯一**存在整文件替换通路的那个
（`REFRESHABLE_HOST_BOUND_INPUTS`）。它不需要"增量修复"，因为它可以被整体重发。
其余九个才是只能靠 `converge` 加键的。所以准确的说法不是"这个文件修不了"，而是
**"这个文件不走增量修复，它走整文件刷新"**。

而且这个字段本身漏了一半账：`bootstrap.env` 也不在 `declared` 里
（`declared_secret_env_keys()` 的 `if keys` 过滤掉了空声明），
但 `not_repairable` 只点了 `channel.env` 一个名。承诺"不让读者自己去推"的那个字段，
自己漏了一项。

## 5.2 `declared_secret_env_keys()` 里有一个死条件

```python
return {
    name: sorted(keys)
    for name, keys in sorted(DECLARED_ENV_KEYS.items())
    if keys and name != "channel.env"      # ← 后半句永远为真
}
```

已用脚本核实：`"channel.env" in DECLARED_ENV_KEYS` → `False`。
`DECLARED_ENV_KEYS` 只有九个条目，`channel.env` 从来不在其中。

这行防御的后果不是多余，是**误导**：读到它的人会认为 `channel.env` 在
`DECLARED_ENV_KEYS` 里、只是在这里被过滤掉了。测试
（`test_credential_convergence.py:72`）也按同样的理解写着
`assert "channel.env" not in declared`，而它断言的是一个恒真命题。

## 5.3 排除 `channel.env` 的理由本身不成立

代码里三处说的是同一句话：「its key set is deliberately open, so "missing" is not a
decidable question there」（`:337`、`:373`、`:382`）。

但同一个文件的必需键集在 `:502` 被**完整地、精确地**声明了：

```python
channel_required = {
    "OPENAI_LLM_API_KEY", "BAILIAN_STT_API_KEY", "BAILIAN_TTS_API_KEY",
    "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "EIDOLON_CHANNEL_PROVIDER_TOKEN",
    "EIDOLON_LIVEKIT_CLIENT_URL", "PAIRING_JWT_SECRET",
    "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN",
}
```

九个键，一个不少，紧接着就是一条严格的上下界检查：
`channel_required <= keys <= channel_required | OPTIONAL_CHANNEL_KEYS`。

所以"missing 不可判定"是假的。可判定得很——**开放的只是可选那条尾巴**。
真正缺的不是判定能力，是 `DECLARED_ENV_KEYS` 的表达能力：它每个文件只能说一个集合，
说不出"必需 + 可选"。于是 `channel.env` 被整个踢出账目，而它的必需集
被复制到一个函数局部变量里，离主声明 340 行。

## 5.4 同一个事实被写了四遍

供应商凭证"哪个键落在哪个文件"这件事：

| # | 位置 | 形态 |
|---|---|---|
| 1 | `provider_inputs.py:18` `EXTERNAL_KEYS` | source_id → 键 |
| 2 | `private_inputs.py:184` `_PROVIDER_DESTINATIONS` | source_id → 文件名 |
| 3 | `install_inputs.py:320` `PROVIDER_ENV_KEYS` | (文件名, 键) —— **可由 1×2 推出** |
| 4 | `install_inputs.py:575` 内联元组 | **与 3 逐字相同** |
| 5 | `install_inputs.py:523` `provider_destinations` | **与 2 逐字相同** |

已用脚本核实 3 == `[(_PROVIDER_DESTINATIONS[s], k) for s, ks in EXTERNAL_KEYS ...]`，
以及 4 == 3（`ast.literal_eval` 比较，完全相等）。

这个文件自己的注释说得最好（`:205`）：
「Those two used to be the same table written twice — and the second copy is how a new
credential gets validated on a Host that has no way to receive it.」
同一段话适用于它下面 370 行处的第四份拷贝。

## 5.5 板上那个键谁都没声明

`EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL` 存在于每一台板子的
`/etc/eidolon/channel.env`，由 `host_application.py:162` 和 `local_product.py:456`
各写一次（两份相同的三元表达式），**在 ops 的任何一张声明表里都查不到**。
它也是 `channel_required` 上界检查看不见的——因为那个检查跑在工作站的 seed 上，
而 seed 里没有它。

要回答"板上的 `channel.env` 应该有哪些键"，今天要读五个地方：
`install_inputs.py:156`（生成）、`:502`（必需集）、`provider_inputs.py:27`（可选集）、
`host_application.py:159`（render 加的）、`SHARED_CREDENTIALS`（哪些是拷贝）。

---

# 第六部分：建议

按优先级。每条给"改什么 / 为什么 / 会碰到什么"。

## 建议 1（改，最高优先）：把 `EIDOLON_LIVEKIT_CLIENT_URL` 从 seed 里删掉

**改什么**

- 删 `install_inputs.py:161` 的 seed 条目；
- 删 `:309` 的 `FIXED_ENV_VALUES` 条目（已是死条目）；
- 从 `:502` 的 `channel_required` 里去掉它；
- 删 `:571` 那条断言；
- render 侧一个字不用动——`environment.merge` 的文档写的就是
  "Set values, **adding names the seed does not have**"。

**为什么**

这一改同时解决 §2 的三个性质：

1. **一个键一个作者。** render 成为唯一作者，与 `livekit_client_url()` docstring 里
   已经写下的原则一致：「Ops writing an address here would be Ops answering a question
   that is only answerable later」。seed 现在正是在回答那个问题，答错了，靠下游覆盖兜着。
2. **逃逸从静默变响亮。** 没有 `[app]` 的 profile 上传一份不含这个键的 `channel.env`，
   provider 的 `channel-provider.yaml:22` 拿到未展开的 `$EIDOLON_LIVEKIT_CLIENT_URL`，
   `channel_provider/config.py:85` 抛 `config contains an unexpanded environment variable` ——
   **在正确的地方、启动时、拒绝启动**。对比现在：干净启动，把回环地址发给每台设备。
3. **契约检查不再站错边。** `:571` 今天断言的是一个从不被使用的值必须等于最危险的那个值。

**会碰到什么**

- 四个测试固定了那个字面值：`test_host_application.py:101`、`test_local_product.py:93`、
  `test_controller.py:1596` 和 `:2583`。前者作为 render 的输入 seed 出现——正好可以改成
  "seed 里没有这个键"，顺便把 merge 的新增语义测进去。
- `local_product.py:601` / `:666` 和 `probe.py:512` 读的都是 render 之后的文件，不受影响。

## 建议 2（改）：让 `DECLARED_ENV_KEYS` 能说"必需 + 可选"，`channel.env` 就不再是例外

**改什么**

`DECLARED_ENV_KEYS` 的值从 `set[str]` 变成一个两字段的小结构
（`required: frozenset`，`optional: frozenset`），然后：

- `channel.env` 进表：`required` = `:502` 那九个（按建议 1 去掉 URL 后是八个），
  `optional` = `OPTIONAL_CHANNEL_KEYS`；
- `:502` 的 `channel_required` 局部变量删掉，`:516` 的上下界检查改成对着表里的两个字段跑
  ——顺带让**其余九个文件也获得了同样的上下界语义**，今天它们是硬相等；
- `declared_secret_env_keys()` 里的 `name != "channel.env"` 删掉（它本来就是死的），
  改为输出 `required`；
- `not_repairable` 字段**整个删掉**——没有不在账目里的文件了。
  如果机主希望保留"哪些文件不参与增量修复"的可见性，正确的做法是让它从表里推出来
  （`required` 为空的那些），而不是硬编码一个名字。这正是 `:331` 那条注释
  「Derived from DECLARED_ENV_KEYS rather than restated」说的原则。

**为什么**

- 它把 §5.1、§5.2、§5.3 三件事一次性解决，而且解决方式是**让特例消失**，不是给特例写更多注释。
- 加新键时不会漏：`channel.env` 和别的文件走同一张表、同一条路。今天给它加一个键，
  要记得同时改 `:156` 的生成和 `:502` 的局部集合，没有任何测试会因为只改一处而变红。
- 副产品：`channel.env` 从此可以被 `converge-inputs` 覆盖，于是"产品长出一个新的
  channel 侧共享凭证"不再需要 reinstall。

**会碰到什么**（这条必须提前知道，否则会踩）

`secret_inputs.py:120` 的轮换检测遍历的是暂存文件的**全部**键，不只是缺的那些：

```python
rotated = [key for key, value in offered.items()
           if key in present and present[key] != value]
```

一旦 `channel.env` 进了 `declared`，这个比较就会把两个 Host 绑定键也算进去。
正常情况下它们 render 出来相同，没问题；但**操作者在 profile 里新增或修改了
`app.lan_ipv4` 之后**，它们会合法地不同，于是 convergence 会以
"rotate through a reinstall" 拒绝——理由是错的，那不是轮换。

所以这条建议要配一个小改动：轮换检测**只看 `wanted` 之外的密钥类键**，
或者更干净地，让 render 产生的 Host 绑定键从比较里显式豁免（它们已经有自己的通路和
自己的 readiness 事实 `LIVEKIT_CLIENT_ORIGIN`）。

## 建议 3（改，小而纯粹）：删掉三份重述

- `install_inputs.py:575` 的内联元组 → 直接用 `PROVIDER_ENV_KEYS`（已核实逐字相同）；
- `install_inputs.py:523` 的 `provider_destinations` → 从 `private_inputs` 导入
  `_PROVIDER_DESTINATIONS`（改成公开名），或者让 `PROVIDER_ENV_KEYS` 从
  `EXTERNAL_KEYS × _PROVIDER_DESTINATIONS` 推出来，那样两份都没了。

零风险，零语义变化，测试不用动。这条之所以值得单独列，是因为它是**这个文件的注释
自己反复警告的那种东西**，留着会让后来的人怀疑那些注释到底算不算数。

## 建议 4（保持现状，但把理由写进代码）：deploy 不带 `channel.env` 是对的

`deploy` 只送代码和从代码派生的东西；凭证是 Host 拥有的，不该被每次发布重写
——这个分工是对的，不要改。

但它现在**只能靠读三个文件的交集推出来**（`REFRESHABLE_HOST_BOUND_INPUTS` 在
`contract.py:337`，`RELEASE_CONFIGURATION_INPUTS` 在 `:339`，选择发生在
`hostagent/host_application.py:169`）。`REFRESHABLE_HOST_BOUND_INPUTS` 这个名字读起来像
"每次 refresh 都带"，而事实是"只有那个 CLI 里没有动词的 refresh 才带"。

建议在 `contract.py:337` 上方写清楚三件事：谁带它、谁不带、为什么。大意：

> 这两个文件只在完整的 Host 层刷新里重发（`refresh-host-application`），
> 不在发布切换里（`refresh-release-configuration`）。发布换的是代码和从代码派生的设置；
> 凭证归 Host 所有，一次发布重写它们意味着每次升级都在悄悄轮换。
> 今天完整刷新只由 `authority-restore` 触发，所以板上这两个文件的写入者实际只有
> `install` 和 `authority-restore` 两个。

## 建议 5（改）：把 `channel.env` 的漂移变成一个有人问的问题

这是 §4 的正面回应，也是我认为**第二重要**的一条。

今天板上那六对共享凭证在安装之后**再也没有被任何东西比较过**。工作站那边的检查
（`validate_install_input_contract`）只在 `install --apply` 时跑，而 `install` 在文件
已存在且不同时会直接拒绝——也就是说，**唯一会检查的那条路，恰好是走不到的那条路**。

建议给 `doctor-host` 加一条只用摘要的关系检查：板子自己读 `/etc/eidolon/*.env`，
对 `SHARED_CREDENTIALS` 的每一对比较 `sha256`（或仅比较相等性），只回布尔和标签。
不回值，不回摘要本身。

理由：这是这个仓最熟悉的那类修复——把一个真实存在但没人读的故障，接到一个有人读的地方。
`SHARED_CREDENTIALS` 表已经存在且是权威的；缺的只是把它送到板子上跑一遍。
（送表而不是在 agent 里再写一份，与 `declared_secret_env_keys()` 的理由完全一致。）

优先级排在建议 1、2 之后，是因为它需要新代码而不是删代码；但它覆盖的失败面最大——
§4.2 里那四对静默的组合，全部由它变成 `doctor` 的一行红字。

## 建议 6（保持现状，但要有话说）：供应商凭证的轮换死路

§3.3(a) 那条死路是真的，但它**不应该在这次一起修**：修它需要一个新动词
（"把 Host 绑定文件重发到板子"，也就是把今天 `authority-restore` 私有的 `refresh`
提升成一个 CLI 动词），那是一个独立的设计决定，涉及"什么时候允许 ops 覆盖 Host 上的凭证"，
不该塞进一次归属梳理里。

现在该做的只有两件：

1. 把 `release_preflight.py:402` 那句 "the installed Host already owns and separately
   proves those credentials" 改掉。它不成立（§4.3），而它正是会让下一个人不去查的那句话；
2. 在 `refresh_provider_credentials`（`private_inputs.py:133`）的 docstring 里补一句：
   这个函数让**工作站**跟上组件的 `config/.env`，把值送到板子是另一回事，今天只有
   `install`（会因文件已存在而拒绝）和 `authority-restore` 两条路。

---

## 附：本次复核跑过的命令

工作站（只读）：

```bash
python3 -c "…"   # 核实 channel.env 不在 DECLARED_ENV_KEYS、FIXED_ENV_VALUES 死条目、
                 # PROVIDER_ENV_KEYS 可由 EXTERNAL_KEYS×_PROVIDER_DESTINATIONS 推出
python3 -c "…"   # ast.literal_eval 比较 :320 与 :575 两个元组，完全相等
cut -d= -f1 .eidolon-ops/opi5max/inputs/channel.env     # 9 个键名
```

opi5max（只读，未写入任何文件）：

```bash
sudo ls -la /etc/eidolon/                                # 时间戳分层
sudo cut -d= -f1 /etc/eidolon/*.env                      # 各文件键名
sudo grep '^EIDOLON_LIVEKIT_CLIENT_URL=' /etc/eidolon/channel.env    # ws://:7880
sudo python3 -                                           # 六对逐对比较，只输出 MATCH/DIFFER
```


---

# 第七部分：落地记录（2026-09-17）

机主 review 后执行。改动未提交到 main，在 `claude/heuristic-benz-5d89f8` 上。

## 7.1 收敛到一件事：让那张表能说出 channel.env 是什么

动手前重新想了一遍"最优雅"，结论和第六部分的建议顺序不同——**建议 1 和 2 不是两件事，
是同一件事的两半**，而且仓里已经有了正确形状的样板：

`local-api.env` 也是 Host 绑定文件。它的种子里是 3 个凭证键，render 再加 5 个 Owner 域键，
**两组完全不相交**（实测确认）。也就是说这条不变式本来就成立：

```
部署件 = 种子 ∪ render 加的键        ——  没有一个键在两边取不同的值
```

`channel.env` 违反它，且**只违反在一个键上**。所以这不是"删掉一个危险字面值"，
是让它服从仓里已经存在的那条规则。

而它之所以成为例外，根因也只有一个：`DECLARED_ENV_KEYS` 的值是 `set[str]`，**说不出"可选"**。
唯一有可选键的文件因此被整个踢出账目，然后四个消费者各自长出一套"除了 channel.env"的说法，
理由还互相矛盾。**给那张表加上类别，例外就同时消失。**

## 7.2 改了什么

**一处新声明**（`install_inputs.py`），十个文件全部进表：

```python
@dataclass(frozen=True, slots=True)
class EnvFileKeys:
    required: frozenset[str]   # 每台 Host 都得有
    optional: frozenset[str]   # 操作者有就有——set 说不出的就是这个
    rendered: frozenset[str]   # Host 层下发路上写的，种子里不准有
```

`channel.env` 现在是 `required` 8 个、`optional` 2 个（SENSETIME 那一对）、
`rendered` 2 个（`EIDOLON_LIVEKIT_CLIENT_URL` 和
`EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL`）。
`local-api.env` 的 5 个 Owner 域键也第一次被声明出来——它一直是这个形状，
只是从来没写下来过。

**由它推出去的**（全部删掉了各自的手写副本）：

| 原来 | 现在 |
|---|---|
| `declared_secret_env_keys()` 里 `name != "channel.env"`（死条件） | 删；输出 `required`，`channel.env` 在内 |
| `:502` 函数局部的 `channel_required`（第二份声明） | 删；上下界检查对十个文件统一跑 |
| `:571` 断言种子必须等于 `ws://127.0.0.1:7880` | 删 |
| `FIXED_ENV_VALUES["EIDOLON_LIVEKIT_CLIENT_URL"]`（死条目） | 删 |
| `not_repairable: ["channel.env"]` | 删；该文件和别的一样走增量修复 |
| `PROVIDER_ENV_KEYS` 手写 | 由 `EXTERNAL_KEYS × PROVIDER_DESTINATIONS` 推出 |
| `:575` 内联元组（与上者逐字相同） | 删 |
| `:523` `provider_destinations`（与 `private_inputs` 逐字相同） | 删 |

**两处新守卫**：

- `host_rendered_fields(name, values)`：两个 renderer（Pi 的和 Mac 的）都从这里过，
  写的字段必须正好等于表里声明的。这是"加新键不会漏"的落点——
  `EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL` 能在每台 Host 上存在而
  在任何声明里查不到，就是因为没有这一道。
- `withdraw_rendered_fields(target)`：把 render 拥有的字段从种子里撤掉，
  挂在已有的 `refresh_derived` 那一趟上。理由和它旁边那两个"跟随源"的步骤一样：
  一份写于 Ops 还以为自己拥有这个字段的年代的输入集，没有别的办法说明这件事。
  拒绝它只会让人去手改一个 0600 文件。

**一处按原则收窄**（`hostagent/secret_inputs.py`）：轮换检测原本遍历暂存文件的**全部**键，
现在只看 `declared`。Host 绑定字段在 Host 绑定变化时合法地不同，那不是轮换，
拿它去拒绝一次 convergence 是用一个不成立的理由拒绝。这正是第六部分建议 2 点名的那颗雷。

**三处措辞改正**：`release_preflight.py` 那句"Host 自己会分别证明这些凭证"改成了它实际
能主张的范围（按名字问持有，不比较值，关系只在工作站证明）；`contract.py` 的
`REFRESHABLE_HOST_BOUND_INPUTS` 上方写清了谁带谁不带、以及"今天完整 refresh 只由
authority-restore 触发"；`refresh_provider_credentials` 的 docstring 补了轮换到不了板子这件事。

## 7.3 顺带被关上的两个口子

这两个不是单独改的，是结构收敛的副产品：

1. **deploy 的凭证闸门现在覆盖 `channel.env`。** `_require_declared_credentials` 问的是
   `declared_secret_env_keys()`，`channel.env` 进表之后自动被问到。已对真机核验：
   板上 `/etc/eidolon/channel.env` 正好持有那 8 个键，**新闸门不会拦住现有部署**。
2. **哑弹的失败形态从静默变响亮。** 没有 `[app]` 的 profile 现在上传一份不含
   `EIDOLON_LIVEKIT_CLIENT_URL` 的 `channel.env`，channel provider 的
   `config.py:85` 拿到未展开的 `$EIDOLON_LIVEKIT_CLIENT_URL` 直接抛
   `config contains an unexpanded environment variable`——启动时拒绝，而不是干净启动
   然后把回环地址发给每台设备。

## 7.4 明确没做的

**供应商凭证轮换的死路**（建议 6）照原判断没修：它要一个新动词，是独立的设计决定。
只把那两处会让下一个人不去查的注释改掉了。

（板端凭证关系检查原本也在这一节，机主随后要求做掉，记在第八部分。）

## 7.5 验证

按 `多会话协作的失效模式` 的 A/B 做法（worktree 位置敏感，先取基线）：

```
基线（干净树）      1067 passed, 48 skipped, 0 failed
改动后              1075 passed, 48 skipped, 0 failed      （+8 个新测试）
ruff check src tests  仅剩 1 个先前就存在的 B905，在本次未触碰的文件里
```

新增的 8 个测试，每个钉住一条这次才成立的性质：

- 生成的输入集里不含任何 render 字段（对每个声明了 `rendered` 的文件）
- 遗留的 render 字段被撤回，且只撤它一个，旁边的凭证不动
- 契约检查把"持有 render 字段"报成它自己的错，而不是笼统的 key set drifted
- `channel.env` 和别的文件一样被增量修复，且缺失的共享密钥是**拷贝**不是重铸
- 可选凭证在与不在都合法，都不触发修复
- renderer 多写或少写一个字段都会失败
- convergence 忽略 Host 绑定字段的差异而照常收敛
- 但真正被轮换的已声明凭证仍然被拒（收窄不能丢掉它存在的理由）

真机侧只读核验：板上 `channel.env` 满足新闸门；机主现有工作站输入集在副本上跑撤回，
落到合法状态（撤掉 `EIDOLON_LIVEKIT_CLIENT_URL`，其余 8 键不动）。
**没有改动机主的真实输入集，也没有在板子上写任何文件**——`deploy` 不经过契约检查，
所以现状继续可用；那个字段会在下一次 `install --apply` 或 Mac 侧运行时自动撤回。


---

# 第八部分：板端凭证关系检查（2026-09-17，机主追加）

第七部分把它列为"明确没做"，机主指定要做。

## 8.1 三个判断

**范围比原计划大。** 原本说的是"那四对静默的"。实际做出来覆盖 `SHARED_CREDENTIALS`
**全部 17 对**——写测试时才发现我第四部分那句"六对"说的是"涉及 `channel.env` 的六对"，
而整张表是 17 对，**板上一对都没被检查过**。既然机制一样，没有理由只查六对。

**挂在 `doctor`，不挂 readiness。** readiness 事实会让发布回滚；一对凭证不一致既不是
某次发布造成的，也不是它能修的，拿它回滚发布是错配。`doctor` 是"这台 Host 哪里不对"的
动词，而且它不拦任何东西。

**不拦 deploy。** 这是唯一需要反复权衡的一条。一对不一致永远是真缺陷——按构造它们在
install 时相等，没有任何合法路径让它们不同。但**今天没有动词能修它**：`converge` 只加
不改，`install` 见到不同直接拒，剩下的只有重装。仓里这一带自己的注释写过
「A gate that refuses without saying what to run is a gate people learn to work around」，
以及 app 检查当年为什么撤掉拒绝、保留测量。所以：**报告，不拒绝**，并且把这个取舍写进
代码，因为下一个人一定会问。

报告也不是塞进一个没人读的字段：`converge-inputs` 的 `next` 那句话会直接变成
「this Host holds two different values for: <标签>。Convergence adds keys and cannot
repair that…」。

## 8.2 形状

和 `declared_secret_env_keys()` 完全同构——**声明在工作站，比较在板上**：

```
install_inputs.declared_credential_relationships()      ← 由 SHARED_CREDENTIALS 推出
        │  随 payload 下发（target_payload 和 converge 两条）
contract.declared_credential_relationships(payload)     ← 板上校验形状，不认的直接拒
secret_inputs.verify_relationships(pairs, root)         ← 读两边、比较、只回名字
        ├── doctor-host          → checks["credential_relationships"]，假则 degraded
        └── converge-secret-inputs → report["relationships"]
```

理由和它旁边那条声明一样，而且在这里更尖锐：**一个自带副本的 agent 就是会漂移的第二意见
——而漂移正是这条检查要找的东西**，用第二副本去造它是荒谬的。

几条刻意的取舍：

- **值不出板子。** 在板上比较，报告只回标签和 `file:key` 两个端点。摘要也不回：对操作者
  没有更有用，却多一样需要小心对待的东西。
- **比不了不算通过。** 缺文件、或文件在而键不在，都记进 `unchecked` 并让 `doctor` 变红。
  这些文件每一个都属于完整安装，"没得比所以算一致"正是这条检查要终结的那种谎。
  文件缺和键缺分开报，因为修法不同——后者 `converge` 能修。
- **老工作站不发声明 = 没人问过。** `status: not_declared`，`doctor` 不红。
  与 `declared_capabilities` / `declared_management_networks` 的既有约定一致。
- `doctor_host` 增加了 `root` 参数（默认 `/`），和 `converge(data, root)`、
  `withdraw_hub_hostname(root=...)` 同一个形状。没有它这条检查无法测。

## 8.3 仍然没做：修

**检查能告诉你哪一对坏了，没有任何东西能把它修好。** 这是有意的，理由值得写下来：

工作站的输入集被 `validate_install_input_contract` 证明是自洽的，所以板上两边不一致时
至少有一边和工作站不同，"把板上那份改成工作站那份"是良定义的。但它会在一台正在运行的
Host 上改写凭证——正是 `converge` 的「What it will not do」明令禁止的——而且如果操作者
是**故意**在板上改过某个值、工作站那份才是旧的，这个"修复"就是在回退他们。

那是一个独立的设计决定（谁是权威、什么时候允许 ops 覆盖 Host 上的凭证），不该塞进一次
检查里。检查的形状已经为它留好位置：`mismatched` 里每一项都带着两个端点。

## 8.4 验证

```
基线（第七部分收尾）  1075 passed, 48 skipped
第八部分之后          1084 passed, 48 skipped      （+9 个新测试）
ruff                  仍只剩那 1 个先前就有的 B905
```

新增测试钉住的性质：声明由 `SHARED_CREDENTIALS` 推出且经得起过线、不一致被点名且
**值不出现在报告里**、一致报一致、比不了不算通过（文件缺与键缺分开）、老工作站不算失败、
agent 拒绝放不下的关系、`converge` 报告它修不了的那一对、以及 **`doctor` 是变红的地方**。

真机端到端（opi5max，用真正会下发的 `injected_script()`，`apply=False` 只读）：

```json
"relationships": { "declared": 17, "compared": 17,
                   "mismatched": [], "unchecked": [], "status": "agreed" }
"applied": false
```

17 对全部比对、全部一致，板上没有写入任何东西，临时文件已清理。

---

# 第九部分：修复（2026-09-17，机主追加）

第八部分把"修"列为明确没做，机主指定要做。

## 9.1 一条规则让"会不会回退操作者"变成构造上不可能

我之前拒绝做这一半，理由是"如果操作者是故意在板上改过某个值、工作站那份才是旧的，
这个修复就是在回退他们"。真正想清楚之后，这个顾虑有一个干净的解法：

> **触发条件是板上自己不一致，不是板上和工作站不一致。**

顺着推一遍：

- 操作者**有意**改一个凭证时，会把它的每一份副本都改掉——否则产品当场就不工作。
  那些副本彼此一致，这个动词找不到分歧，**碰都不碰**，哪怕它们全都和工作站不同。
  「回退操作者」不是靠一个开关避免的，是**不可达**的；
- 副本之间不一致的板子**已经是坏的**。任何能造成这个状态的路径都只写了一份没写另一份。
  **不存在"它触发了、而板子本来是好的"这种状态。**

对齐到哪个值：暂存的那份，也就是本机输入集——它自己的副本在暂存之前先被证明相等。
那正是 `install` 当初写进去的值，所以结果是"操作者当初装出来的那台 Host"，别的什么都没轮换。

## 9.2 必须按等价类修，不能按对修

`SHARED_CREDENTIALS` 声明的是**对**，17 对；连通分量是**凭证**，13 个。
companion authority token 住在 5 个文件里、由 4 对连起来：

```
data.env  kernel.env  admin.env  agent.env  channel.env
   Y          Y           Y          Y          W        ← 板上
```

这时只有 `data↔channel` 一对不一致。**只修这一对**（改成工作站的 W）会让 `data.env` 变成 W，
而和它本来一致的 kernel/admin/agent 仍是 Y——**同一台板子，换了个坏法**。
所以单位必须是凭证，不是关系。分组在工作站算好随 payload 下发，理由和别的声明一样。

## 9.3 第三个动词，不是第二个动词的一个模式

仓里这一带现在是三个动词，三份不同的安全契约：

| 动词 | 遇到"板上已有一个不同的值"时 |
|---|---|
| `install` | 逐字节比较，**拒绝** |
| `converge-inputs` | 只加缺的键，**拒绝**替换 |
| `repair-credentials` | **替换**——但仅限板上自相矛盾的那个凭证 |

没有做成 `converge-inputs --repair`：`converge` 之所以敢在一台正在工作的 Host 上跑，
靠的就是"它从不替换任何值"这句话。把一个专门替换值的模式塞进去，这句话就变成有条件的了。
仓里自己为 `install`/`converge` 分家写过同样的理由。

计划等级 `REVERSIBLE`（apply 时；dry 是 `NONE`），不是 `NONE` 也不是 `IRREVERSIBLE`，
理由写在 `plans.py` 里：它替换一个值，所以不是 NONE；它不轮换任何东西、不碰 authority data、
不动 Host identity，写进去的是本机随时能再写一次的值，所以不是 IRREVERSIBLE。
**真正回不来的是它覆盖掉的那个值**——而那个值所在的凭证在板上是分裂的，
意味着拿着它的那一方本来就在认证失败。

## 9.4 其余的守卫

- **先证明本机。** `validate_input_contract()` 在**暂存任何东西之前**跑。对齐的目标来自本机
  输入集，一个自己都不自洽的输入集会把某一份任选的副本写到所有地方。
- **暂存值必须自洽。** 板上再验一次；不等就拒，不猜。agent 不信任下发方。
- **只改那一个字段**，不重写文件里别的键；原子替换，保持 mode/owner。
- **键不在 = 不是它的事。** 那是 `converge` 加的。两个动词写同一个凭证但规则不同，是坑。
- **值不出板子。** dry 不暂存（没有理由为了报告"不需要"而把本机凭证放到板上），
  所以 dry 报不出值——它报的是**哪些槽位彼此相等**，这正好告诉操作者"channel.env 是那个异类"。
- **暂存目录用完即清。** `repair` 有自己的 stage id，并在 `finally` 里 `cleanup-stage`。
  （`converge-inputs` 不清，靠下次同 id 运行时清——那是既有行为，本次没动，但值得单独看一眼。）
- **`doctor` 现在说得出该运行什么。** 之前它只能报 false，因为那时没有动词可报。

## 9.5 验证

```
第八部分收尾  1084 passed, 48 skipped
第九部分之后  1093 passed, 48 skipped      （+9 个新测试）
ruff          仍只剩那 1 个先前就有的 B905
```

新增测试逐条钉住上面的规则，其中两条最重要：

- **一致地改过的板子绝不被碰**——三个槽位全都和暂存值不同，`apply=True`，结果
  `consistent`、`applied: false`、三个文件一字未动；
- **按类不按对**——三对一分裂的场景，断言写的是"和少数派不同的那两个"，
  而不是"跨越分界线的那一对"。

真机端到端（opi5max，真正会下发的 `injected_script()`，`apply=False`）：

```json
{"status": "consistent", "classes": 13, "divided": [], "repaired": [],
 "unchecked": [], "restart_required": [], "applied": false}
```

13 个类全部一致。事后核对 `/etc/eidolon/*.env` 时间戳，与本次会话开始时完全相同
（Sep 8 install，Sep 14 那次完整 refresh），**板上没有任何写入**，临时文件已清理。

---

# 第十部分：暂存目录用完即清（2026-09-17，收尾）

第九部分记下"`converge-inputs` 的暂存目录不清"当作遗留项，机主指定一并改掉。

## 10.1 查下来是三处，不是一处

把"谁暂存、谁清理"全部列出来之后：

| 动词 | 暂存内容 | 原本清理吗 |
|---|---|---|
| `install` | 全套输入集 | **会**（显式 `cleanup-stage`） |
| `repair-credentials` | 全套输入集 | **会**（第九部分加的） |
| `converge-inputs` | 全套输入集 | 不会 ← 机主点名的 |
| `HostLayer.refresh` | Host identity 私钥、Hub TLS 私钥、两个 Host 绑定文件 | 不会 ← **比点名那个更糟** |
| `HostLayer.refresh_release` | hub 设置、ingress 程序、unit 文件、3 个 yaml | 不会（**全是非密材料**） |

`stage_install_files` 只在**进门时**清一次同 id 的目录，所以没有出门清理的动词，会把一份副本留在
`/var/tmp` 直到下一次用同一个 release id 暂存——对一台不再需要收敛的 Host 来说就是永远。

## 10.2 改了两处，留了一处

**改 `converge-inputs`**：机主点名的。它为了送一两个键而暂存整套输入集。

**也改 `HostLayer.refresh`**：它泄露的是这几个动词里最宽的一组——`host_identity.ed25519`
（Ed25519 私钥）、`hub.key`（TLS 私钥）、`channel.env` 和 `local-api.env`。
动它之前先查证了后续步骤不读这个目录：唯一到达它的 `authority-restore` 用的是自己的
`eidolon-authority-*`（`hostagent/authority_restore.py:141`）。

**没改 `refresh_release`**：它暂存的全是非密材料（hub 设置、ingress 程序、unit 文件、3 个
非密 yaml），而它在 deploy 的关键路径上，后面还有 activate。收益是"整洁"，代价是要在收尾
阶段验证一条发布路径——不值当。记在这里。

三处都用 `try/finally`，因为**失败的那一次恰恰是最可能被撂下不管的那一次**。

## 10.3 变异验证

这三条断言如果对"有没有清理"不敏感，就等于什么都没钉住。所以拆掉 `converge` 和 `refresh`
的清理各跑了一遍：

```
注入变异后：
  test_converge_takes_its_staged_credentials_back_off_the_host        FAILED ✓
  test_converge_clears_its_stage_even_when_the_host_refuses           FAILED ✓
  test_the_host_layer_refresh_takes_its_staged_private_keys_back      FAILED ✓
  test_repair_takes_its_staged_credentials_back_off_the_host          passed ✓（没动它的清理）
  test_a_dry_convergence_stages_nothing_to_clean                      passed ✓（它断言的是"不该有清理"）
```

红的正好是该红的那三条。随后还原并确认工作区无变异残留。

断言写成"同一个 release id 的 `cleanup-stage` 至少出现两次"，而不是"出现过"——因为
`stage_install_files` 进门时就会清一次，只断言"出现过"在删掉出门清理之后仍然会绿。

## 10.4 验证

```
第九部分收尾  1169 passed, 48 skipped
第十部分之后  1174 passed, 48 skipped      （+5）
ruff          仍只剩那 1 个先前就有的 B905
```
