# Pi5 Owner 域世代分歧：诊断与收敛方案（2026-09-16）

只读诊断，未修改工作站的 Owner 材料，未触碰板子上的任何权威数据。收敛方案交机主决定。

## 一句话结论

两代不是同一块板子的前后两次状态，而是**两块物理板子**：工作站的 generation 9 是
2026-09-10 中午给**另一块新 Pi 5** 做首次 install 时自动递进出来的；台架上现在这块是
**原来那块**，从 8 月 27 日起一直是 generation 8，从没有过 9。权威的一侧是板子，
工作站那份计数器是给一块已经不在台架上的板子留下的残留。

## 1. 两代是怎么岔开的

### 机制

当时（`c5c3dd0` 之前）的 `decide_owner_authority` 有一条 `ADVANCE_GENERATION` 分支：

> consumed and the Host holds no Hub Authority marker — that state is gone and cannot be
> restored, so this is a `ResetAuthority` in fact and the generation **must** advance.

也就是说：**工作站的 bootstrap 已消费 + Host 侧没有 Hub 授权库**，就会被判定成"这套授权
状态没了"，于是 `reset_owner_authority()` 把 generation +1、换一个新 `state_id`、
`bootstrap_pending` 重新置为 true。

这条分支不需要 `--reset-existing`，也不需要 `--wipe-authority-data`——**一块全新的、Hub
库还不存在的板子，用同一个 profile 跑一次普通的 `install --apply`，就会命中它**。当天的
install plan 打印的正是 `destructive: none`、`requires_flags: []`，没有任何提示。

### 当天发生了什么（会话记录 `57348e38-…`，时间为 +08）

| 时间 | 事件 | 证据 |
|---|---|---|
| 08-27 09:04 | 原板子建立 generation 8 | 板子 `authority-lineage.json` mtime `2026-08-27 09:04:43`；descriptor `issued_at 2026-08-27T01:00:45Z` |
| 09-10 上午 | 台架换上**另一块新 Pi 5**（换 en7、换 known_hosts、`provision --apply`） | 会话记录 09-10 11:xx–12:0x |
| 09-10 12:11 | `install --release-id 20260910-pi5-new-board-v1 --apply` 发起 | 会话记录 `04:11:13Z`；工作站 descriptor mtime `12:11`，`issued_at 2026-09-10T04:11:18Z`，其中 `owner_domain_generation: 9` |
| 09-10 12:17 | install 成功（7 阶段全 applied），新板子建立 generation 9，控制器把这一代记为已消费 | 会话记录 `04:17:33Z`；工作站 `owner-domain-state.json` mtime `12:17`，`bootstrap_pending: false` |
| 09-10 下午 | 新板子在台架上继续用，`pending` 显示运行 `20260910-pi5-new-board-v1` | 会话记录 `08:35Z` |
| 09-10 21:53 | **原板子换回台架**，对它做了一次 `backup` | `.eidolon-ops/pi5/validation-20260910/backup/20260903-memory-verbatim-off-1-ehost-…`，`taken_at 2026-09-10T13:53Z`，当时板子跑的还是 9 月 3 日那个 release |
| 09-10 23:03 | 第一次撞上本次这个报错 | `.eidolon-ops/pi5/validation-20260910/owner-guard-real-check.json`，8 vs 9，`state_id` 与今天完全一致 |
| 09-11 00:43 | `c5c3dd0` 删掉世代递进机制 | 从此 ops 再也不会自动 +1，同样的形状只会报 `RECOVERY_REQUIRED` |
| 09-11 → 今天 | 一直只走 `deploy`，所以没人再撞到 | `deploy` 走 `refresh_release`，`hub.generated.yaml` 用的是**板子上装着的** authority（见 `host_application.py:192` `prepare_release`），根本不读工作站的计数器 |

台架上这块板子的身份可以独立佐证：rootfs 建于 2026-06-18，`/etc/machine-id` 是
2026-08-04，`/var/lib/eidolon/deployments` 最早一条是 `20260827-authority-lineage-cutover-1`，
并且**没有** `20260910-pi5-new-board-v1` 这条记录。

### 一句话答案

不是工作站单方面"前进了一代却没落到 Host"，也不是 Host 回退过。是**同一份 Owner 材料
被两块板子共用**：第 9 代真实地落在了另一块板子上，而台架上这块从来只有第 8 代。

## 2. 哪一边是对的：板子

- 板子三处自洽，全是 generation 8：Hub 库标记、`authority-lineage.json`、
  `/etc/eidolon/owner-domain/owner_domain_descriptor.json`（`c64738f8…`，8 月 27 日签发）。
- 板子上是真数据：`admission_base_identities_v1` 8 条、`admission_claims_v1` 6 条、
  `admission_claim_grants_v1` 6 条、`hub_device_directory_v1` 6 条，加上 Owner/Companion/记忆。
- 工作站那边**只有两处**和板子不一样，而且都不是密钥：`owner-domain-state.json` 的
  计数器，和 `owner-domain-descriptor.json`。Owner 根、签名者、Host TLS 自 8 月 19 日
  从未变过，两侧逐字节相同：

  | 文件 | 工作站 | 板子 |
  |---|---|---|
  | owner root CA | `605a8df8…` | `605a8df8…` ✅ |
  | authority signing cert | `97de37ce…` | `97de37ce…` ✅ |
  | hub.crt / hub.key | `e2e5cb07…` / `921487cf…` | `e2e5cb07…` / `921487cf…` ✅ |
  | owner domain descriptor | `a728a1dc…`（gen 9） | `c64738f8…`（gen 8） ❌ |

  `owner_domain_id` 两边都是 `owner-b0a862b0aab941d64554`——**同一个 Owner，同一套密钥**，
  分歧只在计数器和那份签名目录上。

- 板子上还留着一件遗留物：`/var/lib/eidolon/hub/authority-bootstrap.json` 里是
  **generation 9 的已消费墓碑**（`bootstrap-consumed`，`5fce834c…`，9 月 10 日晚上随部署
  送过去的）。它是惰性的：Hub 只把 *pending* 的 capability 用进一个空库，已消费的那份在
  库存在时不起作用——这块板子带着它跑了 6 天、跨过重启，Hub 一直正常。收敛后这份文件会
  被重新渲染成 generation 8 的墓碑，与板子自己的库一致。

## 3. 有没有可用的 Authority 备份：没有

`authority-backup` 从来没跑过。全盘找不到任何 `authority-restore.json` 或
`*-hub-authority/` 目录（`authority_backup` 的产物形状见 `controller.py:1326`）。

`.eidolon-ops/pi5/validation-20260910/backup/…` 是普通 `backup`（9 张 sqlite + `backup.json`），
不是 Authority 恢复包；它取自 9 月 10 日 21:53 的**原板子**，可以当兜底快照，但用它做
`authority-restore` 只会把板子的授权库倒回 9 月 3 日，是纯粹的损失。

所以报错里给的第一条路（"Restore the matching Authority backup"）**没有东西可恢复**；
即使有，"matching" 的那份也是第 9 代、属于另一块板子的数据。

## 4. 收敛方案

### 三条路

| 方案 | 做什么 | 代价 | 评价 |
|---|---|---|---|
| A. 恢复 Authority 备份 | — | — | **不成立**：不存在这样的备份 |
| B. `install --reset-existing --wipe-authority-data --apply` | 现在这条路会走 `identity_replacement`，**换 Host ID、换 owner_domain_id**、清盘 | 全部授权数据、已准入设备、Companion、记忆丢失；手机重新配对；Owner 域都是新的 | 对一台正常运行的板子来说是最大代价，且不解决问题——问题不在板子上 |
| **C. 把工作站的计数器改回板子已经证明的那一代**（推荐） | 只改工作站私有材料里的两个文件，**不碰板子** | 已在副本上验证：零副作用 | 最小、可逆、与板子事实一致 |

### 方案 C 具体做法（2026-09-17：已成为一条命令）

最初写下来时这是"手改两个文件"。手改私有材料正是 ops 存在的意义要消灭的东西，而且
这件事会再次发生（台架上就有两块板子），所以它被补成了一条缺失的能力：

```bash
./eidolon pi5 trust-host-authority                                       # 打印两边各认哪一代
./eidolon pi5 trust-host-authority --apply --replace authority-state_LJxSTZE2bamD2OGWCj-2BazBbosGT_AE
```

命令做的正是方案 C，但把两处手工判断变成了机器检查：

- **采纳而不是重签**。板子正在服务的那份签名目录被原样收下，所以缓存过它的设备手里
  那份继续成立，revision 线也不重开。
- **只接受这份材料自己的 Owner 根签过的目录**（`owner_domain_assets._validate_directory`）。
  这挡住的是一台板子报出这个 Owner 从没签发过的世代——也是"为什么可以相信板子给的
  字节"的全部答案。
- **state id 要操作者在板子上核对后指名**。没有任何签名覆盖一个 state id，所以这一半
  和 `trust-host-key` 同一个理由：由能看到板子的人确认。
- **同代目录只退不进**。如果工作站已经签发了更新的 revision 还没送出去，采纳一份更旧
  的会被拒绝（`_require_not_older_directory`），否则会静默撤掉刚做的改动。
- **板子两份记录不一致就拒绝**。那种情况下板子确实丢了东西，该走恢复，没有"已建立的
  那一代"可以采纳。
- 不写板子、不动 key、不作废 Claim。采纳错了也放行不了什么：install 闸门会再问一次
  板子，然后照样拒绝。

实现：`owner_domain_assets.adopt_host_authority`（原语）、
`controller.trust_host_authority`（操作）、hostagent 只读动作 `owner-directory`，
以及 plan/capability/CLI/wrapper 的常规接线。`authority_capability` 的拒绝文案也补上了
第三种可能——在此之前它只说得出备份和清盘两条路。

### 已验证的效果（在副本上跑的，未触碰真实材料）

证据：`.eidolon-ops/pi5/diagnosis-20260916-owner-generation/convergence-simulation.{py,txt}`

```
material rewritten by ensure_owner_domain_assets: none
assets: owner-b0a862b0aab941d64554 8 authority-state_LJxSTZE2… bootstrap_pending= False
descriptor bytes identical to the board's: True
bootstrap document that would be shipped: {"…","operation":"owner-authority.bootstrap-consumed","owner_domain_generation":8,…}
decision (marker==anchor==gen8): keep_established_lineage
```

- `ensure_owner_domain_assets` 之后**一个字节都不再改写**：不签新 TLS 叶子，不推进
  `directory_revision`，不重签目录。
- 工作站的 descriptor 与板子上那份**逐字节相同**——对任何已经缓存过它的设备来说，
  什么都没有变过。
- 闸门的判定从 `recovery_required` 变成 `keep_established_lineage`
  （`controller.py:1165`），`install` 的第一道闸门就过了。
- 随后送到板子的 bootstrap 文件会变成 generation 8 的墓碑，比现在那份 gen 9 的更一致。

### 收敛之后还有第二道闸门

`install --apply` 过了授权闸门之后，会走到 `host_layer.stage_install_files` →
`bind_delivery(..., allow_create=bootstrap_pending)`（`host_delivery.py:30`）。
`bootstrap_pending` 是 false，而 `.eidolon-ops/pi5/` 下**没有** `host_delivery.json`
（也没有 `.host-delivery.lock`，说明这条路对 pi5 从来没走过），所以会报：

> used legacy identity has no hardware delivery evidence; use ordinary deploy to preserve
> the installed Host, or initialize independent inputs for a new board

这道闸门是 `a6eb5a7`（09-11）加的，正是为了防止"同一份 inputs 被第二块板子用掉"——
也就是 9 月 10 日那件事。它只在 `--apply` 时触发，`install` 的 dry-run（`apply=False`）
不会走到。

**所以：修好世代分歧是必要的，但不足以让 `install` 在这块板子上跑完。** 今天那个
`factory_setup_code` 要不要继续走 install 这条路，是一个独立的决定，得先想清楚
"给一台已经在跑的 Host 补一个安装期文件"到底应该由哪个动词负责。

## 5. 需要机主决定的事

1. 是否执行方案 C（只改工作站两个文件，不碰板子）。
2. 那块 9 月 10 日装过的**新板子**现在在哪、还要不要。方案 C 之后，这个 profile 就
   不再认它了（它在第 9 代）；它如果还要用，应该有**自己的** profile 和自己的
   `.eidolon-ops/<host>/inputs`，而不是共用 pi5 这一份——它和现在这块板子目前共用同一个
   Host identity 和同一个 Owner 根。
3. `factory_setup_code` 的投递路径（见 §4 第二道闸门），建议单独立题。

## 6. 收尾状态（2026-09-17）

代码侧已完成，材料侧未执行：

- ✅ `trust-host-authority` 已落地（见 §4），含 12 条新测试：两块板子的分叉在原语层和
  controller 层各复现一次，并证明采纳之后 install 闸门从 `recovery_required` 变成
  `keep_established_lineage`。全量回归 1143 passed / 48 skipped，相对基线
  （1130 passed / 48 skipped）零新增失败；改动范围 Ruff 通过。
- ✅ 拒绝文案、README「一份 Owner 材料只能认一台 Host」、runbook 已同步。
- ⏳ **`.eidolon-ops/pi5/owner-domain/` 仍是 generation 9**。pi5 已下台架（台架上换成了
  opi5max），命令需要读板子才能跑。板子回来后跑一次
  `trust-host-authority --apply --replace authority-state_LJxSTZE2…` 即收敛，然后用
  install 干跑复验。

`.eidolon-ops/pi5/diagnosis-20260916-owner-generation/board-owner-directory.json` 是
板子那份签名目录的离线副本（sha256 与板子自报一致），可用于离线核对，但采纳仍应走命令
——它读的是板子此刻的说法，而这正是离线做不到的那一半。

## 7. 证据

只读观察原件保存在 `.eidolon-ops/pi5/diagnosis-20260916-owner-generation/`：

- `workstation.txt`：工作站 Owner 材料的列表、哈希与 state 内容
- `board.txt`：板子的 anchor / bootstrap / Hub 库标记与行数 / 已装 descriptor /
  机器年龄 / 部署日期分布
- `convergence-simulation.py` + `.txt`：方案 C 在材料副本上的验证

另可交叉核对既有证据：`.eidolon-ops/pi5/validation-20260910/` 下的
`owner-guard-real-check.json`（9 月 10 日 23:03 同一个报错）、
`final-before-identity.json` 与 `authorization-20260911-baseline.json`
（两侧哈希，与今天完全一致，说明 9 月 11 日以来两边都没有再变过）。
