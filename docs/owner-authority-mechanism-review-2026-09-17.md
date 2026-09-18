# Owner 授权血统机制评审：为什么 install 会进入无法退出的状态（2026-09-17）

只读设计评审，未改动任何代码、配置或授权材料。起点是 Pi5 上 `install` 被
`AuthorityRecoveryRequired` 拒绝（板子第 8 代、工作站材料第 9 代）这一例，但本文讨论的是
**机制本身**，不是那一台板子的收敛动作。那一例的现场取证与收敛方案另见
[Pi5 Owner 域世代分歧](pi5-owner-generation-divergence-2026-09-16.md)。

## 结论

`owner_domain_generation` 是一个撤销纪元（revocation epoch），但 ops 把它当成工作站上的
本地可写状态，而这份状态里没有任何字段回答"这一代落在哪一台 Host 上"。于是控制器只能用
一个裸整数去和板子比大小，而这个整数既没有证据支撑，也分不清"我的缓存过期了"和
"板子被回滚了"。

2026-09-10 那次自动进代只是触发器。真正的缺陷是：**同一个事实被复制到七个地方，却没有
指定唯一的写入者**；以及**判定所需的那一个事实（这套材料交付给了哪一块板子）在模型里
根本无法表达**。

## 1. 这个计数器今天还剩多少信息量：零

`c5c3dd0`（2026-09-11）删掉世代递进分支之后：

- 唯一写 `owner-domain-state.json` 的地方是 `owner_domain_assets.py:155` 的 `_authority_state`，
  只在材料目录为空时创建，**恒定写 `owner_domain_generation: 1`**；
- `owner_domain_assets.py:196` 的 `mark_authority_bootstrapped` 只翻 `bootstrap_pending`
  这一个布尔值，从不碰计数器；
- 显式重置走 `identity_replacement.py`，换一个全新的空材料目录，于是又是 1。

**没有任何代码路径能再产生 generation ≥ 2。** 现场所有非 1 的值（8、9）都是被删掉的那套
机制留下的残骸。

再看血统三元组 `(owner_domain_id, owner_domain_generation, state_id)` 今天各自的含义：

| 字段 | 实际含义 |
| --- | --- |
| `owner_domain_id` | Owner 根公钥的哈希，**可从材料现场推导**，不需要存 |
| `owner_domain_generation` | 恒为 1（历史值除外），**零信息** |
| `state_id` | 随机串，与根密钥同生同灭（材料目录非空但缺 state 会直接报错），**与根密钥 1:1** |

也就是说，整个三元组今天等价于"哪一把 Owner 根私钥"这一件事，而这件事本来就可以用
密码学证明。它唯一没说的，恰恰是真正需要知道的那件事：**这套材料交付给了哪一块板子。**

这解释了 2026-09-10 的分叉为什么在模型里无法表达——两块板子共用同一个 Owner 根，
三元组里没有任何一个字段能区分它们。

## 2. 机制哪里不合理

### 2.1 一个事实，七份可写副本，没有唯一写入者

| # | 位置 | 侧 |
| --- | --- | --- |
| 1 | `owner-domain-state.json` 的计数器 | 工作站 |
| 2 | `owner-domain-descriptor.json`（已签名） | 工作站 |
| 3 | `/etc/eidolon/owner-domain/owner_domain_descriptor.json` | 板子 |
| 4 | `hub.generated.yaml` 的 `owner_domain_generation:` | 板子 |
| 5 | `hub_authority_state` 表 | 板子 |
| 6 | `authority-lineage.json` anchor | 板子 |
| 7 | `authority-bootstrap.json`（能力票／墓碑） | 板子 |
| + | 每台设备缓存的 descriptor，以及 `admission_claims_v1`、`hub_device_directory_v1` 每一行的 `owner_domain_generation` | 设备 |

Hub 启动时自己要做一次三方比对（`eidolon_hub/hub/adapters/persistence/database.py:373`），
ops 又做一次两方比对。副本数量本身就是故障率，而副本之间没有"谁是源"的约定，
只有"必须全等"的断言。全等断言施加在分布式状态上只有一个结局：某天不等，然后没有出路。

### 2.2 控制器持有的是"可写镜像"，不是"缓存"

`bootstrap_pending` 是典型症状：它是控制器侧的一次性布尔值，必须靠
`controller.py:1184` 的 `commit_authority_capability` 手工同步回来。代码注释里已经承认过
它导致的缺陷——只装过一次的 Host 会永远停在 `pending`，于是 `authority-backup`
拒绝每一台这样的 Host。**最容易撞到授权闸门的人群，恰好是那条恢复出路覆盖不到的人群。**

这不是实现缺陷，是"把别人的事实存成自己的可写状态"的必然结果。而板子那边其实能自证：
marker 存在即能力已消费。这个布尔值根本不需要存。

### 2.3 用"缺席"做推断

被删掉的 `ADVANCE_GENERATION` 分支的逻辑是：*bootstrap 已消费 + 板子没有 Hub 库 ⇒
这套授权状态没了 ⇒ 必须进代*。

这是**从"在某个端点观察不到"推断"在世界上不存在"**。只要"我现在连的这台"和
"我上次连的那台"可能不是同一台，这个推断就不成立，而模型里没有任何东西保证它们是同一台。
当天的 install plan 还打印 `destructive: none`，把一次"发明新撤销纪元"的操作归类成
无损更新。

**任何不是"继续既有状态"的判定，都应该在 plan 里具名出现**，而不是由操作自己声称无损。

### 2.4 拒绝文案给的两条出路，一条不存在、一条最贵

`owner_domain_assets.py:264` 的 `authority_recovery_required` 给出两条出路：

- 恢复匹配的 Authority 备份——按 2.2，这条路对只装过一次的 Host 在结构上就不可达；
- `reset --wipe-authority-data`——换 Host ID、换 Owner ID、清盘，而问题根本不在板子上。

**一个状态机能进去、却只能靠销毁用户数据出来的状态，是不完整的状态机。**

## 3. 为什么不能直接"兼容"

放宽成兼容是错的，理由有四条：

1. **不能忽略这个字段。** 它被签进 descriptor，也被签进每一张 ClaimGrant 的 AAD
   （`eidolon_sdk/device_foundation/v1/admission.py:553`），还落在
   `admission_claims_v1`（`eidolon_hub/hub/adapters/persistence/models.py:247`）每一行上。
   控制器必须选一个值去签，没有"不填"这个选项。
2. **按当前证据，向下取值和回滚攻击不可区分。** 设备侧
   `eidolon_sdk/device_foundation/v1/authority_locator.py:333` 明确拒绝 generation 回退。
   如果板子真的是被旧快照回滚了，控制器盲从板子就等于替攻击者把已撤销的授权复活。
   给控制器的证据只有一个裸整数，它没法分辨。**所以那次 fail-closed 是对的；
   错的是证据模型，不是那次拒绝。**
3. **代码把"缓存"和"源"当成对等副本来比。** 板子那边的 8 是事实（8 条设备身份、
   6 个 Claim、anchor、已装签名目录，四处自洽）；工作站那边的 9 是主张（一个没人验证过的
   整数）。用元组相等去比，等于宣称两者地位相同。
4. **最该问的问题问不出口。** "它们是不是同一台 Host？"——如果能问，8 vs 9 立刻从
   "灾难"降级成"这份材料的上一代属于另一块板子"。三元组里没有这个字段，
   所以这句话说不出来。

## 4. 更优雅的方案

先记录一个有说服力的事实：**正确的设计已经在仓库里了，而且是被用得更频繁的那个动词
在用。**

`hostagent/deployment_identity.py:22` 的 `observe()` 做的是：先读板子上装着的 authority，
要求板子自己那几份副本互相自洽，然后用它渲染一切。它从不读工作站的计数器。所以
`deploy` 在这块板子上顺顺当当跑了六天——同一块板子、同一份分叉的材料，`deploy` 通、
`install` 挡。差别只在于 `install` 私藏了一份镜像。

方案就是让 `install` 收敛到 `deploy` 的证据模型。

### A. 取消 generation 作为 ops 状态，改为从 Host 学

- 板子有 marker，就签它证明过的那一代；
- 板子是空的，就签 1（今天唯一会产生的值）；
- `owner-domain-state.json` 不再存计数器。

没有第二份副本，就没有分叉。

安全性不降低：**防回滚属性由依赖方强制执行**——设备侧 `accept()` 拒绝回退，Hub 自己
拒绝库／anchor 不一致。控制器再持第三种意见，是用一个无证据的整数做冗余防御，
收益为零，代价就是这次这个死局。

### B. 把"哪块板子"提升为唯一需要持久化的控制器侧事实

工作站真正不能从别处推导的只有一件事：这套材料交付给了谁。这条记录应该是：

```
{host_id, hardware_fingerprint, owner_domain_id, first_delivered_at}
```

- `install` = 创建这条记录（要求板子上没有 authority）；
- `deploy` = 要求记录存在且匹配。

这样 2026-09-10 那件事会在**第一道闸门**、用一句准确的话被挡住："这份材料绑定在板子
sha256:abc… 上，你现在连的是 sha256:def… —— 新开一个 profile，或走显式替换。"
没有计数器、没有清盘、没有分叉。

`host_delivery.py:14` 的 `bind_delivery` 已经是这个形状（`a6eb5a7` 正是为这件事加的），
问题是它排在授权闸门**后面**，而且开关挂在 `bootstrap_pending` 上，所以它救不了它本来
要救的那个场景。应当把它提到最前面，并让它自己决定是不是首次交付。

### C. `bootstrap_pending` 改为向板子求证

板子有 marker 即已消费。决策于是变成 `(材料, 板子观察, 交付绑定)` 的**纯函数**，
控制器侧零可变状态，`commit_authority_capability` 这类"记账不能漏"的路径整条消失。

`owner_domain_assets.py:241` 的决策表可以变成六个具名状态，每个恰好一条出路：

```
板子 marker ≠ anchor                        → HOST_INCIDENT（板子侧真事故，继续 fail-closed）
无绑定 且 板子无 marker                      → FIRST_INSTALL（bootstrap + 建绑定）
绑定匹配 且 marker 的 owner_domain 是我们的   → CONTINUE
绑定存在 但指向另一块板子                     → WRONG_BOARD（新 profile 或显式替换）
绑定匹配 但板子无 marker                      → AUTHORITY_LOST（真正的恢复场景）
marker 的 owner_domain 不是我们的             → FOREIGN_AUTHORITY
```

只有一条出路是"恢复备份"，而且没有一条能靠"缓存过期"走到。

### D. 扩展性：真要纪元的时候，让它是证据，不是计数器

以后若确实需要撤销纪元（根密钥泄露、一次性作废全部 Claim），正确形状是 Owner 根签名的
追加式链条：

```
{prev_digest, new_generation, host_id, reason, issued_at}   ← 由 Owner 根签名
```

这样：

- "控制器超前"与"板子被回滚"可以区分——看那条进代记录点名的是哪一台 Host；
- 板子和设备可以**验证**一次进代，而不是相信一个数字；
- 备份天然带上链条，恢复语义是明确的。

而且这一步现在可以完全不做：有了 A–C，generation 恒为 1，链条为空，将来接上去不需要
改任何已部署的东西。

另有一个现成的坑：协议里已经有 `trust_epoch`（在 trust anchor 和 ClaimGrant AAD 里），
ops 目前把它硬编码成 1（`owner_domain_assets.py:535` 与 `:568`）。真要做纪元时，
先决定由这两个字段中的哪一个承载，不要再加第三个计数器。

### E. 通用准则：工具能进去的状态，工具必须能出来

即使做了 A–C，这条仍然值得作为硬规则：**任何非密钥的状态分歧，都必须有一个不销毁
用户数据的收敛动词。**

本文写下时这个动词还不存在。它随后落地了，叫 `trust-host-authority`（`beadaa6`）：
按板子已建立的那一代，采纳板子正在服务的那份签名目录——只接受本 Owner 根签过的目录，
同代不接受更旧的 revision，板子两份记录不一致时拒绝采纳；state id 没有签名可依，
由操作者在板子上核对后用 `--replace` 指名。不写板子、不动 key、不作废 Claim。

值得注意的是：做了 A 之后这个动词就不需要存在了（没有东西可同步）。收敛动词是 A 的
替代品，不是它的补充——这本身就是选 A 的理由。

## 5. 落到当前这台 Pi5

- 诊断文档里的方案 C（手改工作站两个文件）正好是方案 E 的手工版。它和方案 A 在最终状态
  上收敛到同一点：工作站认板子已证明的第 8 代。
- 但方案 C 之后还有第二道闸门（`host_layer.py:304` 把 `allow_create` 接在
  `bootstrap_pending` 上，而 pi5 没有 `host_delivery.json`）。这道闸门在方案 B 里会变成
  第一道，而且它需要的不是"绕过"，而是**补录一条交付绑定**——这块板子的交付真实发生过
  （2026-08-27），只是那时还没有这个文件。

  `adopt` 语义后来有了，但它落在**第一道**闸门上：`trust-host-authority` 采纳的是世代。
  第二道闸门原样未动，而且采纳会把 `bootstrap_pending` 写成 `false`，于是 `allow_create`
  恒为 `false`——**采纳之后 `install --apply` 在这块板子上仍然跑不完。** 交付绑定这一侧
  还没有对应的补录动词。
- `factory_setup_code` 要不要走 install 是独立问题：给一台正在跑的 Host 补一个安装期
  文件，本来就不该由 `install` 负责。

## 6. 不建议做的

- **不要放宽元组比较**（例如"只比 `owner_domain_id`"）。那会把回滚检测一起丢掉，
  而且治标不治本。
- **不要在 ops 里加 generation 覆盖参数。** 它会变成每次撞墙就用一次的开关，
  等于把闸门作废。
- **不要把局部 `backup` 改名成整机恢复。**
  `authorization-recovery-simplification-2026-09-11.md` 已写明这条边界，值得保持。

## 8. 后记（2026-09-18）

§4 的 A、B、C 已实施：工作站不再持有 generation / state id / `bootstrap_pending`，`install`
先观察板子再渲染；交付绑定成为第一道闸门，且在板子证明建立之后才写；判定是一个纯函数，
八个具名情形。`trust-host-authority` 随 A 一起退役——正如 §4E 预告的那样。D（签名的纪元链）
没有做，也不需要做：generation 对新 Host 恒为 1。实施记录与迁移说明见
[Owner 授权单一账本](owner-authority-single-ledger-2026-09-18.md)。

## 7. 引用位置

| 事实 | 位置 |
| --- | --- |
| 计数器唯一写入点，恒为 1 | `src/eidolon_ops/owner_domain_assets.py:155` |
| 只翻 `bootstrap_pending`，不碰计数器 | `src/eidolon_ops/owner_domain_assets.py:196` |
| 当前决策表 | `src/eidolon_ops/owner_domain_assets.py:241` |
| 拒绝文案与两条出路 | `src/eidolon_ops/owner_domain_assets.py:264` |
| `trust_epoch` 硬编码为 1 | `src/eidolon_ops/owner_domain_assets.py:535`、`:568` |
| install 侧闸门 | `src/eidolon_ops/controller.py:1126` |
| 消费记账（可写镜像的同步点） | `src/eidolon_ops/controller.py:1184` |
| 硬件交付绑定 | `src/eidolon_ops/host_delivery.py:14` |
| deploy 的证据模型（正确形状） | `src/eidolon_ops/hostagent/deployment_identity.py:22` |
| 板子侧只读观察 | `src/eidolon_ops/hostagent/authority_state.py` |
| 设备拒绝 generation 回退 | `eidolon_sdk/eidolon_sdk/device_foundation/v1/authority_locator.py:333` |
| generation 签进 ClaimGrant AAD | `eidolon_sdk/eidolon_sdk/device_foundation/v1/admission.py:553` |
| Hub 启动时的三方比对 | `eidolon_hub/hub/adapters/persistence/database.py:373` |
| 每个 Claim 行携带 generation | `eidolon_hub/hub/adapters/persistence/models.py:247` |
