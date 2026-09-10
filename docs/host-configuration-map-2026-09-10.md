# Host 与模型配置机制清单

核对日期：2026-09-10。Ops 最新核对到 `e77dd28`，包含当前未提交的 `hostagent/app_contract.py`、`lan_observation.py` 和相关测试；原始配置备份基线为 `7860986`。相关网络运行时核对到 Kernel `41ed793`。根据仓库配置和生成/下发代码整理；未连接目标机，不据此判断线上进程已经加载了这些配置。本文不包含密钥值。

实施更新：用户随后授权修复和 Pi5 真机验证，基线已推进到 `f1cd4b2`。SSH 信任与 LAN 歧义修复已在工作树实现，Pi 的强制有线策略已关闭；现场步骤与生效证据见[验证记录](/Users/manson/ai/eidolon/eidolon_ops/docs/pi5-optimization-validation-2026-09-10.md)。

## 1. 先识别三个 Host 的配置入口

| 命令别名 | Host ID | Host profile | operations config | 执行方式 |
| --- | --- | --- | --- | --- |
| mac | mac-dev | [mac.toml](/Users/manson/ai/eidolon/eidolon_ops/config/hosts/mac.toml) | [eidolon-pi.toml](/Users/manson/ai/eidolon/eidolon_ops/config/eidolon-pi.toml) | 本地 source-run / supervisord |
| pi5 | eidolon-pi5 | [pi5.toml](/Users/manson/ai/eidolon/eidolon_ops/config/hosts/pi5.toml) | [eidolon-pi.toml](/Users/manson/ai/eidolon/eidolon_ops/config/eidolon-pi.toml) | SSH / systemd |
| rk3588 | eidolon-opi5max | [rk3588.toml](/Users/manson/ai/eidolon/eidolon_ops/config/hosts/rk3588.toml) | [eidolon-rk3588.toml](/Users/manson/ai/eidolon/eidolon_ops/config/eidolon-rk3588.toml) | SSH / systemd |

`rk3588` 是配置文件名形成的命令别名，`eidolon-opi5max` 是具体机器 ID，二者不是同一层命名。当前 Mac 与 Pi 共用 operations config，但 Host 路径、生成环境和执行适配器不同，不等于两者运行配置完全一样。

SSH 入口也仍有差异：Pi 是 `eidolon-pi5.local`，OPi 当前是台架静态地址 `10.42.0.2`。它们是工作站连接板子的地址，不是产品 Host ID，也不应直接替代手机接入的 Hub 名称或网络观测结果。

## 2. 现有配置机制

| 机制 | 决定什么 | 配置源与实现 | 生成物或生效位置 |
| --- | --- | --- | --- |
| Host profile | 机器身份、平台、driver、目录、App/LAN 接入和配对码文件引用 | `config/hosts/*.toml`；[paths.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/paths.py)、[host.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/host.py) | 选择 local/SSH 适配器及所有后续路径；`.example.toml` 是模板 |
| operations config | SSH、foundation profile、源码仓库/revision、服务清单、私密输入引用、Host settings overlay | `config/eidolon-*.toml`；[config.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/config.py) | 解析为本次部署参数；TOML 本身不会作为业务配置直接让 Agent/Channel 读取 |
| 操作者公钥声明（新增） | 板子接受哪些 SSH 操作者，独立于当前工作站用哪把私钥 | Pi `[host].operator_keys_file` → [eidolon-pi.operators](/Users/manson/ai/eidolon/eidolon_ops/config/eidolon-pi.operators)；[bring_up.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/bring_up.py) | bring-up 写入 Ops 账号的 `~/.ssh/authorized_keys`；shell 交付将集合收敛到声明，包含删除不再声明的 key |
| Host SSH 信任（新增操作） | 工作站信任板子的哪把 SSH host key | `[host].known_hosts_file`；[host_keys.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/host_keys.py)、controller.trust_host_key | Pi `.eidolon-ops/pi5/known_hosts`；OPi 仍为工作站 `~/.ssh/known_hosts`；SSH 使用 StrictHostKeyChecking / HostKeyAlias |
| 起机平台渲染（新增） | OS 刷好后具备账号、无交互 sudo、sshd/mDNS 和有线链路配置 | Host/operations 配置 → BringUp → foundation 对应 renderer；[bring_up.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/bring_up.py) | Pi boot-medium 输出 `user-data`、`meta-data`、`network-config`；shell 输出 `eidolon-bring-up.sh`，由操作者在板上执行；不刷 OS 镜像 |
| 基础环境 profile | OS/架构/包/工具版本、下载摘要、模型能力所需工具 | [foundation.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/foundation.py)、[目标 foundation.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/hostagent/foundation.py) | provision/install 安装并验证基础环境；OPi 使用 `ubuntu-2604-rk3588-arm64-v1` |
| capabilities + 组件契约 | 安装哪些可选模型服务、占用哪些端口、携带什么模型制品 | operations config 的 `[capabilities]`、`[services]`、`[sources]`；[models component.toml](/Users/manson/ai/eidolon/eidolon_models/ops/component.toml) | `local_asr` → ASR 8768；`local_llm` → LLM 8769；`local_tts` → TTS 8770；Host capability 写入 Host 环境，供运行时筛选服务 |
| settings 模板与 overlay | ASR/TTS provider、LLM 名称/地址、业务开关和运行参数 | 各组件 `config/settings.yaml` → [product_settings.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/product_settings.py) 的 PRODUCT_OVERLAY → Host `[[settings.overlay]]` | 工作站私密 inputs 内生成三份 YAML；产品板安装到 `/etc/eidolon/{agent,channel,memory}.yaml` |
| 私密输入 | 云服务 key、内部共享 token、Host identity、factory setup code | 各组件本机 `config/.env` 白名单；[install_inputs.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/install_inputs.py)、[private_inputs.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/private_inputs.py)、[provider_inputs.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/provider_inputs.py) | OPi 的工作站目录 `.eidolon-ops/opi5max/inputs/`；目标 `.env` 在 `/etc/eidolon/`，Host identity/setup code 在 `/var/lib/eidolon-bootstrap/` |
| Host 派生配置 | Hub ID/域名、证书、Owner trust、Local API/Channel 的 Host 相关环境、TLS ingress | [host_identity.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/host_identity.py)、[host_application.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/host_application.py)、[owner_domain_assets.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/owner_domain_assets.py) | `/etc/eidolon/generated/hub.yaml`、`tls/`、`owner-domain/`、Hub systemd drop-in；工作站签名材料在 `.eidolon-ops/opi5max/owner-domain/` |
| 模型服务运行配置 | 监听端口、模型目录、引擎路径、上下文长度、CPU/NPU 核分配等 | [cpu-allocation.env](/Users/manson/ai/eidolon/eidolon_models/deploy/cpu-allocation.env)、`eidolon_models/deploy/systemd/`、`scripts/`、各服务 `config.py` | systemd `Environment` / `EnvironmentFile` → launcher → Python/C++ 引擎；CPU 配置安装为 `/etc/eidolon/cpu-allocation.env` |
| 模型包与权重配置 | 实际使用哪份权重、文件布局、摘要和来源 | ASR/TTS 版本目录中的 `manifest.json` 与模型配置；外部 GGUF 由 `ops/component.toml` 声明 | ASR/TTS 随 models release；Qwen GGUF 单独放 `/var/lib/eidolon/models/qwen3-1.7b/`；与“业务调用谁”的 overlay 分开 |
| Mac source-run 配置生成 | 将产品配置转换为本机路径和 supervisord 环境 | [local_product.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/local_product.py)、[source_assets.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/source_assets.py)、[run_all.sh](/Users/manson/ai/eidolon/eidolon_ops/deploy/dev/run_all.sh) | 当前生成到 `/Users/manson/ai/eidolon/.eidolon/mac-product/config/` 的 `env/`、`settings/`、`tls/`，不写产品板的 `/etc/eidolon` |
| 网络观测与 LiveKit 运行状态（更新） | 当前接口地址、可用传输链路，以及 LiveKit 是否消费了当前网络输入 | [endpoints.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/endpoints.py)、[transport.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/transport.py)、[lan_observation.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/lan_observation.py)、[目标 app_contract.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/hostagent/app_contract.py)；Kernel [system-services.yaml](/Users/manson/ai/eidolon/eidolon_kernel/config/system-services.yaml)、[service_manager.py](/Users/manson/ai/eidolon/eidolon_kernel/eidolon_system/application/service_manager.py) | Host 地址来自系统观测；eidolond 协调网络变化；Ops 从 system.sock 的 `/api/system/v1/services/livekit` 读取 network_current。Mac wrapper 默认原生 ICE，`EIDOLON_LIVEKIT_NODE_IP` 仅作显式覆盖 |

上述机制并非都要人工配置：profile/operations config 和组件源文件是声明，inputs 中的 YAML 与目标 `/etc` 大部分是派生结果，网络地址和服务状态则是观测事实。手改派生结果可能在下次 prepare/deploy 时被重新生成。

SSH 操作者私钥、板子 SSH host key、产品 Host identity、Owner 签名材料和 factory setup code 是不同用途的材料；不能因 SSH 换板子或撤销操作者就一起轮换产品身份。`app.setup_code_file` 现在仅保存路径，`AppAccess.factory_setup_code()` 在 commissioning-code 或 HostLayer 暂存安装/刷新材料实际需要时读取；缺少它不会在加载 profile 时阻挡 status 等无关操作。HostLayer 从同一引用生成私密暂存的 `factory_setup_code`，无需另维护第二份值。

## 3. 模型配置需要分成三层理解

### A. 安装能力

`[capabilities].provides` 决定本地服务是否进入部署和运行拓扑。当前 OPi 仍声明 `rknpu2/local_asr/local_llm/local_tts`，也保留三个模型 unit。

### B. 调用路由

以下是 **当前仓库声明**，不是本次现场探测结果：

| 调用方/功能 | 当前正式配置 | 9 月 10 日备份配置 |
| --- | --- | --- |
| Channel STT | `bailian` | `local_asr` |
| Channel TTS | `bailian` | `local_tts` |
| Agent 主 LLM | `openai/deepseek-v4-flash`；api_base 继承模板的云端地址 | `openai/qwen3-1.7b`，`http://127.0.0.1:8769/v1` |
| Channel 自己的 LLM 配置 | `deepseek-v4-flash`，`https://api.deepseek.com` | 未在备份里单独 overlay，继承选定模板 |
| Memory 抽取 LLM | `openai/deepseek-v4-flash`，`https://api.deepseek.com/v1` | 未在备份里单独 overlay，继承选定模板 |

Channel 的 LLM 配置不能简单等同于正式对话主 LLM；当前仍保留 Channel → Agent RPC 的主路径。Memory 有自己的抽取 LLM 配置，改 Agent 的模型并不自动修改它。

因此，“保留本地模型运行能力”与“业务走云端”可以同时成立。只改 provider 不等于卸载/停止本地服务，也不保证释放其内存。

### C. 本地推理参数和权重

- ASR：[unit](/Users/manson/ai/eidolon/eidolon_models/deploy/systemd/eidolon-asr.service)、[launcher](/Users/manson/ai/eidolon/eidolon_models/scripts/eidolon-asr)、[config.py](/Users/manson/ai/eidolon/eidolon_models/src/eidolon_models_asr/config.py)。读取 `EIDOLON_ASR_*`；模型根由 launcher 指向 release，模型包括 streaming/offline Paraformer 与标点模型。`auto` 当前选择 ONNX CPU，不代表 NPU。
- LLM：[unit](/Users/manson/ai/eidolon/eidolon_models/deploy/systemd/eidolon-llm.service)、[launcher](/Users/manson/ai/eidolon/eidolon_models/scripts/eidolon-llm)。启动 llama-server；unit 指定 Qwen3-1.7B Q4_0 GGUF、8769 和 context 2048；launcher 将 `EIDOLON_LLM_*` 转为启动参数。
- TTS：[unit](/Users/manson/ai/eidolon/eidolon_models/deploy/systemd/eidolon-tts.service)、[launcher](/Users/manson/ai/eidolon/eidolon_models/scripts/eidolon-tts)、[config.py](/Users/manson/ai/eidolon/eidolon_models/src/eidolon_models_tts/config.py)。使用 CosyVoice2，launcher 默认指向 models release 的 `tts/cosyvoice2-rk3588/2026-07-21/model`；引擎在 `/var/lib/eidolon/tts/engine/`，由 ExecStartPre 构建。NPU core 和 CPU affinity 是不同参数。
- CPU 分配当前声明：ASR 不绑核；LLM decode `0-3`、prefill `0-7`；TTS `4-6`。源文件是 `deploy/cpu-allocation.env`。它目前是 models 仓的共同发布资产，**并不是每个 Host TOML 都有一份独立的核分配 overlay**。
- Memory 还存在 embedding 模型配置，和抽取 LLM 是两件事；`EIDOLON_MEMORY_EMBEDDING_MODEL`/memory 环境影响 encoder，不应因切换对话 LLM 而一起更换。

## 4. 生成与下发流程

```text
./eidolon rk3588 <操作>
  → config/hosts/rk3588.toml
  → adapter.operations_config
  → config/eidolon-rk3588.toml
  → 解析平台、sources、capabilities、私密输入路径和 overlay
```

settings 的明确覆盖顺序为：

```text
选定 commit 中的组件 config/settings.yaml
  → PRODUCT_OVERLAY：产品通用修正（prod、INFO、路径/服务端点等）
  → Host 的 [[settings.overlay]]（后应用）
  → .eidolon-ops/opi5max/inputs/agent.yaml、channel.yaml、memory.yaml
  → SSH 私密暂存 /var/tmp/eidolon-secrets-<release-id>/
  → 兼容性和文件权限校验
  → /etc/eidolon/ 下正式 YAML
  → release 激活、启动服务、doctor/app-ready
```

对应代码：[product_settings.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/product_settings.py)、[settings_overlay.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/settings_overlay.py)、[release_preflight.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/release_preflight.py)、[host_layer.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/host_layer.py)、[目标 host_application.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/hostagent/host_application.py)、[release_transaction.py](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/release_transaction.py)。

这不是“所有配置统一按一个优先级合并”：settings 有上述顺序；secret 有单独生命周期；systemd 环境与 launcher 默认值又是另一条链。当前模型/部分端口契约仍有读取工作树的路径，未完全统一到 exact commit，已列入前一份评审。

| 操作 | 对配置的作用 |
| --- | --- |
| `bring-up --via boot-medium\|shell --output DIR [--apply]` | 只使用公开起机声明；无 apply 返回交付计划，有 apply 写输出文件。boot-medium 由板子首次启动消费，shell 文件须在板上作为 root 执行；不是远端自动执行命令 |
| `trust-host-key [--apply] [--replace SHA256:...]` | 扫描并展示板子 key；apply 写本机 known_hosts，已有不同 key 时要求明确指纹。本轮已补哈希条目识别及共享 alias 保留；匹配 wildcard/revoked/CA 等规则时拒绝自动改写 |
| `init-inputs` | 首次生成/验证私密输入：导入组件本机 provider key 白名单，生成内部 token/Host identity，渲染 settings；不部署 Host |
| `install --apply` | 基础环境、身份/目录、首次状态初始化、输入与派生配置安装、启动与发布健康门禁；App 状态另作观测，返回 degraded 不阻挡首次安装 |
| `deploy/update --activate` | 更新 release，同时重算并下发三份产品 settings 与可刷新 Host 配置；不是全量 secret 轮换命令 |
| `converge-inputs --apply` | 补齐工作站和 Host 缺失的凭据键；已有键不会在目标端自动替换；结果提示哪些服务需要 restart |
| `restart` | 让进程重新加载已下发的环境/配置；不能把工作站尚未下发的修改自动变成目标配置 |
| `commissioning-code [--code ...]` | 从显式参数或 profile 引用读取配对码，没有指定时由 Host 生成；当前 Host 签发无时限的一次性窗口，消费或再次签发关闭；Ops 已删除 `--ttl-seconds`，无期限的 expires_at 输出 null |
| Mac `start/restart` 的 source-run 路径 | 使用本机生成环境和 settings，由 supervisord 启动；与产品板部署流程分别实现 |

Pi 新机链路现在是：外部刷好 OS → bring-up 交付 → 板子启动/操作者执行脚本 → 核对并记录 SSH host key → init-inputs → install → app-ready。单独修改 `.operators` 或生成 shell 文件都不会立即修改板子的授权；普通 deploy/update 也未调用这条授权收敛路径。

这里 app-ready 是独立检查：普通 deploy 的发布成功由 release doctor 等判断，App-ready degraded 或探测异常被记录，不触发回滚。authority-restore 则明确等待 App-ready，升级其判定协议时需要单独处理旧 Kernel 兼容性。

## 5. 本次备份的范围和限制

已核实提交 `7860986` 新增：

- [operations config 备份](/Users/manson/ai/eidolon/eidolon_ops/config/eidolon-rk3588.local-backup-20260910.toml)：保留本地 ASR/TTS 和本地对话 LLM 的路由配置、capabilities、sources、输入路径等。
- [Host profile 备份](/Users/manson/ai/eidolon/eidolon_ops/config/hosts/rk3588.local-backup-20260910.toml)：保留机器身份、路径、App 配置和 operations config 引用。

这两份是声明文件快照；它们不内含私密 `.env`、Host 私钥、owner-domain 签名材料、目标机 systemd drop-in、CPU 配置实况、模型权重或数据库。也不锁定当时所有源码 commit——sources 仍主要是路径，模型/模板/CPU 参数可随对应仓库 HEAD 改变。本文只确认这两份备份文件，不能据此断言另一个任务没有在其他位置做额外备份。

有两个已确认的细节：

1. 备份 Host profile 的 `adapter.operations_config` 仍是 `../eidolon-rk3588.toml`，指向当前云端路由配置，而非旁边的备份文件。因此直接选“备份 Host”不会恢复旧路由。
2. `eidolon` 与 Console 都扫描 `config/hosts/*.toml` 且仅排除 `.example.toml`，这个备份文件会被当成 Host 候选，且与正式文件有相同 Host ID。备份目录应与活动 inventory 分离；本次仅记录，没有移动文件。

项目另外还有 `backup/restore`（组件 authority/Memory 数据快照）、`authority-backup/authority-restore`（权威恢复协议）、release/cutover snapshot（部署恢复）。这些都不应直接等同于“完整机器配置备份”。如果目标是完整复现一台 Host 的模型配置，需要一起记录 Host/operations 配置、源码 commit、渲染后配置摘要、CPU/服务覆盖配置、模型 manifest/摘要和私密材料的独立备份引用。

## 6. 新机制尚未覆盖的差异

| 项目 | Pi5 | OPi5Max / RK3588 |
| --- | --- | --- |
| SSH 入口 | `eidolon-pi5.local`，可发现多链路 | `10.42.0.2`，台架固定地址 |
| operator 公钥集合 | 已声明 `config/eidolon-pi.operators` | 尚未声明 operator_keys_file |
| known_hosts | `.eidolon-ops/pi5/known_hosts`，本机私有文件 | 仍指向本机 `~/.ssh/known_hosts` |
| 部署链路策略 | 本轮改为 `require_wired_release_upload = false`，有线优先但允许无线 | 仍显式要求有线，尚未修改 |
| bring-up foundation | `raspberry-pi-os-debian-arm64-v2` 有 boot-medium / shell renderer | `ubuntu-2604-rk3588-arm64-v1` 未声明 renderer；现有命令不会自动适配它 |
| 网络就绪门禁 | 新门禁要求 Kernel 与服务 manifest 支持 network_current | 同一目标 agent 门禁也适用，不能因为平台不同略过版本要求 |
| 模型与配置备份 | 按其已有配置 | 本文第 3、5 节的云端正式路由、本地能力和备份引用问题仍成立 |

已移除“从 YAML 的 node_ip 或旧日志证明 LiveKit 网络可用”的做法，但新门禁仍不等于手机 WebRTC 真机验证。本轮已让无默认路由、多候选场景报告歧义而不选择数字最小的 IP；已有显式 app.lan_ipv4 是解决此类歧义的配置入口。工作树修复与真机结果分别记录，不能混为线上已全部生效。
