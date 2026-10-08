# 智能家居对接：Mac 源码栈基准（2026-10-03）

非真机证据。Mac 源码栈（Hub `53a2d0d`、Agent `5ffd406`、Channel 变化流提交前后）、Home Assistant Core 2026.2.3 台架（demo 集成）、周边好生活协议模拟云。所有数字来自 Hub 回执的四段时间戳（`receipts` 接口）与调用方往返计时，由 `eidolon_hub/devtools/smarthome_demo.py bench` 与 `eidolon_agent/scripts/bench_home_hub_e2e.py` 生成，原始样本在同目录 JSON 中。样本量 10–20，不是生产 p95。

## 1. Hub 执行回执分段（每台设备 20 次 on/off 交替）

| 设备 / Provider | 状态 | 往返 p50 / p95 | Hub 受理 p50 | Provider 段 p50 / p95 | 完成 p50 / p95 |
|---|---|---|---|---|---|
| 厨房灯 / Home Assistant（事件确认） | 20/20 `succeeded` | 74.9 / 81.2 ms | 37 ms | 2 / 3 ms | 39 / 43 ms |
| 书房空调 / Home Assistant（n=10） | 10/10 `succeeded` | 80.9 / 83.5 ms | 40 ms | 2 / 4 ms | 43 / 45 ms |
| 主卧吸顶灯 / 周边好生活（委托，模拟云） | 20/20 `delegated` | 48.1 / 58.0 ms | 45 ms | 2 / 6 ms | 47 / 56 ms |
| 客厅主灯 / virtual | 20/20 `succeeded` | 46.5 / 59.0 ms | 42 ms | 1 / 7 ms | 44 / 57 ms |
| 厨房灯 / Home Assistant，**注册表读取 1 s 复用后**（Hub `53a2d0d`） | 20/20 `succeeded` | **4.3 / 6.2 ms** | 1 ms | 2 / 3 ms | 3 / 4 ms |

结论：Provider 段本身在本机 ≤ 7 ms（demo 设备瞬时响应，真实米家云路径预估 0.3–1.5 s 待真机）；此前每次执行都向 Data 读一遍注册表，占去约 40 ms，复用一秒后一次命令 Hub 内约 3 ms。

## 2. 文本链路端到端（Agent 规则解释 → Hub → Provider，不含 ASR/LLM/Companion 校验）

`agent_text_e2e.json`，7/7 通过：

| 话语 | 结果 | 耗时 | 卡片文案 |
|---|---|---|---|
| 打开厨房灯 | executed | 135.5 ms | 已打开厨房灯 |
| 关闭厨房灯 | executed | 129.9 ms | 已关闭厨房灯 |
| 厨房灯调到70 | executed | 196.7 ms | 厨房灯 已调到 70% |
| 把书房空调调到24度 | executed | 189.3 ms | 书房空调 已设为 24°C |
| 书房空调调成制热 | executed | 210.8 ms | 书房空调 已切换到制热 |
| 打开大厅窗帘 | executed | 199.7 ms | 已打开大厅窗帘（窗帘已在 100，立即确认） |
| 关闭主卧吸顶灯 | executed | 140.8 ms | 主卧吸顶灯 已交给平台，平台回复：好的，为您关闭主卧吸顶灯 |

耗时含两次 Hub 快照读取（注册表复用前）。最后一行是委托型设备：Agent 没有说「已关闭」。

## 3. 其他实测

- 导入覆盖层：把三台 HA 设备改成中文名后再同步，`updated 0`，名字保留（`Device.overrides=["name"]`）。
- 慢设备：Hall Window 70→100 在 3 s deadline 内未到位 → 回执 `unknown`；观测流随后记录 position=100；Hub `261f57c` 起这类回执会被观测自动回填为 `succeeded`（单测覆盖，台架重现待慢速 demo 设备）。
- 面板推送：Channel 变化流 watcher 单测 12 项通过；真机面板收 delta 列入真机清单 A2。
