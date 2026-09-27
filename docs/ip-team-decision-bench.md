# IP Team 决策契约台架

此配置仅用于 opi5max 开发台架。决策模型尚未接入，只有决策 HTTP 接口使用输入匹配 fixture；成员回复、ASR、TTS、LiveKit 与设备回执使用真实实现。fixture 不具备通用语义理解，未匹配或歧义输入返回 abstained。

## 生命周期

1. 从已提交的 Agent 启动 `python -m scripts.participation_fixture --cases <文档仓>/IP团队/evidence/semantic-contract-20260927/decision-cases.json --port 8772`。监听地址固定为工作站 loopback。
2. 使用运维已信任的 SSH 身份连接 Host，建立 `-N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -R 127.0.0.1:8772:127.0.0.1:8772` 隧道。必须使用该 Host 的 known_hosts 和严格主机密钥校验。
3. 从 Host 检查 `http://127.0.0.1:8772/openapi.json` 可达，再通过官方 Ops deploy 发布兼容组件。rk3588 的 settings overlay 给 Agent 设置完整 participation.url，生产模板由固定提交读取，不手改远端 YAML 或数据库。
4. Mobile 配置本场角色为用例中的名称；名称到真实 Companion ID 的映射由请求候选决定，不绑定任何板型、MAC 或选择次序。
5. 验收结束可以停止隧道和 fixture；此后团队输入会明确失败，不回退固定轮换。若要持续使用台架，必须保持这两个进程运行。现有的普通陪伴不依赖该端点。

fixture 属于测试进程，不是新产品 systemd 服务；不得将它当作生产语义模型。模型就绪后，替换 participation.url（必要时配置 token）并通过 Ops 发布，无需修改 Agent/Channel 的团队编排逻辑。

## 验收边界

Mobile 启动/结束及 Host 服务健康不能证明语音链路通过。完整验收需要真实按住说话，核对 ASR 文本、决策请求、真实成员回复、TTS、设备播放完成回执和下一步决策。fixture 精确匹配文档中的原始输入，ASR 的标点差异也可能导致弃权；这种情况记录为 fixture 覆盖差异，不能改业务流程去补说。

2026-09-27 配套版本：SDK 7fd4ab3、Agent 998806e、Hub 6cf3a15、Channel dfd782d、Admin 92d2b52、Mobile b33fdbf、ESP32 4b6d4023。Host 发布标识为 rk3588-semantic-team-20260927-2；实际激活及真机结果以发布日志和 IP 团队证据文档为准。

使用真实 Companion 展示名“小方／小栈”验收时，可将用例路径换成同目录 `bench-decision-cases.json`。这是 fixture 输入数据，不是产品角色或设备身份配置。
