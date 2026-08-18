# 运维控制台

`eidolon-ops-console` 是同一套 Host 生命周期契约的第二个前端。它不是第二个 Ops：它构造同一个
`HostController`、声明同一份 `Plan`、渲染同一份 `Evidence`。它只补两件终端给不了的事——**一个还在跑
的步骤会说它在跑**，以及 `plans.py` 从第一天就写着要给的那个**确认梯度**。

```bash
uv sync --all-extras          # 后端依赖（fastapi、uvicorn）
cd web && npm install && npm run build   # 界面，一次就够，除非改前端
uv run eidolon-ops-console    # http://127.0.0.1:9010
```

默认读 `config/hosts/*.toml`（跳过 `*.example.toml`）。`--config HOST.toml` 可重复，指定单个 profile；
`--profiles DIR` 换目录；`--port` 换端口（默认 9010，在产品端口表之外）。

## 它运行在哪里，以及为什么没有 `--host`

只监听 `127.0.0.1`，没有绑定地址开关。这个进程能装 release、能永久删除权威数据、能吊销每一台在管的
手机——这套按钮属于操作者自己的机器。另一台机器要这套按钮，就需要它自己的 checkout、自己的 SSH key
和自己的 profile，而这正是 Ops 已经画好的边界。没有用户体系，也没有 token：能在这台机器上打开
loopback 端口的人，本来就能直接跑 `eidolon-ops`。

## 它不持久化任何东西

一次 run 活在这个进程里，随进程消失。Host 上发生过什么，权威是 Host 自己的收据和 release 事务的
证据；控制台若留一份自己的副本，就是同一个问题的第二个答案，而且是过期得最快的那个。

## capability 驱动，不是平台驱动

`GET /api/hosts` 返回的每一台 Host 都带着 `HostAdapter.describe()` 的结果，操作面板只提供
`capabilities` 里有的那些。Mac 上没有 `install`，Pi 上没有 `debug`——这不是前端写死的判断，而是
Host 自己回答的。加第三种板子不需要改这一侧。

profile 每次请求都重新读盘，不缓存：profile 是操作者在两个操作之间会编辑的文件，快照会让控制台描述
一台已经不存在的 Host。

## 确认梯度

服务端按参数产出的 `Plan` 决定要多少确认，浏览器只负责渲染这个要求：

| 计划 | 要求 | 例子 |
|---|---|---|
| `applies=false`，或 `touches` 为空 | 无 | `status`、`doctor`、`install`（未勾 apply）、`restart --dry-run` |
| 会改动、`destructive != irreversible` | 一次显式勾选 | `deploy --activate`、`restart`、`backup`、`init-inputs` |
| `destructive == irreversible` | 勾选 + 手输该 Host 的 id | `reset --apply`、`controller-reset --apply`、`install --wipe-authority-data`、`init-inputs --new-identity` |

强制在服务端，不在浏览器：界面是梯度**被展示**的地方，不能是它**被执行**的地方，否则绕过它只差一次
fetch。未确认的变更返回 `428`，手输的 id 与目标 Host 不一致同样返回 `428`。

一台 Host 同一时刻只允许一个会改动它的 run（`409`）；只读的 run 不受此限，也不会被它挡住。run 不能
取消：Ops 的边界动作是可续传的，不是可中断的——把一个跑到一半的 release 事务打断，正是让 Host 落到
任何计划都没描述过的状态的办法。控制台给的是让人想取消的那件事的解药：知道现在卡在哪一步。

## 进度是怎么来的

长操作本来就在维护一份 phase 列表——就是 `steps_from_phases` 用来和计划配对的那一份。以前它只在全部
结束后才随报告一次性出现。`eidolon_ops/progress.py` 把这份列表换成 `Journal`：一个会把自己的条目播报
出去的 `list`。

- 没有 sink 时，它就是原来那个 list，条目、顺序、值都不变——现有 CLI 与全部测试看到的就是这个；
- 有 sink 时，同一批条目在发生的当下到达，并且一个 phase 还会在**开始时**说一声，因为二十分钟的发布
  期间有用的问题是"现在在跑哪一步"，不是"哪些跑完了"。

sink 从 `HostController(progress=...)` 经 `build_adapter` 注入到 `EidolonPiController` /
`ReleaseTransaction` / `SupervisordSupervisor`。`plans.py` 里的 step id 就是 phase 名，所以界面在动工前
就能画出骨架：计划声明的步骤先摆好，实际发生的 phase 逐个点亮。计划没声明过的 phase 也照样显示，但标
成计划外——它是真实发生的工作，而这个不对称是诚实的那一侧：计划才是被批准过的东西。

`install` 中途的 foundation 安装只播报开始、不记入 phase 列表：phase 列表用的是计划的词汇表，而计划没
给它命名，一个叫 `install` 的 foundation phase 出现在 `install` 操作中间会把两件事叫成同一个名字。

## HTTP 表面

| 端点 | 作用 |
|---|---|
| `GET /api/health` | 进程活着、管着哪些 profile、界面是否已构建 |
| `GET /api/hosts` | 每台 Host 的 profile、组合、capability、是否有 run 在跑 |
| `GET /api/hosts/{id}` | 上述加上该 Host 可用的操作目录与就绪事实的说明表 |
| `POST /api/hosts/{id}/plan` | 纯函数：出计划与确认要求，不接触 Host |
| `POST /api/hosts/{id}/runs` | 校验、要求确认、起一个 run（`202`） |
| `GET /api/runs?host_id=` | run 列表（不含报告） |
| `GET /api/runs/{id}` | 单个 run，含计划与证据 |
| `GET /api/runs/{id}/events` | SSE：先回放已发生的，再跟进实时的 |

事件种类：`run.started`、`phase.began`、`phase.recorded`、`run.finished`。每个 phase 事件都带完整的
phase 列表，浏览器因此不需要自己实现一份状态机——用 TypeScript 复刻服务端的 phase 模型，漂移的总是副本。

`run.status`（`running` / `completed` / `failed`）说的是 run 有没有发生，`run.outcome` 才是 `Outcome`
的裁决。一次读到降级 Host 的 `status` 是 `completed` + `degraded`：它做完了被要求的事并把答案带回来了，
把这叫失败会让界面上的红色同时表示两件事，而操作者需要行动的是那件没跑完的。

## 秘密

每份报告本来就被设计成不携带 credential 值（每一份都自己写着 `redaction: ...`）。控制台在此之上再过一
道键名遮蔽——`*_token`、`password`、`private_key` 之类一律替换。这不是替代品，是那种不花钱、但万一某个
组件某天在报告里放了不该在浏览器标签页里出现的键时能挡住的检查。路径不算 credential：`secret_staging`
是一个路径，遮蔽它只会遮掉证据。

`commissioning-code` 的 `setup_code` 是例外：签发它就是这个操作的目的。它照原样返回，界面默认打码、
点一下才显示，并且不写到任何地方。

## 改前端

```bash
uv run eidolon-ops-console            # 后端留在 9010
cd web && npm run dev                 # 9011，/api 代理到 9010
```

`npm run build` 的产物落在 `src/eidolon_ops/console/static/`，不进 Git——它是构建产物，会腐烂，也会把
每次 diff 撑大。没构建时打开控制台会得到一页说明和那两条命令，而不是一个空白的 404。
