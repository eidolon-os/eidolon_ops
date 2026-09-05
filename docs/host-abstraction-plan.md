# 把随 Host 变化的部分从代码里挪到配置

写于 2026-09-05，起因是 Orange Pi 5 Max（RK3588）要成为第三种 Host，
而它与前两种的差别恰好落在 ops 目前用代码而非配置表达的地方。

本文只做分析与方案，不含改动。所有论断都标了出处行号。

---

## 1. 现状：骨架是对的

三件事已经做对了，重构应当沿着它们走，而不是另起炉灶。

**① 平台注册表已存在。** `host.py:47` 的 `PLATFORM_PROFILES` 把平台做成表项，
`HostAdapter`（`host.py:56`）由 platform + transport/supervisor/packages 三个 port
组合而成。这一段的注释已经写明了意图：

> "One entry per real platform. A third board that is also Linux/systemd/apt
> belongs here as another row, not as another adapter."

**② 组件契约机制已存在。** `contracts/component-ops/v1.schema.json` 定义了组件向 ops
声明自己的方式——`units` / `ports` / `inputs` / `artifacts` / `state`。八个组件里
六个已经有 `ops/component.toml`。连 ops 自己安装的 NATS/LiveKit 也走同一个 schema
（`contracts/platform/component.toml`），而不是当例外写死。

**③ 硬编码的平台分支很少。** 全仓 23.7k 行里只有约 10 处直接判断平台，
且集中在 `paths.py:306,324`、`host_controller.py:69`、`paths.py:53-54` 的驱动映射。

**所以问题不是"缺抽象"，是"抽象的壳建好了但没装东西"。**

---

## 2. 缺口一：`PlatformProfile` 是空壳

```python
@dataclass(frozen=True, slots=True)
class PlatformProfile:
    """Data, not code: what this kind of machine is, and what it always offers."""
    id: str
    platform: HostPlatform
```

只有两个字段。而真正"这种机器是什么"的事实，全部作为模块级常量躺在
`foundation.py` 里，且都以 Raspberry Pi OS 为唯一取值：

| 常量 | 当前值 | 对 RK3588 |
| --- | --- | --- |
| `FOUNDATION_PROFILE` | `"raspberry-pi-os-debian-arm64-v2"` | 不同 |
| `FOUNDATION_OS_IDS` | `("debian", "raspbian")` | 要加 `ubuntu` |
| `FOUNDATION_OS_VERSIONS` | `("13",)` | 要加 `26.04` |
| `APT_MIRRORS` | debian / raspberrypi / security | ubuntu 源不同 |
| `APT_PACKAGES` | 通用 + Pi 专属 | 要加 RKNPU 相关 |
| `BOOTSTRAP_PACKAGES` | | 大体相同 |
| `FOUNDATION_SERVICES` | | 大体相同 |
| `JOURNAL_PERSISTENCE` | systemd 路径 | 相同 |
| `FOUNDATION_ARTIFACTS` | nats/livekit/uv/node | 要加 `librknnrt.so` |

这些常量在 `hostagent/foundation.py` 里还有**第二份副本**。

而且 `config.py:246` 要求配置里的 `foundation.profile` 与那个单一常量**逐字相等**：

```python
if foundation_profile != FOUNDATION_PROFILE:
    raise ...(f"foundation.profile must be the reviewed profile: {FOUNDATION_PROFILE}")
```

**这一条把"只能有一个 profile"钉死在了校验里。**

### 方案

把这九项从模块常量搬进 `PlatformProfile`，每个平台一份。形式上有两种选择：

* **A：仍是 Python 表**，`PLATFORM_PROFILES` 的每行从 2 字段长成 11 字段。
  改动最小，但平台数据仍在代码里。
* **B：每平台一个 TOML**，放 `contracts/platform/<id>.toml`，
  用与组件契约**完全相同的 schema 与 loader** 读入。
  与既有的 `contracts/platform/component.toml` 是同一条路，
  也符合那个文件自己写的理由——"described rather than excepted"。

**建议 B。** 理由是 ops 已经证明这条路走得通（平台自己的 NATS/LiveKit 就是这么描述的），
且加第四种 Host 时不需要碰 Python。`config.py:246` 的等值判断相应改为
"必须是已注册 profile 之一"。

---

## 3. 缺口二：组件契约不能表达"哪些 Host 才要"

schema 里 `artifacts` 已经存在，而且形状正确：

```json
{"id": ..., "kind": "model" | "binary", "install_root": ..., 
 "files": [{"path": ..., "sha256": ...}]}
```

它的 description 也已经说清了为什么需要它——"a release is exact git commits,
so whatever is not in git does not exist in a release"。`embedding_model.py`
按 SHA-256 钉 bge 权重，证明这条机制可用。

**但整个 schema 里没有任何键可以表达 host 条件。** 全文搜 `platform` / `host` /
`arch` / `when` / `only_on`，只有一处 `"platform"`，且不是这个用途。

于是无法表达：
* `eidolon-asr` 这个 unit 只在本地跑模型的 Host 上存在；
* Qwen3 的 `.rkllm`（2.3 GB）只有 RK3588 需要，Pi5 和 Mac 不需要；
* CosyVoice2 的六个 mel 分桶 `.rknn` 同上。

### 方案：用**能力**而不是平台名做选择器

不要写 `only_on = ["rk3588"]`——那会把组件重新耦合到具体机型，
第四块板子来了又要改每个组件。改为：

```toml
# host profile 声明它提供什么
[capabilities]
provides = ["rknpu2", "local_asr", "local_tts", "local_llm"]
```

```toml
# 组件声明它需要什么
[[units]]
id = "eidolon-asr"
requires_capability = "local_asr"

[[artifacts]]
id = "qwen3_1_7b_rkllm"
kind = "model"
requires_capability = "rknpu2"
```

ops 在装配 plan 时按 capability 过滤 units 与 artifacts。
`HostAdapter` 已经有 `capabilities` 与 `require()`（`host.py:66-77`），
只是目前只由 supervisor/packages 贡献——把 platform 的能力并进去即可，
**不新增概念**。

---

## 4. 缺口三（最严重）：`product_settings.py` 完全不知道 Host 是谁

```python
def product_settings(config, read_exact_file) -> dict[str, str]:
```

**签名里没有 host 参数。** 所有 product Host 拿到同一份渲染结果。
而渲染方式是对组件自己的 `settings.yaml` 做字符串替换：

```python
agent = _replace(agent, "env: dev", "env: prod", expected=1)
channel = _replace(channel, "  dump_wav: true", "  dump_wav: false", expected=1)
```

`_replace` 在计数不符时报 "pinned settings template drifted"。
这个模块自己的注释已经承认了问题：

> "Rewriting a component's shipped settings by string substitution breaks the
> moment that component improves the line being matched."

**这正是"有的 Host 本地跑模型、有的不跑"该落地的地方，而它现在落不了。**
channel 已经有了正确的表达方式：

```yaml
providers:
  stt_provider: bailian      # 云端
  tts_provider: bailian
  vad_provider: firered_pvad
```

要让 RK3588 用本地模型，只需要这两个值变成指向本地服务——
**channel 一行代码都不用改**。缺的是 ops 能不能按 Host 给出不同的值。

### 方案：把字符串替换换成按 Host 的结构化 overlay

组件照常发布自己的 `settings.yaml` 模板；每个 Host profile 带一份 overlay，
按文档名 + 键路径给值；ops 做结构化合并（YAML 层面，不是文本层面）。

```toml
# config/hosts/rk3588.toml
[settings.channel.providers]
stt_provider = "eidolon_models"
tts_provider = "eidolon_models"

[settings.memory.embedding]
model = "bge-small-zh"
```

带来的三件好处：

1. **ops 不再需要知道组件配置文件的内容长什么样**——`expected=1` 这类
   与组件行文耦合的断言全部消失，组件改自己的 settings 不再打断 ops。
2. **每台 Host 可以不同**，这是本次重构的目的。
3. 现有的 `env: dev → prod`、`dump_wav`、`avatar.enabled` 三处替换
   自然变成"所有 product Host 共用的 overlay"，不再是代码。

保留的校验：overlay 里的键路径必须在模板中存在，否则报错——
这保住了 `expected=1` 原本想防的那件事（组件漂移无声失效），
但检查的是**键是否存在**，而不是**某一行文本长什么样**。

---

## 5. `eidolon_models` 具体怎么落

今天的状态：有 `deploy/systemd/eidolon-asr.service`，
但 **ops 完全不认识它**——没有源条目、没有 `ports.yaml` 角色、没有 `ops/component.toml`。
八个组件里它和 `eidolon_vision` 是仅有的两个没有契约的。

需要新增 `eidolon_models/ops/component.toml`：

```toml
schema_version = 1
component_id = "eidolon_models"
contract_version = "1"

[[units]]
id = "eidolon-asr"
kind = "service"
exec = ".venv/bin/eidolon-asr"
serves = ["asr_stream"]
requires_capability = "local_asr"        # ← 缺口二

[ports.asr_stream]
default = 8767
bind = "loopback"
purpose = "funasr 2pass streaming ASR"

[[artifacts]]
id = "paraformer_zh_2pass"
kind = "model"
requires_capability = "local_asr"
install_root = "/opt/eidolon/models/asr"
files = [ ... sha256 ... ]

[[artifacts]]
id = "qwen3_1_7b_rkllm"
kind = "model"
requires_capability = "rknpu2"           # 只有 RK3588 有
install_root = "/opt/eidolon/models/llm"

[[artifacts]]
id = "cosyvoice2_rknn"
kind = "model"
requires_capability = "rknpu2"
install_root = "/opt/eidolon/models/tts"
```

**关键点：本地与云端的差别完全落在两处配置里**——
Host profile 声明的 capabilities（决定装哪些 unit 与哪些权重），
以及 Host overlay 里 channel 的 `stt_provider`/`tts_provider`（决定运行时走谁）。
`eidolon_models`、`eidolon_channel`、`eidolon_agent` 的逻辑代码都不动。

同一机制顺带解决 `eidolon_models` 内部的差异：TTS/LLM 的 unit 也挂
`requires_capability`，一台只做 ASR 的 Host 不会拿到 2.3 GB 的 `.rkllm`。

---

## 6. 落地顺序

按"每一步都能单独验证"排：

1. **缺口三（overlay）** —— 与 RK3588 无关，对现有两种 Host 就有价值：
   拆掉 ops 与组件配置文本的耦合。可先落，用 Mac + Pi5 验证行为不变。
2. **缺口二（capability 选择器）** —— schema 加字段 + plan 装配时过滤。
   现有六个组件不写 `requires_capability` 即视为"所有 Host 都要"，向后兼容。
3. **`eidolon_models` 的 `ops/component.toml`** —— 此时才有地方写 artifacts 与条件。
4. **缺口一（platform profile 外置）** —— 最后做。它改动面最大，
   但前三步做完后，加 RK3588 这一行的内容已经清楚了，风险最低。

前三步都不需要碰板子，**不产生 VPN 流量**。

---

## 7. 未决

* `hostagent/foundation.py` 与 `foundation.py` 那两份重复常量该合并还是保持镜像，
  取决于 hostagent 是否必须能独立于 ops 包运行——**未查证**。
* overlay 的合并语义（列表是替换还是追加）需要定，本文未给结论。
* `eidolon_vision` 同样没有契约，本文未涉及。
