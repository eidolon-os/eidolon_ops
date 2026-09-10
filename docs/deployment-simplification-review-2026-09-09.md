# eidolon_ops 部署职责与简化方案

评审日期：2026-09-09。基线：`eidolon_ops` HEAD `6ae7411`。

2026-09-10 二次复审（Ops `7860986`）：本方案有功能回归风险，不能直接整体替换。第 8 节补充的兼容约束和实施顺序优先于前文概括；当前只调整文档，未实施重构。

2026-09-10 增量复审：已对照 `7860986..e77dd28` 的 18 个提交，并纳入尚未提交的 `hostagent/app_contract.py`、`lan_observation.py`、`tests/test_target_agent.py`；运行记录的未提交改动不作为现场验收。相关 Kernel 核对到 `41ed793`。**当前结论、完成状态和下一步以第 9 节为准；第 8 节的兼容约束继续有效。** 本次只更新这份方案及配置机制清单，没有修改运行代码或执行部署。

随后用户授权实施本轮 4 项修复和部署链路优化，并在更换的 Pi5 上验证。实施基线推进至 `f1cd4b2`，第 10 节及[真机验证记录](/Users/manson/ai/eidolon/eidolon_ops/docs/pi5-optimization-validation-2026-09-10.md) 记录交付状态；前述“仅文档”指评审阶段。

## 结论

存在局部过度设计，但不建议重写或退回一组部署脚本。主要问题是：**同一个事实有多份手写声明，同一次发布有多段恢复流程，部署工具还承担了部分产品运行时和身份权威实现。** 这些设计增加维护面，却没有等比例增加可靠性。

建议把职责收敛为：**给定 Host、确定的发布内容和私密配置，将机器从已观测状态推进到目标状态，提供可解释的计划、恢复路径和证据。** 优先修复发布一致性与完整回滚，再减少重复配置，最后根据耗时优化增量部署。

本文是代码评审和实施方案，没有修改部署逻辑，没有连接或操作真实 Host。

## 1. 职责边界与实际规模

当前支持 Mac source-run、Raspberry Pi 和 RK3588。产品板共用 SSH/systemd/apt；Mac 使用本地 supervisord。初评时 Python 实现为 84 个文件、26,622 行，包含目标 agent、控制台、诊断及身份恢复。这是历史规模，行数不能单独证明过度设计。

`install` 仍以 **OS 已写入、SSH 可达、host key 已可信、操作账号具备无交互 sudo** 为前提。增量复审修正：Ops 已增加独立的 `bring-up` 和 `trust-host-key`，覆盖起机配置交付与 SSH 信任建立，不能再说整个 Ops 完全不负责 SSH-ready。`bring-up` 输出首次启动文件或由操作者执行的 shell，目前仅声明 Raspberry Pi OS 平台；它不下载或刷写 OS 镜像，OPi 平台尚未接入。应保留“起机 → 信任 → 首次部署”的阶段边界。

| Ops 应拥有 | Ops 应编排或消费 | 应由组件或其他模块拥有 |
| --- | --- | --- |
| Host inventory、平台基础环境、发布输入解析、传输、执行顺序、部署证据 | 服务启停、健康事实、配置校验、备份恢复、身份初始化 | 服务常驻调度、业务 schema/迁移、Owner 权威语义、在线 TLS 转发实现 |

以下设计应保留：

- 发布开始时把 HEAD 解析为 exact commit，支持显式 revision 重现；不要恢复“日常手动维护另一份 commit 清单”的旧模式。
- 不可变发布目录、独立环境、摘要校验和缺失制品传输。
- 新装与更新的不同前置条件，以及删除业务数据时的明确范围。
- Host identity/secret 与代码发布隔离，产品身份不能随普通升级重新生成。
- 真实健康门禁、组件状态所有权、代码回滚与不可逆数据变更的区别。
- 平台 profile 与少量真实执行适配器。三种 Host 足以支撑这些差异，不需要为此引入插件平台、任意工作流 DSL 或集群调度系统。

## 2. 最需要处理的发布正确性问题

### P0-A：发布的组件契约没有全部绑定到选定 commit

**代码事实：** `BundleTransfer._carry_component_artifacts()` 从 `self.config.sources[*].path` 调用 `read_component_contracts()`；loader 直接 `read_text()` 读取工作树中的 `ops/component.toml`。Host capability 端口生成也走这条路径。主代码、unit 和 settings 模板则有 exact Git object 读取机制。

因此，显式发布旧 revision 时，即使工作树完全干净，也可能组合出“旧代码 + 当前 HEAD 的模型声明/端口”。`--allow-dirty` 和操作过程中的工作树变化会进一步扩大这个缺口。摘要正确只能证明下载了契约指定的字节，不能证明契约属于本次代码版本。

恢复出厂的 `_authority_to_remove()` 同样读取工作站当前契约。删除范围应根据 **目标机已安装 release 的状态契约** 作证，不应让工作站新版本替旧部署回答。

**方案：** 复用已有 `SourceResolver`，让组件契约支持从已解析 commit 的 Git object 读取；本次运行只生成一份解析后的部署描述。将契约摘要、模型摘要和配置摘要写入现有 bundle/descriptor，而不是新增平行清单。reset 使用目标已安装 descriptor 中的状态声明，旧版本通过显式 legacy 分支处理。

**验收：** 在工作树 B 上显式部署 commit A，代码、契约、端口、模型和 settings 全部来自 A；封装后不再混入工作树新内容。保留现有 resume 语义：HEAD 已变化时仍要求显式指定原 revision 或使用新 release ID，而非悄悄接受新组合。缺少旧部署契约时不冒称已完成逐组件删除检查。

证据：`release_bundle.py:330`、`component_contract.py:329`、`host_layer.py:131`、`controller.py:866`。

### P0-B：代码与 Host 配置需要成为一个完整切换/恢复单元

**代码事实：** deploy 先创建 `host_cutover_snapshot`，刷新 `/etc` 等 Host 配置，再调用 Kernel 仓中的 release activator。activator 自己加锁、快照、停服务、切代码和恢复。Ops 后续以 doctor 判断发布健康；App-ready 是观测记录，不是 deploy 回滚门禁。doctor 失败等路径仍涉及 Host 快照恢复。这里修正初评将两种检查混称为门禁的措辞。

这并不是“没有回滚”，而是回滚范围被拆开了：

- 自动失败路径会先通过 release rollback 恢复并启动旧代码，再在外层 finally 中恢复旧 Host 配置；旧代码可能先读到新配置。
- `hostagent/cutover.restore()` 恢复文件后只做 `daemon-reload`，没有重新启动已经读取新配置的业务进程。
- 显式 `ReleaseTransaction.rollback()` 只调用 release CLI，没有对应 Host 快照恢复和完整 app-ready 复验。
- install/reset 使用 `eidolon-install.lock`，release 使用 `eidolon-release.lock`；Host snapshot/refresh 不在该 release 锁内。Console 的 busy 限制只覆盖本进程，不能代替跨 CLI、跨工作站的目标机互斥。

以上是代码可见的恢复和互斥边界缺口；本次未在真实机器注入并发、断网或掉电，不能据此宣称已经发生了数据损坏。

**方案：** 在现有目标执行器中建立一个部署事务入口及公共 mutation lock，事务内执行“快照 → 停止受影响服务/协调器 → 安装配置和代码 → 启动 → 门禁 → 提交”。恢复时先恢复配置和代码，再启动旧服务并复验。自动恢复与显式 rollback 调用同一实现。

工作站继续拥有“什么算发布成功”的策略，但把判定所需的门禁规范下发；目标保存 durable journal，使工作站断线后仍能查询和恢复。无需增加永久 Ops daemon。旧发布自带 activator 的兼容入口保留。

`forward-only` 的状态屏障必须保留：跨过不可逆状态变化后要求向前修复，不自动启动旧解释器。目标机 journal 必须在副作用前记录这个屏障，不能只依赖工作站内存中的布尔值。

**验收：** 在配置刷新后、代码切换后、服务启动后分别中断；恢复后配置/代码来自同一版本且门禁通过；显式与自动回滚结果一致；两个 CLI 同时 mutate 同一 Host 时有一个在任何 live 变更前被拒绝。

证据：`release_transaction.py:196`、`:579`、`:752`，`hostagent/cutover.py:186`，`hostagent/install.py:72`、`hostagent/reset.py:141`；Kernel 仓 `eidolon_deploy/activation.py:105`、`linux.py:229`；`console/runs.py:201`。

### P0-C：模型制品应复用已有可靠传输机制

**代码事实：** release artifacts 已采用目标端重新哈希、同文件系统临时文件和原子发布；另一条 component artifacts 路径则信任 `.files-sha256` 标记。目标安装只检查标记存在，没有按可信文件清单重新验证内容，随后删除已有目标目录再 move。名称由组件约定，代码并不强制“不同摘要不同目录”。

这会让两类大对象拥有不同保证：传输中损坏仍可能带着正确标记；同路径换模型还可能影响活跃版本或可回滚版本。是否实际换过同一路径需另查发布记录，这里指出的是机制允许的风险。

**方案：** 将 component 模型也接入已有内容寻址制品通道。摘要和文件清单随本次 exact-commit descriptor 封装；目标重哈希后原子发布到摘要目录；release 引用目录，GC 只回收没有 active/candidate/rollback 引用的对象。不要覆盖仍被引用的模型，尤其不能替已有 memory 数据悄悄更换 encoder。

**验收：** 保留摘要标记但改坏任一文件，必须拒绝；下载或发布中断时旧模型仍可读；新旧模型并存时可恢复旧版本。

证据：`component_artifacts.py:123`、`:139`、`:160`；`hostagent/staging.py:291`、`:330`、`:350`。

## 3. 哪些地方属于过度设计，如何减掉

| 设计 | 判断 | 调整 |
| --- | --- | --- |
| 多份手写拓扑 + 大量 parity tests | 最明确的重复设计。相同 unit/capability 在组件 TOML、Ops config、agent、release manifest 和 Host 配置中重复出现 | 组件契约 + 受评审平台 profile 生成唯一部署描述；目标保留独立路径/权限/schema 校验，不再手写同一完整清单 |
| `HostAdapter` 的三个 ports | 差异真实，保留接口；但当前 systemd/packages 多数只是转发到同一 `EidolonPiController`，尚非自由组合平台 | 接口按生命周期、基础环境、release 各自承担能力；改名为 product/release controller，逐步拆出 authority recovery，停止增加空转发层 |
| 手写 YAML 标量编辑器 | 为避免工作站运行时多一个 YAML 依赖，维护了 263 行特殊语法，收益有限 | 在工作站用标准 YAML 解析/序列化，生成独立配置；需保留注释才选 round-trip 方式。目标仍可保持 stdlib-only |
| Ops 自带 TLS relay | 77 行本身不重，但它是常驻数据路径，越出了“部署工具”边界 | 将当前 relay 先迁入正式 runtime/ingress 组件并版本化；Ops 管端口、证书和安装。是否替换实现单独评估，不在这次重构中顺便引入代理平台 |
| Owner Domain PKI、generation、权威恢复 | 密钥留在工作站可以合理；复杂权威规则长期放在部署控制器中会放大升级耦合 | 第一阶段抽成职责明确的 authority 模块，Ops 调用；其协议/状态规则由产品身份权威维护，不能简单删除或把私钥搬进 Host |
| CLI 与 Console 的操作声明 | 共用 Controller 是正确的，但 argparse、OPERATIONS、CATALOG、plans 仍重复参数/能力/调用信息 | 提取很小的 OperationSpec，CLI 和 Console 各渲染一次；保留 UI 标签差异，不做通用工作流语言 |
| 为防漂移而增加的各种拒绝 | 有些保护安全不变量，有些只在拒绝过时的派生副本 | secret、权限、路径、身份、摘要不一致继续拒绝；可重建 settings/unit/端口配置则比较后收敛，而不是要求人工同步多份文件 |

“目标独立校验”与“手抄第二份数据”不等价：当前 agent 的代码与 payload 都由同一个工作站经 SSH 下发，重复常量主要增加一致性约束，并没有额外建立独立的信任根。可以共享经过版本化的声明，同时在目标上独立检查路径边界、允许的执行原语、文件摘要和签名/可信来源。

另外，foundation 是否使用 Python 或 TOML 不是核心问题。受 Git 管理、受 schema 校验、与发布版本绑定且必须评审的 TOML，同样可以是受控平台清单；不要把文件格式当成供应链评审边界。当前 Python profile 可以继续使用，优先消除 workstation/agent 的重复数据。

证据：`config.py:39`、`:49`、`:70`、`:553`，`hostagent/contract.py:18`，`release_matrix.py:29`，Kernel 仓 `eidolon_deploy/capabilities.py`、`manifest.py`；`host.py:38`、`adapters/systemd.py`、`adapters/packages.py`；`settings_overlay.py`；`lan_ingress.py`、`host_application.py:202`；`owner_domain_assets.py`、`controller.py:937`；`host_cli.py:51`、`console/catalog.py:71`。

## 4. 面向新装与增量部署的目标流程

建议保留现有公开命令，通过内部收敛完成演进，不先要求用户学习一套新命令。

```text
Host 配置 + 一次性解析的源码 commit + 私密输入引用
                       ↓
              生成/封装部署描述
                       ↓
             读取 Host 当前状态并比较
                       ↓
        输出实际新增、变化、删除和恢复范围
                       ↓
    新装：基础环境 → 服务身份/目录 → 首次状态初始化
    更新：必要的基础环境变化 → 仅补齐缺失依赖与制品
                       ↓
          一个目标事务切换代码与生成配置
                       ↓
          服务/发布门禁 → App 状态观测 → 收据
```

### 配置组织

把当前一个 Host 需要维护的两份配置收敛为“平台默认值 + 单 Host 差异”，不是增加复杂继承树。工作站工具路径与 Host 事实分开；FHS 默认路径和自动派生的 service 清单不要求每台机器抄一遍。提供 resolved/show 输出，让实际生效值仍能评审。

部署描述在现有 manifest/descriptor 体系内做版本化演进，至少封装：源码 commit、component contract digest、平台/架构/ABI、启用组件与依赖、制品摘要、生成配置摘要、相关 unit、状态兼容约束及健康要求。现有 schema 拒绝未知字段，不能在旧版本号下直接加字段或改变 artifact kind/数量。私密值不写入该描述；完整 secret 摘要也不作为公开证据，尤其不能暴露低熵配对码的可枚举哈希。

硬件是否存在与产品选择用不用本地 ASR/TTS/LLM 是两个问题。短期保留 `rknpu2/local_asr/local_tts/local_llm` 命名，但明确 hardware observed 与 enabled feature 的含义，并校验它们的依赖。无需立刻设计复杂硬件能力体系。

### 新装

- 提供一次聚合 preflight，把缺少的 OS/驱动/包/输入/磁盘条件一起报告，避免每次只修完一个问题才看到下一个。
- 依据 active/candidate/首次安装 journal 判断当前状态，同一输入的 install 重试复验后继续或返回已完成。
- 重装的破坏性操作尽可能后置。当前 `install --reset-existing` 在 `bundles.prepare()` 前就 wipe：至少先完成工作站封装、模型和依赖准备、目标容量与基础条件检查，再删除旧业务数据。完整 target prepare 能否前移需结合旧 namespace 和容量决定，不能假称本地 preflight 已证明可安装。
- 将 reboot 后服务自启动、网络恢复、BLE/板级模型加载列入每种产品 Host 的首次资格验证；这些不是 Mac 单元测试能够证明的事实。

### 增量部署

当前的“增量”主要是 **传输层增量**：缓存和 CAS 已减少重复下载，但 prepare 仍为每个 release 建立各组件环境，activator 按完整 affected_units 停启服务。这是保守可靠的基线，不等于已经做到组件级增量。

按收益和风险依次优化：

1. **无变化就不发布。** 比较当前 release 的代码、生成配置、制品和平台摘要，并确认目标实际状态、身份绑定、文件权限、服务健康和事务完成状态；全部满足才返回 up-to-date，不新建环境、不停服务。同 commit 但配置漂移、服务损坏或有未完成事务时不能报告成功。需要强制重部署时再显式指定。
2. **只变配置就只处理配置影响面。** 同样受事务和健康门禁保护，先实现明确的 component 配置变更，不推导任意隐式依赖。
3. **复用不可变构建结果。** 缓存键包含源码/共享 SDK、lock、工具链、OS/架构/ABI、构建选项；目标继续独立验证。不要直接复制含绝对路径 shebang 的旧 venv，不共享可变环境。
4. **再做最小停启集合。** 根据可靠依赖图计算变更组件及必须重启的消费者。涉及 SDK、协议、数据解释、基础服务或依赖不明时退回整组切换。保留整机 release manifest 作为一致组合，不引入任意混搭版本。

不承诺“零停机”：当前依赖图和权威组件的行为不足以支持这种保证。

## 5. 优先级有运行记录支撑

读取本地 `config/hosts/runs/{pi5,rk3588}.jsonl`，仅统计 `deploy/update` 且 `outcome=applied` 的记录：

| 样本 | 成功更新数 | 总耗时中位数 | activate 中位数 | prepare 中位数 | upload_finalize 中位数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Pi5，2026-08-26 至 09-04（UTC） | 58 | 220.1s | 106.5s（58 次） | 67.2s（42 次） | 0.8s（42 次） |
| RK3588，2026-09-05 至 09-09（UTC） | 20 | 337.2s | 105.2s（20 次） | 87.4s（18 次） | 13.7s（18 次） |

这些记录跨历史版本，包含 resume；阶段中位数不能相加，也不是当前提交的受控性能测试。activate 含启停和门禁等待，不等于业务停机时长；upload_finalize 不包含全部模型/CAS 传输。它们支持的结论是：**日常优化优先看 prepare、activate 和 Host 配置处理，不要先重做网络传输。**

失败记录同时出现 dirty worktree、无法确认有线 endpoint、目标 prepare、跨版本配置、激活和业务门禁失败。它们不能全部归因于 Ops 过度设计，更不能通过放宽所有门禁“提高成功率”。

现有 RunLedger 值得保留，只需补强：

- 统一记录结构化 `error_code`、失败阶段和可执行 remedy；当前将错误字符串归一化截断到 96 字符，容易把不同 gate 归为一类。
- 失败时也保留 release_id、selected commits、link、已产生的目标事务 id；当前这些 report 字段主要在成功返回后记录。
- Console 与 CLI 使用同一事件记录机制；目标 journal 是恢复依据，工作站 ledger 是统计依据，职责不混淆。
- 后续用同一组真实 Host、明确变更类型测冷安装、热更新、no-op 和故障恢复；暂不设没有基准支持的耗时承诺。

## 6. 实施顺序与完成标准

| 阶段 | 工作 | 完成标准 |
| --- | --- | --- |
| P0：发布正确性 | exact-commit 契约；模型端到端校验和不可变存储；统一代码/配置恢复；公共目标 mutation lock | 重现旧版本无工作树混入；损坏模型被拒绝；显式与自动恢复完整；并发/断线故障测试通过 |
| P1：减少维护面 | 生成拓扑取代多份手写表；平台默认值 + Host 差异；小型 OperationSpec；工作站标准 YAML 处理 | 新 Host 主要增加 profile；新增组件从组件契约导出 unit/端口/制品；无需再同步多张同义表 |
| P2：改善部署效率 | no-op 判定；配置变化识别；不可变构建缓存；证据允许后再最小停启 | 无变化部署不 build/停服；复用不破坏摘要和回滚；真实 Host 冷/热部署耗时可比较 |
| P3：清晰长期边界 | authority 恢复模块隔离；TLS relay 进入正式 runtime；剥离 release executor 对 Kernel 仓布局的开发依赖 | Ops 负责部署编排，运行时和业务权威自有版本/测试；旧 release 的恢复入口继续可用 |

P0 不必等 P1 的大范围拓扑收敛完成。优先做几个小而可验收的修复；不要一开始迁仓、重写 CLI、换 supervisor 或加中央控制面。把 `eidolon_deploy` 从 Kernel 仓移出可以是最终整理，先收敛接口和事务，再迁位置，避免只移动文件但保留相同耦合。

## 7. 验证与文档维护

本次先运行 capability 拓扑、组件制品、cutover、HostController、settings overlay、CLI 分派和 foundation parity 的 154 项测试，全部通过。随后运行全量测试：998 项通过，另外 37 项在 Unix socket bind 处被沙箱权限阻挡（1 failed、36 setup errors）；放开该环境限制后单独复跑，37 项全部通过。因此全套 1,035 项均已验证通过，但不是一次无环境限制的整套运行。没有运行真实硬件部署或故障注入。

当前文档存在实质漂移：README 仍以“15 个产品 unit”为主，而 `config.PRODUCT_UNITS` 已有 19 个条目（其中包含 socket），capability 还会增加 unit；runbook 的固定计数及回滚说明没有完整体现当前 forward-only 模式。更正初评的一处措辞：`database_migrations=[]` 仍是 descriptor V2 的真实约束，不能当作过时要求删除；forward-only 是启动后的持久状态屏障，不代表 release 工具已支持执行迁移列表。已有 architecture-audit/verification 包含历史现场结论，不能作为当前版本就绪证明。

建议 README 只写职责、前提、常用命令和生成的支持矩阵；具体拓扑和参数从契约/OperationSpec 生成。旧事故和取舍集中进 ADR/历史验证报告，源码注释保留当前不变量与必要缘由，减少长篇历史叙事掩盖实际行为。

回归重点应从“几张副本相等”逐步转向：解析后的部署描述正确、旧 revision 可重现、能力增删收敛、未变组件不受扰动、失败后代码与配置一致、并发和断线有确定结果。parity tests 在删除相应手写副本后再移除，不先削弱现有保护。

## 8. 二次复审：会不会破坏现有功能

**结论：原计划存在会破坏现有功能的实施方式，不能直接整体执行。方向保留，实施改为先证明等价、再小步替换。** 当前仅改文档，因此现有部署功能尚未因本计划改变。初评的 1,035 项通过是旧实现基线，不是未来重构的安全证明。

### 8.1 必须收紧的兼容条件

| 计划项 | 可能破坏什么 | 修订后的要求 |
| --- | --- | --- |
| 合并 Host/operations 配置 | Mac 与 Pi 共用 operations config；改公共默认值可能串到另一台 Host；移动文件改变相对路径含义 | 第一阶段保留文件格式、命令别名与入口，内部归一化；相对路径始终按原文件目录解析。Mac、Pi、OPi 分别比较最终配置，默认值升级不得默默改变已有 Host |
| 从契约生成全部拓扑 | `systemd_units` 的枚举不等于发布启停顺序；可能漏 socket、reconciler、权限、非 unit 资产和就绪检查 | 新生成器先只读计算，与旧结果比较。比较有序启停图、socket/service 配对、文件来源/模式、用户/组、模型、端口、CPU 配置和所有门禁，不能只比较 unit 集合。契约尚未表达的部分继续由受版本控制的旧规则提供 |
| exact-commit 契约与新 descriptor | 旧 schema `additionalProperties=false`；bundle 校验还限制 artifact 数量和 kind；旧 release 没有完整组件契约 | 显式识别格式/执行能力版本，先增加读取兼容，再启用新写入。旧 release 继续使用它的 descriptor/activator；新能力不受支持时在 live 变更前拒绝。缺契约绝不解释成零服务或零状态 |
| 标准 YAML 替换文本 overlay | 现有 `value` 是原样写入的 YAML 文本；把 `"false"` 当字符串写入可能变成字符串 false，或改变引号、列表、重复键语义；重新序列化还会改变摘要 | 保持“组件模板 → 产品 overlay → Host overlay”；按现有标量语义解析 value，拒绝重复键/歧义；使用组件真实配置模型比较最终类型和值。旧 bundle/resume 保留旧渲染器和字节摘要，不原地重算 |
| 统一切换事务和锁 | 外层和旧 activator 双重获取同一锁会失败或死锁；掉线后可能只恢复半套配置；forward-only 不能撤回状态 | 由一个目标协调器持锁，内部调用明确不重复持锁的原语；约定统一锁顺序，不移除仍在用的锁文件。旧协议走旧兼容路径，不把旧 CLI 强行包在同一 flock 内。所有恢复先恢复匹配代码/配置，再启动和复验；不可逆屏障保持 |
| 模型迁入 CAS | unit、launcher、settings 仍引用旧固定路径；旧 release 和 Memory realm 可能继续依赖旧 encoder | 先补摘要验证与原子安装，暂不改变消费者路径；再支持版本化引用并兼容旧引用。GC 必须保护 active/candidate/可回滚 release，以及持久 Memory realm 和保留备份的 encoder 引用；无法证明无引用时不自动删除 |
| 配置自动收敛 | 误覆盖现场调优 drop-in、CPU 参数；把 private input 当普通模板重建导致鉴权或认领失效 | 只收敛明确归 Ops 管理且有来源记录的文件；未知现场覆盖先报告。普通升级保持 identity、TLS pin、Owner generation、setup code、内部 token 及业务状态；有意轮换另走对应生命周期 |
| 本地/云端切换 | 通过 provider 推导 capabilities 会卸载本地服务；统一 LLM 字段会误改 Memory 抽取、embedding 或 Channel 路径 | 保留安装能力与调用路由的独立性。当前 OPi“云端路由 + 本地模型服务仍保留”必须原样可表达；Agent、Channel、Memory LLM、Memory embedding 分别管理。重构不顺便改变模型产品方案 |
| no-op、配置专用更新和最小停启 | 只看 commit 会漏配置/权限漂移；少重启消费者导致旧连接或旧缓存继续运行；不同组件混用 SDK/协议 | no-op 必须核实实际状态和健康；有未完成事务先恢复；配置变更也做依赖闭包与门禁。首轮仍整组激活，仅在依赖完整且经过验证后允许局部重启 |
| 迁出 TLS/authority/release executor | 破坏已认领设备的证书固定、旧 CLI 路径、恢复入口和私密材料寻址 | 本轮暂缓迁仓和运行路径变更，最多先做行为不变的模块整理；不得随整理换 Host identity、证书、端口或 Owner 状态 |

补充代码证据：`paths.py:225` 对路径和旧格式的严格解析；`component_contract.py:278` 的 unit 枚举；Kernel `eidolon_deploy/manifest.py:583` 的有序拓扑/门禁校验；`release_bundle.py:605` 的固定制品形状；`tests/test_settings_overlay.py:123` 的 value 原样写入契约；Kernel `eidolon_deploy/activation.py` 的锁与 forward-only rollback 禁止逻辑。

特别修订：当前“旧与新解释器都读懂 settings”的跨版本门禁是两段切换的保护。**在代码/配置已经由同一目标事务完整切换并可完整恢复前，不删除该门禁。** 新事务路径可采用候选配置由候选解释器校验、快照配置由原解释器校验；旧发布仍保留原行为。

### 8.2 需要先建立的功能基线

| 场景 | 必须证明的行为 |
| --- | --- |
| Mac source-run | start/stop/restart/status、实际对话、生成路径、外部基础服务保持；不被 Pi 的配置归一化牵连 |
| Pi5 新装与增量 | 首次初始化成功；旧数据保留更新；相同输入中断后 resume；重装 wipe 仍需明确参数；重启后服务和网络恢复 |
| OPi 当前模式 | 云端 ASR/TTS/LLM 路由保留，本地模型 capability、权重、CPU 分配不被自动删除或修改 |
| OPi 本地/混合模式 | 既有本地 ASR/TTS/LLM 可运行；本地语音+云 LLM、本地 LLM+云语音分别验证路由和凭据；备份作为测试输入时先解析正确的 operations config，不能使用那个仍指向正式文件的备份 profile 冒充本地基线 |
| 手机与设备 | 已认领手机/ESP 无需重配对，TLS pin、Host ID、Owner/Claim 保持；真实语音输入→对话→播音、插话、设备管理通过；app-ready 不能代替这些测试 |
| Memory | 既有 realm/encoder 不变；事实写入、抽取、召回正常；模型文件不能只按活跃 release 引用做 GC |
| 发布兼容 | 新 Ops 读取已保存旧 descriptor/收据；旧 candidate resume；显式 revision 重现；HEAD 已变化时保持原拒绝与指引；新字段不会强发给旧执行器 |
| 故障恢复 | 目标配置写入、代码切换、启动、门禁、收据提交处分别注入失败；断网/工作站退出、目标重启、并发 CLI 后有明确结果；reversible 恢复完整且可服务，forward-only 不重启旧解释器 |
| 诊断及备份 | CLI/Console 命令、JSON/退出码、确认条件兼容；backup/restore、authority 恢复、rollback 各自范围不混淆；坏模型/坏快照/磁盘不足在规定边界拒绝 |

身份、secret 和数据基线只在受保护的验证环境比较，不把私密值或低熵 secret 的摘要写入公开测试报告。真实 Host 验证安排在明确的部署任务里；本次 review 不执行它。

### 8.3 修订后的推进顺序

1. **只读基线与影子计算。** 保存受保护的完整配置/版本清单，识别现场覆盖；新配置解析和拓扑生成只计算差异，不改旧 writer，不生成新身份、不下发。
2. **小范围正确性修复。** exact-commit 契约、目标模型内容校验、备份 inventory 隔离分别交付；保留旧格式和旧模型落点。移动备份时处理相对路径及 Host ID 重复，不能只改文件名。
3. **事务专项。** 独立完成协议版本、锁所有权、可恢复 journal 和配置/代码统一恢复；通过故障注入和一台产品 Host 的维护窗口验证，再推广另一平台。它属于高风险改造，不与配置格式迁移同批发布。
4. **逐项替换重复实现。** 先切换已证明等价的拓扑/配置生成；仍保持既有服务范围、模型路由和整组激活。语义等价但字节不同的 YAML 必须作为一次新的配置发布，不修改旧 bundle。
5. **性能和职责整理后置。** no-op 首先仅报告“本可跳过”的证据，验证无漏检后启用；缓存、局部重启逐项推进；TLS/authority 迁移另做兼容性评审。

只有相关差异比较、历史格式测试、故障恢复测试和真实功能验收通过，才可说“该项改造未观察到功能回归”。不能用“重构理论上不改行为”或现有单元测试全绿保证整个计划不会破坏功能。

## 9. 最新修改复审与计划更新（e77dd28 + 当前工作树）

### 9.1 对照原计划的完成情况

结论：这批修改主要改善了首次起机和运行状态判断，方向合理；原计划中的发布一致性问题尚未解决。不能按新增文件数把它们判成过度设计，也不能把这些改进等同于统一部署事务已经完成。

| 议题 | 最新事实 | 计划调整 |
| --- | --- | --- |
| 首次起机 | `bring_up.py` 用一个 BringUp 声明表达 hostname、账号与 operator keys；按 foundation 渲染 boot-medium / shell，稳定 instance-id，未知平台拒绝 | 将原先“SSH-ready 完全由外部提供”改为已部分覆盖；保留小型平台渲染表，不另建刷机框架 |
| 多操作者与 Host 信任 | Pi 公钥集合入库，私钥仍在各工作站；Pi 有独立 known_hosts 和显式换 key 操作 | 合理拆分两类身份，保留；补 known_hosts 格式兼容及其他 Host 的支持矩阵 |
| 私密输入读取 | `setup_code_file` 改为使用时读取，缺少配对码不再阻挡无关读取操作；SSH 材料错误提示 trust-host-key | 此问题已经改善，不再安排一轮同义“配置惰性加载重构” |
| 有线传输 | resolver 没给出有线候选时，通过已认证 SSH 问 Host 地址，再复用排序与连接验证；有线发布门禁仍保留 | 保留，不重写传输层；OPi 当前仍固定 `10.42.0.2`，不要宣称所有 Host 已统一为 mDNS |
| LiveKit 网络职责 | Mac wrapper 不再自动固化观测 IP 为 node_ip；默认原生 ICE，显式 override 保留；Kernel eidolond 负责网络变化协调，Ops 读取 network_current | 已部分完成“运行时协调由运行时拥有”；TLS relay 和 authority 的职责问题仍独立存在 |
| 发布与 App 状态边界（纠正原评审） | deploy 的 `_observe_app_readiness` 不把 degraded 或探测异常转换为回滚；install 也不因 App 返回 degraded 拒绝。该行为在 `7860986` 已存在 | 原计划不能借“统一健康门禁”重新把整个 App-ready 作为发布成功条件；发布健康、App 观测、authority 恢复验收分别表达 |
| LAN 观测（未提交） | 无默认出网路由时可选本机私网接口，过滤 loopback/link-local 等 | 解决隔离 LAN 的单一接口场景；多网卡/VPN/虚拟网桥选择仍需修正，见 R3 |
| commissioning | 删除 Ops `--ttl-seconds`，无期限输出 JSON null，对齐当前 Admin 协议 | 接受产品语义调整，但它是明确的 CLI 变更；记录旧自动化调用迁移，不再承诺所有旧参数原样兼容 |
| exact-commit 契约 | `_carry_component_artifacts`、capability 端口和 reset 权威范围仍从工作树读契约 | 原 P0-A 保留，未完成 |
| 发布恢复与锁 | release_transaction 的此次变动只有有线探测；代码与 Host 配置仍分两段恢复，显式 rollback 仍只调 release CLI | 原 P0-B 保留，未完成；恢复条件以发布 doctor 等真实失败为准，不能归因于 App-ready degraded |
| 模型校验 | target staging 仍只读 `.files-sha256`，安装先删除已有目标再 move | 原 P0-C 保留，未完成；先验证与原子安装，再谈 CAS 路径迁移 |
| 配置与备份 | 双 TOML、备份 profile 引用正式配置、备份混入活动 inventory 均仍存在 | 保留内部归一化及备份隔离；不与事务改造同批迁格式 |
| OperationSpec / YAML / 性能 | 尚未统一操作声明，也未实现基于实际状态的 no-op；YAML 测试已使用真实解析器 | 小型 OperationSpec 后置；不再把换 YAML 库本身当必做目标，优先语义验证；性能阶段继续后置 |

### 9.2 本次新增的可操作评审发现

以下 P1/P2 是本次代码问题严重度；原文 P0/P1/P2 是项目实施阶段，两者不要混用。

**R1 · P1：新增 LiveKit 就绪事实与旧运行时不兼容，会阻挡 authority 恢复的完成判断。**

[目标 probe.py:534](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/hostagent/probe.py:534) 无条件要求 `service_id=livekit`、`runtime_state=ready`、`network_current=true`；Mac 同样如此。Kernel `41ed793` 之前的 ServiceStatusWire 没有最后一个字段；新 Kernel 也需要服务 manifest 声明 `restart_on_network_change` 才会报告该事实。新 Ops 的 injected agent 来自当前工作站，不随旧 candidate 回到旧版本。

影响必须按调用方区分：新 Ops 检查旧 Kernel/manifest 时 App-ready 会降级；[release_transaction.py:545](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/release_transaction.py:545) 在 doctor 成功后仅记录该观测，**普通 deploy 不会因此失败或回滚**，包括 forward-only。初评和本次中途将它描述成发布后强制门禁是不准确的。

但 [controller.py:1725](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/controller.py:1725) 的 authority-restore 在执行目标恢复后，调用 `_wait_for_restored_authority_readiness` 强制等待 App-ready。若活跃的旧 Kernel 不会报告该字段，等待再久也不可能通过，导致已经执行恢复却无法完成成功验收。新 Kernel 与旧 manifest 混搭也可能落入该情况。

修订方案：明确 readiness 协议能力；对于 authority 恢复等强制依赖该事实的操作，在修改目标状态前检查兼容条件，给出先升级的路径，或提供经过验证的版本适配。历史诊断区分“旧实现无法证明新事实”与“已观察到网络失效”，不能把缺失字段当 true。普通 deploy 保持观测语义，不额外引入 App-ready 拒绝条件；验证旧、新、混合 manifest，覆盖恢复拒绝时点，并保留已有的 degraded/unreachable App 不导致 deploy 回滚测试。

**R2 · P1：Host key 读写没有覆盖 OpenSSH 已支持的 known_hosts 表达。**

[host_keys.py:127](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/host_keys.py:127) 仅按明文逗号列表识别 alias；[write:169](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/host_keys.py:169) 使用相同判断。临时文件复现：OpenSSH `ssh-keygen -F` 能找到哈希 Host 条目，Ops 返回空；之后写入保留了哈希旧条目并新增明文条目。若扫描到不同 key，controller 会走首次信任分支，不要求 `--replace`，且旧 key 未撤销。OPi 当前仍使用工作站 `~/.ssh/known_hosts`，因此不能只按 Pi 新建空文件推断兼容性；未读取或修改用户实际 known_hosts。

另一个复现：`host-a,host-b key` 更新 host-a 时，host-b 同行条目也被删除，违反保留其他 alias 的约定。

修订方案：利用 OpenSSH 的查找能力或完整支持已声明格式；遇到未支持的哈希/marker/wildcard 等不能当作“无旧信任”。更新精确保留其他 Host，只替换已确认目标；先对临时副本完成检查，再原子提交。覆盖哈希旧 key、同一 key、不同 key、共享行及独立文件迁移。保留 fingerprint 核对和 StrictHostKeyChecking，不静默接受换板子。

**R3 · P2：未提交的 LAN fallback 将“数字最小的地址”当成设备接入网。**

[目标 app_contract.py:26](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/hostagent/app_contract.py:26) 与 [Mac lan_observation.py:29](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/lan_observation.py:29) 丢失了接口归属。无默认路由、同时有虚拟网桥/VPN `10.0.0.1` 和设备 LAN `192.168.1.37` 时，两端均选择前者，已用 fake runner 复现。这只证明选择结果，未声称现场存在该网卡。地址属于本机并不能证明手机能走这条链路；它会进入 App 合约、客户端地址或就绪检查，可能误判或阻挡可用 LAN。

修订方案：保留“隔离 LAN 不必有默认路由”，观测结果携带接口类型、状态与网络归属，区分部署传输地址和设备接入候选。候选不唯一时报告歧义或消费已有显式配置，不用 IP 排序替代网络策略。验证单隔离 LAN、多网卡、VPN、网桥、仅有线 link-local、显式地址及地址变化。link-local 用于部署传输，不自动当成产品已具备 App-ready。

**R4 · P2：bring-up shell 的结尾没有可靠验证它宣称的起机条件。**

[bring_up.py:416](/Users/manson/ai/eidolon/eidolon_ops/src/eidolon_ops/bring_up.py:416) 吞掉 `nmcli con up` 的失败；随后 `systemctl is-active ... | tr ...` 在 POSIX sh 中以 tr 的成功覆盖检查失败，最终 `ip addr show` 无地址也可成功。`sudo -n -u "$USER" true` 是 root 切换到目标用户，不是目标用户执行 Ops 所需的无交互 root 提权。因此有可能脚本 exit 0、打印 non-interactive，但实际网络或提权条件尚未成立。

修订方案：将网络切换中可能掉线与真正失败分开；返回“已写入、待重连验证”或执行精确 postcheck，不输出未经证明的就绪。sudo 检查从目标账号提权。使用 fake commands 验证生成脚本的结果及退出码，不能仅做 `sh -n` 和字符串包含检查。现有 `test_the_script_cannot_lock_the_operator_out_of_the_board` 仍叙述 append-only，代码已改成声明集合覆盖；其中 `">> "` 可以匹配 `/etc/hosts` 而通过，应改为验证实际公钥集合增删。保留撤销操作者的能力，不为让旧测试通过而改回只追加。

### 9.3 配置机制的覆盖范围

- Pi：已声明 operator 公钥文件、专属 known_hosts，支持两种 bring-up 交付；known_hosts 改路径后，已有工作站可能需运行信任建立流程，这是预期迁移步骤，不能声称完全零操作升级。
- OPi：仍是固定 SSH 地址 `10.42.0.2`、共享 known_hosts，未声明 operator_keys_file；其 Ubuntu foundation 不在 bring-up 平台表中。直接调用 bring-up 会拒绝，不能拿 Pi 的输出套用。
- 修改 `.operators` 后，普通 deploy/update 不会自动修改板子的 authorized_keys；需要重新生成并执行 shell 交付。撤销在 Git 中提交，不等于已在 Host 生效。
- OPi 正式云端路由、本地模型 capabilities、CPU 资产及两份备份的机制未因本批修改改变；完整流程见[更新后的配置清单](/Users/manson/ai/eidolon/eidolon_ops/docs/host-configuration-map-2026-09-10.md)。

### 9.4 更新后的执行顺序与退出条件

| 次序 | 交付范围 | 完成标准 |
| --- | --- | --- |
| 1：补新机制边界 | R1 authority 恢复的 readiness 兼容 preflight、R2 SSH 信任读写；随后 R3 LAN 候选与 R4 shell 证据分别修复 | 恢复旧运行时的已知不兼容在改状态前处理；普通 deploy 保持 App 观测语义；既有信任不会误判/误删；多网卡与失败退出有行为测试 |
| 2：小步修发布正确性 | exact-commit 契约、目标模型内容验证、备份 inventory 隔离；重装 wipe 前移除可提前发现的失败条件 | 保持旧格式/模型落点；工作树 B 发布 A 可重现；坏模型不会破坏旧安装；备份确实恢复所声明路由 |
| 3：事务专项 | 代码/配置一致切换恢复、协议版本、目标锁与 durable journal | 自动/显式恢复一致；断线、并发、失败、forward-only 屏障验证；Pi 和 OPi 分别维护窗口验收 |
| 4：减少重复声明 | 内部 resolved 配置、影子拓扑、有边界的 OperationSpec；补 OPi 专属 bring-up 平台和操作者声明 | 生成结果与旧行为等价；每个平台由自身证据支持；起机支持矩阵明确，CLI-only 操作明确标注 |
| 5：性能与长期整理 | 先观测 no-op，再缓存/局部重启；TLS/authority 边界另评 | 不放松健康门禁，不改变身份和状态解释；真实冷/热部署数据支持收益 |

标准 YAML 解析器不再是第 4 阶段的硬性交付。现有 scalar overlay 保留模板字节有实际价值，测试也已解析最终类型；优先补不支持语法的明确拒绝、重复键/歧义与组件配置模型验证。只有出现无法满足的语义需求，再在工作站引入标准实现并按新发布处理字节变化。Ops 仍不需要任意配置继承树、工作流语言或中央控制面。

### 9.5 本次验证边界

- 全量测试：1,032 passed；另外 3 failed、42 setup errors 均为 Unix socket bind 被沙箱拒绝。允许本地 socket 后仅复跑这些 45 项，45 passed。合计 **1,077 项在两轮运行中通过**，不是一次完整的非沙箱全量运行。
- 另外以临时文件复现 known_hosts 哈希漏识别、共享行 alias 丢失；以 fake runner 复现 LAN fallback 的选择问题。未修改真实 SSH 信任、用户配置或目标状态。
- R1 基于 authority-restore 调用顺序、旧 Kernel wire 与新 manifest/服务状态实现交叉核对，且确认普通 deploy 的观测语义及对应测试；R4 基于生成脚本语义与现有测试检查。没有实际刷机、SSH 部署、模拟目标掉电或验证手机语音通路。
- 既有测试全绿说明当前测试覆盖下未观察到回归；它没有覆盖上述所有边界。README 仍有固定“15 个 unit”和部分过期注释，生成支持矩阵时一并清理；历史现场叙述不等同于本次实测。

## 10. 用户授权实施后的状态

本轮范围明确为 R1–R4 和部署链路，再做 Pi5 真机验证；原计划的统一事务、exact-commit 契约、模型存储与增量构建没有借此一并改造。

| 项目 | 实施状态 |
| --- | --- |
| R1 | authority-restore 的 plan/apply 在目标状态变更之前调用只读 readiness-compatibility；旧运行时不能报告网络事实时提前拒绝。真机旧 Pi 返回 unsupported，符合预期。普通 deploy 的 App 观测语义保留 |
| R2 | OpenSSH 查找哈希与明文条目；共享行保留其他 Host；匹配特殊信任策略时拒绝自动修改；已通过换板确认及本地行为测试 |
| R3 | 无默认路由只允许唯一候选，多个候选不按 IP 排序选择；显式 app.lan_ipv4 继续可用。真机当前识别 Wi-Fi LAN `192.168.100.15`，与 USB 传输端点分开 |
| R4 | 起机脚本的激活失败、提权失败、服务失败和缺地址均返回非零；使用 fake commands 实际执行生成脚本验证，而非只检查包含哪些字符串 |
| 链路 | Pi 默认允许普通 LAN/Wi-Fi，有线可达时优先；保留显式强制有线选项，OPi 原策略不变；删除将历史带宽当作当前事实的发布提示 |

实际板子有旧产品数据，不能按空白新机清盘。现已核对身份、保存在线权威备份和完整停服状态快照，并恢复旧服务。候选升级要求 Bootstrap schema 7→9、forward-only；是否已激活及完整真机验收结果以独立验证记录为准，不能由“代码修复完成”推断“整机测试完成”。


### 10.1 真机追加发现：换板的 Authority 世代不能由 Host identity 推断

授权部署后发现板上 generation 8 与工作站 generation 9 混用，原 deploy 到 Hub 启动阶段才拒绝，已补充部署前的 authority_capability 检查并以真实换板状态验证提前拒绝。原四项修复已提交 `036f400`；此次追加修复及现场结果见[真机记录](pi5-optimization-validation-2026-09-10.md)。Host 身份、Owner root 相同，也必须进一步核对 generation 与 state_id；不允许普通更新隐式重置 Authority。


## 11. 普通更新保留授权，以及原计划第二阶段实施

用户进一步授权简化 generation 问题并继续原计划。本节取代 10.1 中把本地/目标谱系相等作为普通 deploy 前提的临时方案。

| 事项 | 当前实现 | 验证边界 |
| --- | --- | --- |
| 普通更新与授权分离 | 读取板上已建立的谱系和保留文件摘要；只生成业务配置与 ingress，安装/重置/恢复仍使用显式授权材料流程 | 工作站与目标 generation 不同仍可更新，不推进或回退任何一方；目标身份自身不一致或发生并发变化时拒绝 |
| 精确提交契约 | 制品、能力端口、reset 范围的组件契约均由 SourceResolver 从选定提交读取 | 真 Git 测试：工作树 B / pin A 读取 A；缺失契约沿用已有明确 absent/partial 规则，不能退回 B 的文件 |
| 模型内容校验 | 工作站与目标重算 manifest 中每个文件；目标新目录校验后发布，已有冲突目录拒绝自动覆盖 | 损坏内容、软链接、额外文件、旧目录冲突有行为测试；保持现有路径和记录格式，尚非模型 CAS/GC 重构 |
| 备份隔离 | OPi 两份 TOML 迁入 config/backups，Host backup 指向对应 operations backup，相对路径重算 | 解析后的配置等价，活动 inventory 不再重复发现 OPi 备份；Pi 专用 HIL profile 仍是显式独立测试入口 |
| wipe 前准备 | 显式重装先完成本地封装和模型准备，再推进 Owner 世代或清理旧安装 | 模拟封装失败时不 wipe、不改变 Owner；目标安装阶段仍可能失败，不能宣称重装已经具有完整事务保证 |
| 实际配置校验 | 修正 Hub 校验环境变量为 EIDOLON_HUB_SETTINGS_YAML，并校验渲染后的 Owner ID/generation | 用真实 Hub 解释器执行暂存配置验证；消除加载默认配置产生的假通过 |

原计划第三至第五阶段仍需独立推进：跨代码/配置的完整事务和恢复、统一目标 mutation 锁/持久日志、配置声明去重与 OPi 起机支持，以及经过冷/热测量再决定的构建缓存和局部重启。现有代码未被描述为这些事项已经完成。


本批验收：1,111 项测试通过；正式 Pi5 release `20260910-pi5-ops-final-wifi-1` 通过固定 Wi-Fi 端点部署（214.7 秒），随后完成重启恢复复验，doctor healthy、App-ready 24/24。部署和重启前后板上授权材料、工作站 Owner 文件均保持不变。手机端到端与其他平台真机覆盖仍未完成，详见[验证报告](pi5-optimization-validation-2026-09-10.md)。
