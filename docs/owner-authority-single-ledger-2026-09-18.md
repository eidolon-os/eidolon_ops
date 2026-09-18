# Owner 授权单一账本：板子是它自己授权的唯一记录（2026-09-18）

实施 [Owner 授权血统机制评审](owner-authority-mechanism-review-2026-09-17.md) §4 的方案 A、B、C。
起点是 BOX-3 在 pi5 上配网被手机以「Owner Domain generation rollback: accepted 9, offered 8」
拒绝；那个 9 是 2026-09-10 第二块板子留在手机上的残留，而制造它的机制早在 09-11 就删掉了。
本文记录的是让这一类事在结构上不再发生的改动，不是那一台手机的清理。

## 原则

`owner_domain_generation` 与 `state_id` 是「某一台 Host 建立过的授权」这一事实的属性。它们诞生
在 Host（Hub 消费 bootstrap 能力票时写下 `hub_authority_state` marker 与 `authority-lineage.json`
anchor），也只活在 Host。**工作站不保存它们。** 任何需要它们的操作先观察 Host，再渲染。

`deploy` 从一开始就是这个形状（`hostagent/deployment_identity.observe()`），六天里在一块材料
分叉的板子上顺顺当当；`install` 私藏了一份镜像，于是撞墙。改动就是让 `install` 收敛到 `deploy`。

七份可写副本变成一份源（板子的 marker）加若干由它渲染出来的拷贝。Hub 启动时的三方比对保留，
性质从"分布式全等断言"变成"渲染结果对源的一致性自检"——按构造必然通过。

## 工作站还保存什么

| `owner-domain/` 里的文件 | 去留 | 理由 |
| --- | --- | --- |
| `owner-domain-root.key.pem` / `-ca.pem` | 留 | Owner 根 |
| `authority-signing.key.pem` / `-certificate.pem` | 留 | 目录签名者 |
| `hub.crt` / `hub.key` | 留 | Host TLS 叶子 |
| `owner-domain-descriptor.json` | 留 | 上次签发或采纳的目录：revision 线的记忆 |
| `owner-domain-state.json` | **删** | generation / state_id 是板子的；`bootstrap_pending` ⇔ 板子无 marker；`owner_domain_id` 可从根证书推导 |

旧文件在首次触碰时被 `retire_legacy_authority_state` 删除，操作结果里报 `legacy_state_removed`。
带它的旧格式 `authority-backup` 包继续可恢复：文件只与包内快照互校，不再被读作权威。

`init-inputs` 只创建密钥（`ensure_owner_material`），不签目录——目录要写一个 generation，而
generation 是 Host 建立的，此时没有 Host 可言。

## install 的判定：一次往返，一个纯函数，八个具名情形

hostagent 新增只读 op `install-context`，一次往返带回：lineage 两份副本（marker、anchor，及二者
一致时的 `established`）、板子服务的签名目录原文、永久硬件标识、板子持有的身份密钥摘要。

`install_decision.decide_install(context, owner_domain_id, host_id, identity_sha256, binding)`：

```
marker ≠ anchor（或只有一份）                          → host_incident        拒
已建立，但 owner_domain_id 不是我们的                  → foreign_authority    拒
有绑定，硬件 ≠ 绑定                                    → wrong_board          拒
有绑定匹配，板子无 Authority                           → authority_lost       拒
有绑定匹配，已建立且是我们的                           → continue_established_lineage
无绑定，板子无 Authority                               → first_install
无绑定，已建立且是我们的，目录署名此 Host id 且持有身份 → adopt_delivery_evidence
无绑定，已建立且是我们的，但以上证明不齐               → identity_unproven    拒
```

每个拒绝恰好一句话（`install_decision.refusal`），没有一句提到已不存在的动词，也没有一句
提议"前进一代"。2026-09-10 的形状落在 `wrong_board`，第一道就挡住。

判定顺序即证据优先级：板子自己两份副本不一致，是事故，与这个 profile 记什么无关；板子持有
别人的 Authority，是外人，与是哪块板子无关；然后才轮到交付绑定说"这是不是我交付过的那块"。

## 采纳并入签发

`ensure_owner_domain_assets(material_root, identity, port, host_authority, served_directory=…)`：

- `host_authority` 是板子报出的 `established`（`HostAuthority.established_from`），或板子为空时
  `HostAuthority.fresh()`——generation 1，随机 state id。**这是工作站唯一会叫出名字的世代。**
- `served_directory` 是板子服务的目录。用本材料的 Owner 根**验签**（只验签名与委托，不验有效
  窗——过期的目录仍是这个 Owner 的，过期的答案是重签下一版而不是拒绝），要求它署的世代等于
  板子建立的世代，然后**逐字节采纳**为本材料 revision 线的基线。唯一不倒退的：同代下工作站
  已签发、未投递的更高 revision。
- 采纳之后再跑原来的"端点变了就升 revision"逻辑。

结果：手机、ESP 缓存过的那份目录一个字节不变；`--replace` 消失——工作站上已没有 state id 可与
板子对比，板子 marker==anchor 的那一份就是事实；`trust-host-authority` 退役。

## 交付绑定：第一道闸门，且在证明之后才写

`host_delivery.json` 在 **`confirm_authority_established`**（install 收尾，板子报出的 marker ==
本次送去的 lineage 之后）才写，不再在 staging 时写。于是：

- 「有绑定、板子无 Authority」确实意味着丢失（`authority_lost`）；
- Hub 还没消费能力票就失败的首次安装留不下绑定，重试仍是 `first_install`；
- 绑定存在之前装好的 Host，由 install 自己补录（`adopt_delivery_evidence`）；`trust-host-delivery`
  保留为"不发 release 只补证据"的入口，共用同一判定。

`converge-inputs` / `repair-credentials` / `refresh`（authority-restore 用）在 staging 前也走这
个判定（`HostLayer.require_established_host`）：一块投错的板子在收到任何一份凭据之前就被拒绝。
`deploy` 不改：它不交付身份，也从来不读工作站的授权状态。

## Mac source-run

同一判定，本地观察：Hub 库 / anchor 直接读文件，硬件用本机标识。两处与产品 Host 不同、并写在
代码注释里的地方：

- 身份证明：本机既是 profile 也是 Host，放置的身份文件就是它从自己的输入里放的，"持有"即证明。
- 绑定位置：`<bootstrap_state_root>/delivery/host_delivery.json`，与放置的身份同生同灭——
  `reset --wipe-authority-data` 一起清掉，不 wipe 的 reset 一起保留。绑定在 `commit_owner_authority`
  （start 之后）写，不再在放置身份时写。

## 化石

- pi5 留在 **8**、opi5max 留在 **3**：09-11 边界 #4（不改旧描述），已入册设备继续工作。新 Host 恒 **1**。
- 手机里那个 **9**：是 09-10 第二块板子留下的唯一残留。出口是 mobile `9e3a4d0` 的设置页对齐入口
  （14:12 提交，晚于当时装机的 APK，需重装 app）。ops 侧这次改动让同样的残留无法再产生。

## 明确不做的

- **`state_id`（或任何血统标识）不进签名 descriptor。** 它解决的是"同一个 Owner 根下两条血统"，
  而 09-11 的产品规则是 Owner 与逻辑 Host 一一对应；这个场景现在在 `wrong_board` 被挡住。
  `OwnerDomainDescriptorV1` 在手机与 ESP 固件里都是严格字段集解析，加字段等于全 fleet 已部署设备
  同时失效。
- **不做签名纪元链**（评审 §4D）。generation 恒为 1，链为空；真要做时再接，不改任何已部署的东西。
- **不放宽设备侧的单调棘轮。** 它是防重放的正确形状；它现在只会在残留上触发，而残留不再产生。

## 验证

- ops 全量：`1300 passed / 47 skipped`（基线 1259 / 47），改动范围 Ruff 通过。
- 突变验证（把修好的判定故意弄坏，看测试变红）：① 绑定不校验直接 `continue` → 3 条红；
  ② 采纳板子目录时跳过验签 → 1 条红；③ 板子证明之前就写绑定 → 1 条红；④ 让工作站更高世代的
  化石目录压过板子的 → 1 条红。四处还原后全绿。新增 `tests/test_install_decision.py`（八态全覆盖、
  拒绝文案、观察形状校验）；`test_owner_domain_assets.py` 重写为"从板子学"的语义（采纳、不倒退、
  拒签、化石替换、遗留文件退役）；controller 侧复现 09-10 分叉 → `wrong_board`、工作站 gen 9 化石
  + 板子 gen 8 → 自动采纳零确认、旧 Host → `adopt_delivery_evidence`、首装 → 证明后才写绑定。
- 真机只读：见下方「真机核对」。

## 真机核对（2026-09-18，只读）

不跑 `install` 干跑（它会真的准备候选，上一轮记录 104 秒），直接调用闸门本身
`authority_capability(apply=False)` 与 `trust_host_delivery(apply=False)`——就是 install 第一步做的
全部事情，一次 hostagent 往返。为了不碰现役材料，把 `.eidolon-ops/{pi5,opi5max}` 的 inputs /
owner-domain / 绑定 / ssh 配置复制到 worktree 下的私有副本上跑；hostagent 随操作发送，所以板子
侧跑的是新的 `install-context`。

| | pi5 | opi5max（rk3588） |
| --- | --- | --- |
| 板子 marker == anchor == established | gen 8 / `authority-state_LJxSTZE2…` | gen 3 / `authority-state_cVqyDQNN…` |
| 硬件 | `device-tree:raspberrypi,5-model-b` `sha256:d2bd0ca8…` | `device-tree:rockchip,rk3588-orangepi-5-max` `sha256:9791c070…` |
| 服务目录 / 持有身份 | 是 / 是 | 是 / 是 |
| 判定 | `continue_established_lineage` | `continue_established_lineage` |
| delivery / directory | `verified` / `current` | `verified` / `current` |
| `trust-host-delivery` | `current` | `current` |
| 材料副本 7 份密钥/证书/目录 | 前后 sha256 逐一相同 | 前后 sha256 逐一相同 |
| `owner-domain-state.json` | 首次触碰时退役（副本上） | 首次触碰时退役（副本上） |

两台板子都没有被写入任何东西——闸门只读板子。现役 `.eidolon-ops/` 里两个 `owner-domain-state.json`
会在合并后第一次 `install` / `authority-backup` / `trust-host-delivery` 时被退役并在结果里报出。
