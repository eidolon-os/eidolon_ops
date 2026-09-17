# Pi5 常开 claim 窗口：投递路径与"一次声明该不该要一次 install"

> 2026-09-17。诊断、设计**与执行**。机主 22:30 选定方案 B 并批准执行；
> 22:37–22:45 在真机完成，验收通过（见 §8）。
> 前一份是 `pi5-owner-generation-divergence-2026-09-16.md`，那份的 §5 第 3 条把本题留下来了。

---

## 0. 结论先行

- 板子确实兑现不了它的声明，原因就是 `/var/lib/eidolon-bootstrap/factory_setup_code` 不存在。
- **但守卫已经在板子上了，而且已经是红的。**它不靠部署到达——它是注入的。上一份报告和
  `1e4ec4c` 的 commit message 里"守卫要等部署才能到板子"这个担忧，实测不成立。
- 验收 SQL 太松，按它验会得到假绿。
- 投递不必走整机 install。`converge-inputs` 今天**已经把这个文件传到板子上了**，又删掉。
- 设计问题的答案：**write-once 推不出 install-only。**这两件事被当成一件了。

---

## 1. 板子现在什么样（2026-09-17 14:05 UTC 实测）

```
EIDOLON_BOOTSTRAP_CLAIM_WINDOW=always_open        ← 声明在
ls /var/lib/eidolon-bootstrap/factory_setup_code  ← No such file
claim_state = claimed
```

`/var/lib/eidolon-bootstrap/` 全部内容：`bootstrap.sqlite3`（+wal/shm）、
`commissioning_tls.pem`、`host_identity.ed25519`。码不在。

`_open_factory_window`（`eidolon_admin/.../bootstrap/service.py:131`）第一个条件
`code is None → return`。窗口从来没开过。

### 1.1 验收 SQL 要改

任务给的那条现在就返回 **7**：

```sql
select count(*) from commissioning_sessions where consumed_at is null;   -- 7
```

7 行全是 8 月被 supersede 掉的，`revoked_at` 有值。最后一个 session 是 `2026-08-28T09:06Z`。
`is_open()`（`eidolon_admin/.../bootstrap/domain/model.py:129-134`）的判据是三个条件：

```python
if self.consumed_at is not None or self.revoked_at is not None:
    return False
return self.expires_at is None or self.expires_at > now
```

对齐后的查询，现在返回 **0**：

```sql
select count(*) from commissioning_sessions
 where consumed_at is null
   and revoked_at is null
   and (expires_at is null or expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now'));
```

**验收用这条。**按原来那条验，什么都不做就已经"通过"了。

### 1.2 对照组这轮没验成

opi5max `10.42.0.2` 超时。台架的 USB 网线现在插在 pi5 上，两块板子共用一条线——这是台架的
已知性质，不是故障。对照组的事实沿用上一份报告里已记录的那次实测。

---

## 2. 守卫已经到了，而且是红的

这是对任务前提的一处更正，有实测。

```
./eidolon pi5 app-ready
  "status": "degraded"
  "claim_window_honored": false      ← 红
  其余 24 条全 true
```

板子跑的还是 `pi5-claim-window-20260916c`，早于 `1e4ec4c`。那为什么这条事实会出现？

**因为 host agent 不是部署上去的，是每次调用现场注入的。**

- `transport.run_agent`（`src/eidolon_ops/transport.py:276-283`）：
  `script = injected_script()`，然后 `python3 - <action> <payload>`，脚本从 stdin 喂进去。
- `injected_script`（`src/eidolon_ops/hostagent_delivery.py:62-79`）把
  `_SOURCE_ROOT = src/eidolon_ops/hostagent/` 下所有 `*.py` base64 打包成一个自包含脚本。
- `1e4ec4c` 改的正是 `src/eidolon_ops/hostagent/probe.py`。

所以 **`claim_window_honored` 随 ops checkout 走，不随 release 走**。工作站一 `git pull`，
下一次 `app-ready` 板子就开始回答这条。

### 2.1 那 `doctor` 为什么报 healthy

不是因为守卫没到，**是因为 doctor 问的是另一个问题**。

- `doctor` → `run_agent("doctor-host", ...)`（`controller.py:587`）
- `app-ready` → `run_agent("app-ready", ...)`（`controller.py:550-554`）

readiness 契约（`READINESS_CONTRACT`，`readiness.py:250-254`）归 `app-ready`。`doctor` 从来
没有承载过这 25 条事实。机主跑 `doctor` 扑空，是这两个动词的分工没被说清，不是守卫缺席。

### 2.2 这直接解掉了任务里那个"先有鸡"

任务第 2 条方向三写着：「守卫本身也要靠部署才能到板子上。一个只在部署后才会报红的检查，对
'声明已经写下但还没部署'的窗口期是无效的。」

**这个窗口期不存在。**守卫和声明走同一条路——都在 ops checkout 里，都在下一条 ops 命令时
一起到达板子。声明改了、守卫改了，同一个 `git pull` 之后第一次 `app-ready` 就同时生效。

（这个性质对**所有** `hostagent/` 里的探针都成立，不只这一条。）

### 2.3 红着不挡部署

`_observe_app_readiness`（`release_transaction.py:587-609`）明写「Observed after the gate, so
it can never be one」，且自吞异常。所以这面红旗不会卡住 `deploy`。板子 20/20 服务正常，
这条红是准确的、且是唯一的红。

---

## 3. 投递路径：install 不是唯一能到的动词

### 3.1 文件其实每次 converge 都已经上去了

`stage_install_files`（`src/eidolon_ops/host_layer.py:322-333`）：

```python
factory_code = self._factory_setup_code()
if factory_code is not None:
    staged = temporary / "factory_setup_code"
    ...
    self.transport.upload(staged, f"{stage}/factory_setup_code")
for name in names:      # ← names 管不到上面那段
```

**在 `names` 循环之外，无条件。**profile 指了码就上传。

而 `converge_inputs`（`controller.py:1029-1031`）正是调这个函数：

```python
stage = f"/var/tmp/eidolon-secrets-{_CONVERGENCE_STAGE_ID}"
self.host_layer.stage_install_files(_CONVERGENCE_STAGE_ID, stage)
```

于是今天 `converge-inputs --apply` 在板子上的实际行为是：

1. 把 `factory_setup_code` 传到 `/var/tmp/eidolon-secrets-<id>/factory_setup_code`；
2. agent 侧 `converge()`（`hostagent/secret_inputs.py:164-192`）只遍历 payload 里的
   `declared` —— 即 `declared_secret_env_keys()`，全是 env 文件的 key 集合，
   `factory_setup_code` 不是 env 文件、没有 key，**根本不在这个集合里**；
3. `finally` 里的 `cleanup-stage`（`hostagent/staging.py:318-327`）`shutil.rmtree` 整个目录。

**码每次都到了板子上，又被删掉。**缺的只是 agent 侧认它这一步。

实测（`./eidolon pi5 converge-inputs`，只读）：

```
host status : already_current
host missing: {}
host absent : []
```

### 3.2 `deploy` 确实不投递

`RELEASE_CONFIGURATION_INPUTS`（`contract.py:350-353`）不含它；
`deployment_identity.PRESERVED_INPUTS`（`deployment_identity.py:17-21`）含它，但只做保留：

```python
if name in {"authority-bootstrap.json", "factory_setup_code"} and not path.exists() ...:
    hashes[name] = None
    continue
```

不存在记 `None` 放行。所以 deploy 永远不会创建它，也永远不会因为它缺席而失败。

### 3.3 install 的代价（读码得出；dry-run 被权限挡下，见 §6）

`_INSTALL_MUTATIONS`（`release_transaction.py:29-37`）七条，逐条对一块正在服务的板子的含义：

| 步骤 | 代价 |
|---|---|
| preflight `require_install_files=True` + `require_clean_sources` | 共享 checkout 现在是脏的（另一会话 3 个文件），大概率先被挡 |
| `_provision(apply=True)` | foundation 重装 |
| `bundles.prepare` | **整套 release 重新构建 + 上传**（有线 3–6 分钟，无线远不止） |
| `_install_prerequisites` | 15 个已存在输入逐个哈希/权限/属主比对 |
| `_create_data_v2_baseline` | `alembic upgrade head` |
| `install_assets` + `switch_components` + `reload_systemd` | 组件切换 |
| `start_release` + `wait_ready` | **整机停服重启**，产生新 release id 取代 `pi5-claim-window-20260916c` |

还有一处风险值得点名：`_install_prerequisites`（`install.py:421-446`）对已存在的目标是
write-once —— 哈希、权限、属主任一不符就抛
`existing prerequisite differs during resume`。这块板子上那 15 个输入全都已存在。任何一个
在这半个月里漂了，install 会在 prerequisites 阶段炸，**而那时 foundation 和 bundle 已经跑完**。

**为一个 8 字节文件付这个代价，不成比例。**

### 3.4 `commissioning-code` 能满足验收，但不是这件事

`commissioning_code`（`controller.py:1158-1181`）不传 `--code` 时自动取
`app.factory_setup_code()`，**不需要板上有那个文件**，且现在的窗口不设过期。

跑一次它，§1.1 那条验收 SQL 立刻返回 1，`claim_state` 仍是 `claimed`。**验收通过。**

但它不是 `always_open`。它开的是一个一次性窗口：被认领消耗掉就没了，bootstrapd 重启也不会
自己回来。`always_open` 买的恰恰是这两件——`_open_factory_window` 在 `initialize()` 里跑，
`reopen_standing_claim_window` 在 claim 之后跑。两者都读那个文件。

**所以：验收标准可以被一条不解决问题的命令满足。**验收时必须同时证明窗口是**站着**的
（重启 bootstrapd 之后它自己回来），而不只是**开着**的。

---

## 4. 那个设计问题

> 一次纯声明的改动，要经过一次完整 install 才能兑现，这合理吗？

不合理。但上一轮给出的理由（"收益已被既有动词覆盖"）现在不成立了，因为多了一个新事实：
**`claim_window_honored` 是红的，而唯一能让它变绿的动词是整机重装。**一个 readiness 事实
红着、唯一修法是重装，这是个坏契约——它把"报告问题"和"能修问题"之间的距离拉到了最大。

### 4.1 我的判断：write-once 推不出 install-only

上一份报告说：「契约把它和 `host_identity.ed25519` 归为一类（"same write-once delivery"），
这是对的，不改。」

归类是对的——同目录、同属主、同 0600、都不许改写。**但从"不许改写"推不出"只有一个动词能
创建"。**这是两件事被当成了一件：

- **write-once** 说的是：已存在就不许被覆盖。
- **install-only** 说的是：不存在时只有 install 能创建。

`converge` 的既有语义恰好只做前者不做后者——它是 additive 的，值不同就拒绝（
`secret_inputs.py:228-244` 那段注释把理由写得很清楚："Rotation is a different operation"）。

而且这两个文件本来就不是一类东西：

- `host_identity.ed25519` 丢了要走 `trust-host-delivery` / `authority-restore`，因为换它
  **等于换主机**——每一条信任绑定都要跟着动。
- `factory_setup_code` 丢了只是窗口开不出来。补上它**不改变任何信任绑定**：它的值本来就
  在工作站的 profile 里，`commissioning-code` 每天都在用同一个值开窗。

### 4.2 建议：给 `converge` 加一条窄规则

不是把 converge 的模型从"env 文件里的 key"扩成"声明过的输入"（那次模型扩展上一轮撤回得
对）。是加一条窄得多的：

> **`converge` 额外处理 `OPTIONAL_INSTALL_INPUTS` 里的不透明文件输入，且只在目标不存在时
> 创建；存在就一个字节都不碰、也不读。**

为什么这条窄规则正当：

1. **语义和现有的那半完全一样。**env 那半："加 Host 缺的 key，值不同就拒绝"；这半："加
   Host 缺的文件，存在就不碰"。同一条 additive 规则，只是粒度从 key 变成文件。
2. **不动 write-once。**`factory_setup_code` 在 `PRESERVED_INPUTS` 里已经是"可以没有，
   有了就不动"这个语义（不存在记 `None` 放行）。这条规则是它的另一半，不是它的例外。
3. **范围由既有集合定界。**`OPTIONAL_INSTALL_INPUTS` 是一个已经存在的 frozenset，今天只有
   一个成员。这不是开一个通道，是给一个**已经被命名为"可选"**的输入配上它唯一缺的动词。
4. **传输已经建好了。**文件本来就已经在 stage 目录里。不需要新的上传、新的 profile 字段、
   新的动词、新的 CLI 参数。
5. **动词名字本来就对得上。**converge-inputs 的自述是"give this machine and the Host the
   credentials the product declares and they are missing"。这个文件正是"declared and
   missing"。

### 4.3 另外两个方向为什么不选

**方向一（变 refreshable，让 deploy 投递）：反对。**`REFRESHABLE_*` 的语义是"控制器重新
渲染整份并覆盖目标"——那正好破坏 write-once。而且 deploy 每次都发，意味着每次升级都在重写
一个 secret；`contract.py:341-344` 自己的注释已经说了这正是 deploy 不发凭据的原因。

**方向三（维持 install-only，让守卫成为正式契约）：已经做到了一半，但不够。**守卫该留，
而且它**没有**先有鸡问题（§2.2）。但它只让静默变响，没给出响了之后的修法。红旗 + 唯一修法
是重装 = 一面大家学会无视的红旗。方向三需要方向二才完整。

### 4.4 采纳后要跟着改的文字

`probe.claim_window_honored` 的 docstring 和 `1e4ec4c` 的 commit message 现在都写着
"only an install delivers one"。采纳方向二的话这两处变成不实。docstring 必须改；commit
message 由新 commit 的正文更正。

### 4.5 一个独立的小问题

`doctor` 不带 readiness 事实，`app-ready` 才带（§2.1）。机主的直觉是跑 `doctor`，扑空了。
要不要让 `doctor` 也捎上这 25 条，或者至少在 `doctor` 报 healthy 时提一句"readiness 归
app-ready"——这是个独立决定，不在本题范围内，但值得单独立题。

---

## 5. 两条可执行路线（待机主选）

### A：不改代码，等下次本来就要做的 install 顺手交付

- 代价：`claim_window_honored` 这面红旗挂到下次 install 为止。它是准确的——板子确实兑现不了。
- 期间台架要开窗：`./eidolon pi5 commissioning-code`（不需要板上的文件，同一个码，不过期）。
- 风险：下次 install 本身带 §3.3 的全部代价和 write-once 比对风险。

### B：加 §4.2 的窄规则，用 converge 交付（**我建议这条**）

1. ops 侧改 `hostagent/secret_inputs.py::converge` + `controller.converge_inputs` 的 payload，
   带测试（包括"目标已存在则一个字节都不碰"和"profile 没指码则什么都不做"两条）；
2. `./eidolon pi5 converge-inputs`（先只读，确认报告里出现这个文件）；
3. `./eidolon pi5 converge-inputs --apply`；
4. **`sudo systemctl restart eidolon-bootstrapd`** —— `_open_factory_window` 在
   `initialize()` 里跑，不重启不开窗。只重启这一个 unit，不是整机 `restart`；
5. 按 §1.1 的 SQL 验收，并确认 `claim_state` 仍是 `claimed`；
6. 再重启一次 bootstrapd，证明窗口**站得住**（不只是开了一次）。

代价：一次 ops 侧代码改动 + 一次 bootstrapd 重启。不重建 release，不动其余 19 个服务，
不碰板子上任何已存在的文件。

---

## 6. 本轮没做到的事

- **install 的 dry-run 没跑成。**`./eidolon pi5 install --release-id ...`（无 `--apply`）
  被会话的自动权限分类器判为 production deploy 拦下。§3.3 的代价分析是读码得出的，不是
  实测的。如果机主倾向路线 A，这条 dry-run 应该先跑一次再决定——它本身是只读的
  （`release_transaction.py:666-684` 返回 `"status": "planned"`）。
- **opi5max 对照组没复验**（§1.2，共用网线）。
- **没有动板子。**工作区也没动：`git status` 里只有另一个会话的三个文件
  （`product-source.conf`、`source_assets.py`、`test_companion_voice_deployment.py`），
  本轮一个字节都没改。

---

## 7. 补充：「为什么不能 install」（机主提问，2026-09-17）

**能。没有任何东西禁止 install。**上面说的是"现在直接跑会被挡下，而且它送的不只是那个码"。
三件事，都是实测：

### 7.1 现在直接跑会被 preflight 拒

`install --apply` 走 `preflight.run(require_install_files=True)`，`require_clean_sources`
默认 True → `sources.require_clean()`（`source_resolution.py:227`）。实测此刻：

```
6 source(s) hold uncommitted work, which no release can carry
  eidolon_admin 7 / eidolon_channel 8 / eidolon_data 10 /
  eidolon_sdk 3 / eidolon_agent 2 / eidolon_kernel 1
```

这是硬闸门，不是判断题。（`doctor` 能跑是因为它显式传 `require_clean_sources=False`。）

### 7.2 就算清干净了，install 送的是"工作站此刻的全部"

`config/eidolon-pi.toml` 里**故意没有** `revision` 钉子——那段注释自己解释了为什么：
"A release is defined by what the repositories hold"。所以一次 install 会把这些一起推上板
**并激活**：

```
eidolon_admin    2 commits    629694d → 46a062b
eidolon_channel  1 commit     d622069 → 8990dd8
eidolon_kernel   6 commits    88e3a42 → c8a2e72
eidolon_sdk      3 commits    fe7cf1d → ab4a66d
```

是别的会话正在做的 Body contract 重构。**为送一个 8 字节文件，替别人决定他们半途的工作
什么时候上板。**这一条和代价无关，是权限问题。

### 7.3 代价（§3.3 的总结）

整机停服重启 + `alembic upgrade head` + 整套 release 重新构建上传 + 15 个已存在输入逐个
write-once 比对（任一漂了就在 prerequisites 阶段抛错，**而那时 foundation 和 bundle 已经
跑完**）+ 一个新 release id 取代 `pi5-claim-window-20260916c`。

### 7.4 但有一条干净的 install：把每个源钉到板子正在跑的提交

`--revision` 钉住的源**豁免** clean 检查，理由写在 `source_resolution.py:250-259`：
commit 被点名之后，那个 worktree 的脏根本进不了 `git archive`。

于是可以构造一次**单变量** install：代码逐字节不变，唯一的新东西就是那个文件。

```bash
./eidolon pi5 install --release-id pi5-setup-code-20260917 \
  --revision eidolon_admin=629694d4cb16a7e9b98e1944bc6e4db30984c25f \
  --revision eidolon_channel=d62206954b86b423d2d3ca2f0a02a4f24f0630c4 \
  --revision eidolon_kernel=88e3a4263a9f9a58ddf66fdf1000d3187d0845e0 \
  --revision eidolon_sdk=fe7cf1d6cafb5310f726d34dd8b6a30a9923da4d \
  --revision eidolon_data=7461fb27f144b5488948acba20c7bcfb25ceeea3 \
  --revision eidolon_hub=85cbc58b95a5d0123db6168953c43dcfc963f3a3 \
  --revision eidolon_agent=694956294735b253a25d6953b879dca4bea1879f \
  --revision eidolon_memory=5333cb0a3f6c2653bfb6f9de4cb25b59f163a667
```

（前 4 个是 `pending` 报的板上提交；后 4 个没有分歧，取本机 HEAD —— 但仍必须钉，因为
`eidolon_data`/`eidolon_agent` 的 worktree 是脏的，不钉就过不了 §7.1 的闸门。
**跑之前重取一遍这 8 个值**：共享 checkout，别的会话随时在提交。）

剩下的代价：封包/上传/构建/激活约 4 分钟、产品重启、一个新 release id、以及 §7.3 那条
write-once 比对风险仍在。

**这就是"C：钉住的单变量 install"，它是一条正当的路。**它和方案 B 的差别只有一个：
B 改一次 ops 代码，换掉此后每一次这种交付的成本；C 不改代码，这一次付 4 分钟停服。


---

## 8. 执行与验收（2026-09-17 22:37–22:45，真机）

机主定的是"彻底修复"：不只这一次把码送上去，而是**这个坑以后不要再踩，且不该需要完整
install**。所以走 §4.2 的窄规则（方案 B），不走 install。

### 8.1 代码（`eidolon_ops`，工作站侧，不随 release 上板）

| 文件 | 改动 |
|---|---|
| `hostagent/secret_inputs.py` | 新增 `_converge_opaque_files`：只处理 `OPTIONAL_INSTALL_INPUTS` 里的文件输入，**只在目标不存在时创建**；已存在则不读、不比、不 chmod。报告新增 `added_files` / `missing_files` |
| `controller.py` | payload 新增 `declared_files`（由 `_convergeable_file_inputs` 给出，条件与 `stage_install_files` 相同——profile 没指码就不提供）；`restart_required` 带上交付的文件；`next` 为出厂码给出专门指引（重启 `bootstrapd`，不是笼统 `restart`） |
| `tests/test_credential_convergence.py` | 9 条新测试 |

两处散文同步改正：模块 docstring 原写 "will not create a file that is absent"，对 optional
文件输入已不成立，现区分"声明的 env 文件"（仍不创建）与"契约已称为可选的文件输入"（缺则补）。

顺带修掉一个本轮暴露的错误提示：`--apply` 跑完且无事可做时，`next` 原本仍说
"rerun with --apply"，读起来像失败。现在说"this Host holds every declared input"。

**变异验证**（原件存 scratchpad，每次立刻还原）：

| 变异 | 结果 |
|---|---|
| 拿掉"已存在就跳过" | `test_a_file_the_host_already_holds_is_never_touched` 红 |
| 拿掉 `OPTIONAL_INSTALL_INPUTS` 边界 | 越界测试红 |
| 拿掉 `declared_files` 校验 | 畸形声明测试红 |

全量 **1298 passed**，零失败。改动范围 Ruff 通过（仓里另有 2 条 lint 错误，一条是 HEAD
既有债、一条在另一会话的新文件里，均未触碰）。

### 8.2 交付

```
./eidolon pi5 converge-inputs          → host: planned,  missing_files: ['factory_setup_code']
./eidolon pi5 converge-inputs --apply  → host: converged, added_files:   ['factory_setup_code']
```

板上落地核验：

```
-rw------- 1 eidolon-bootstrap eidolon-bootstrap 9 Sep 17 22:37 factory_setup_code
sha256 6fc93e5e63c2563d68ff6f5e4df3fdadac6bd0636224b38ee13dd5b7d4d96405   ← 与工作站逐字节一致
/var/tmp/eidolon-secrets-*  → 不存在（stage 已清理）
```

**重启前窗口仍是 0** —— 印证了"码是惰性的，要 bootstrapd 读它"，也印证了 `next` 那句指引
不是客套话。

### 8.3 验收（§1.1 的对齐查询，不是任务给的那条）

`sudo systemctl restart eidolon-bootstrapd` 之后：

```
eidolon-bootstrapd[14846] INFO opened the factory claim window from
    /var/lib/eidolon-bootstrap/factory_setup_code

claim_state    : claimed        ← 未被改动，符合验收
OPEN sessions  : 1
session        : cfbd9954  created 2026-09-17T14:38:42.809275Z  expires_at = NULL（无时钟）
```

**再重启一次，证明它"站得住"而不只是"开过一次"**：

```
OPEN sessions  : 1
session        : cfbd9954  created 2026-09-17T14:38:42.809275Z   ← 同一个，未重复铸码
```

`_open_factory_window` 第三个条件（已有开着的就不铸）按设计生效。

`app-ready`：**`app_ready`，25/25 事实全绿**（`claim_window_honored: true`），20/20 服务 active。

### 8.4 幂等性（在已交付的板子上再跑一次 `--apply`）

```
host: already_current,  added_files: [],  missing_files: []
stat 前后完全一致：mtime 1789655871 / 600 / eidolon-bootstrap:eidolon-bootstrap / 9 字节
```

**已存在的文件一个字节都没碰。**这是代码保证的，不是运气。

### 8.5 这个坑以后还会不会踩

- **会被发现**：`claim_window_honored` 是 readiness 契约的正式一条，且随 ops checkout 注入
  到板子（§2），不等部署。
- **能被修复，且不用 install**：`converge-inputs --apply` 现在就是那个动词。
- **另一块板子同样覆盖**：`config/hosts/rk3588.toml` 也声明 `always_open` 并指向
  `.eidolon-ops/opi5max/inputs/factory_setup_code`，走同一条路。
- **不会误伤**：已持有该文件的 Host 是 no-op；profile 没指码的 Host 什么都不做；
  `host_identity.ed25519` 之类被边界明确拒绝。

### 8.6 留下的、没做的

- ~~红了之后没有任何地方说该跑哪个动词~~ **已做，见 §9。**
- ~~`doctor` 不带 readiness 事实~~ **已做，见 §9。**
- **`reopen_standing_claim_window`（被认领消耗后自动重开）这条路没有在真机验过**，因为它
  要真的完成一次 claim，需要手机。开机自动开窗这半已验。
- **opi5max 对照组仍未复验**（共用网线）。

---

## 9. 两个后续，当天做完（2026-09-17 23:xx）

机主要求这两件也在 main 上落地，不另开会话。

### 9.1 报红时说出该跑的动词

`ReadinessCheck` 增加可选字段 `remedy`。**大多数事实不填，这是刻意的**：
「Channel worker 够不到 LiveKit」的答案是去看，不是一条命令；给一个本来就帮不上忙的事实
编一个动词，和不说动词是同一种失败换张友善的脸——而且运维试过一次没用之后，下一条真的
建议也不会再被信。

只有 `claim_window_honored` 填了：`converge-inputs --apply` + 重启 `eidolon-bootstrapd`。
它也是这个字段存在的原因——这条事实刚加上时，唯一能修它的动词是整机重装。

投递到运维眼前的两条路（`describe()` 没有任何调用者，往那里写等于写进死代码，所以没动它）：

- `failed_facts(checks)` 把报告里为 false 的事实、它的含义、它的动词接起来，
  `app-ready` 和 `doctor` 都在报告里带 `failures`。**绿的时候不带这个键**——一个空的
  `failures` 会被读成"查过了，没事"，而缺席才是缺席。
- `describe_failures` 给认得的事实附上动词。这条字符串正是 authority-restore 拒绝时运维
  拿到的东西，也是他们最没工夫去查文档的时刻。

### 9.2 `doctor` 现在也问 readiness

`doctor` 现在跑 `app-ready` 动作并把结论并进自己的判定。两个刻意的选择：

- **不等待**（`settle_seconds=0`）。等四分钟让 Channel worker 注册，是发布切换该做的事；
  诊断该说"问它的那一刻是什么样"。为此 `target_payload` 增加了 `settle_seconds` 覆盖。
- **永不抛**。这是已经觉得不对劲才会跑的命令，探针答不上来是一条发现，不是"干脆没有报告"
  的理由——和它早就有的"容忍脏工作区"是同一条规矩。

但 **`unobserved` 不算健康**。`healthy` 要求 `readiness == "ready"`，不是"只要不是
degraded"。没问到就说健康，等于把这次要修的绿标题重新盖回去。

并且结论从 `host` 里**提到了 `HostController` 报告的顶层**。埋在四层下面的判定等于没有——
那只是把同一个失败换了个地方。不提供 readiness 的 adapter（本地 supervisord）什么都不加。

**为什么这样改是安全的**：`doctor` 不是任何人的门禁，只有 CLI 和 console 读它，没有任何
东西因它的判定而回滚。`_observe_app_readiness` 上那段伤疤——「a degraded App gate rolled a
Host back to a release that could not start at all」——针对的是**拿这个答案去做决定**，而
`doctor` 不做决定。另外 `HOST_SETUP_COMPLETABLE_STATES` 含 `absent`，所以"还没人认领"的
Host 本来就是绿的，不会因此误报。

### 9.3 验证

全量 **1306 passed**，改动范围 Ruff 通过。**6 次变异探针全部被咬住**：

| 变异 | 变红的测试 |
|---|---|
| `unobserved` 重新算作健康 | `..._will_not_call_a_host_healthy_it_could_not_ask` |
| `settle_seconds` 覆盖被忽略 | `..._takes_a_snapshot_rather_than_waiting_for_one` |
| `failures` 里不带 remedy | 两条（doctor + app-ready） |
| remedy 文案改错 | 同上两条 |
| 顶层提升被删 | `..._lifts_the_readiness_verdict_where_an_operator_will_see_it` |
| 提升改成无条件 | `..._invents_no_readiness_section_for_an_adapter_without_one` |

真机（pi5）：

```
./eidolon pi5 doctor
  status            : healthy
  readiness (顶层)  : ready / 25 facts        ← 以前这里什么都没有
  耗时              : 7.3s
```

**没有实地复现红色路径。**那要把板上的 `factory_setup_code` 挪开，而本仓反复记录的教训就是
不要手改板子上的文件。红路径由单元测试覆盖（含上表 4 条相关变异），且这块板子今天早些时候
本来就用同一个探针报过 `claim_window_honored: false`；**只有 remedy 文案是仅经测试验证的，
没有在真机上被人眼看过。**

### 9.4 顺带修正的一处散文

`READINESS_CONTRACT` 里 `claim_window_honored` 那段注释原写着「only an install delivers
that」。`25b0ad2` 之后这句话为假，已改。
