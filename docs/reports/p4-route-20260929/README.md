# OPi5Max p4 路由验证

## 配置与发布范围

将 `config/eidolon-rk3588.toml` 的 `participation.url` 从工作站测试 fixture
8772 改为板上 p4 8773。所有组件固定为上一正式发布
`rk3588-laya-participation-20260929-1` 的提交，见 `source-revisions.json`。
候选发布为 `rk3588-p4-route-20260929-1`，通过现有 Ops prepare / activate 流程。

已激活：Ops 返回 status=activated，release doctor=healthy，app_ready=app_ready。
激活后读取 `/etc/eidolon/agent.yaml` 的地址，通过该地址调用部署版 Agent 适配器，
五类决策各一个，5/5与先前结果相同，无请求错误。见 `configured-route-probe.json`。
这是配置与适配器到模型的验证；没有模拟运行中 CoordinationSession 或真实语音输入。

注意：板上 Agent 为 a09f583，只有兜底组合器；完整 LLM 决策器及默认关闭配置
在 f01bbcd，本次未发布。不能把“组合器已上板”称为“完整 LLM 兜底已上板”。
家居仍用 rules；本次只直接调用家居推理服务进行并发测量。

## 切换前推理探针

使用板上部署版本 Agent 的 HttpParticipationDecision / LayaInterpreter，复用
SDK 校验及 HTTP 客户端。参与样本取正式 p-dev 回放中每类前三个：respond、
clarify、wait、finish、abstained，共15个；比较完整 SDK 结果与先前正式部署记录。
家居使用 SDK apartment 合成目录，三条话语循环，预算800ms。没有设备执行。

| 服务 / 模式 | 样本数 | 中位数 ms | 最大 ms | 请求错误 |
| --- | ---: | ---: | ---: | ---: |
| 参与 / 单独 | 15 | 811.60 | 1241.10 | 0 |
| 参与 / 与家居并发 | 15 | 904.43 | 1352.10 | 0 |
| 家居 / 单独 | 6 | 442.51 | 461.95 | 0 |
| 家居 / 与参与并发 | 15 | 499.91 | 543.60 | 0 |

参与两组均15/15与先前结果相同。家居报告 backend=rknn、revision=45f3dedb。
并发是每轮一个参与请求与一个家居请求同时发起，轮间间隔250ms；不是饱和压测。
家居不存在的卧室窗帘返回 target_status=none，不能把 HTTP 成功算作设备执行成功。
原始结果见 `inference-probe.json`。样本用于接口和低并发冒烟，不是独立准确率、
生产p95/p99或端到端语音验收。

## 后续分工

- Codex：Agent 路由、状态/取消与发言许可、参与 LLM 兜底完成度语义验收。
- Claude Code：Models 模型与 NPU 服务；并发/取消恢复性能；同时通过单句和续接回归的家居候选。
- Ops 持有发布配置，Hub 持有设备执行权威。模型不维护第二份会话状态。
- 家居模式另行切换与验收，不能把端口8771或阈值0.8视为已经支持上下文。
- 真机后续核对 ASR、决策、回复、TTS、播放回执和打断；保持参与 LLM 兜底关闭。
