# IP Team 决策契约台架

2026-09-29：opi5max 已通过 Ops 发布 `rk3588-p4-route-20260929-1`，Agent
使用板上 p4 的 `http://127.0.0.1:8773/v1/participation/decide`。
doctor / app-ready 通过；参与 LLM 兜底未启用，拒答仍结束本轮。
见[路由与推理验证](reports/p4-route-20260929/README.md)。

以下保留早期 fixture 契约台架操作，仅用于显式测试。当前 Host 不依赖8772隧道。
fixture 不具备通用语义理解，未匹配或歧义输入返回 abstained；不得作为模型故障的自动回退。

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


2026-09-27 停播故障隔离修复：Host 已通过 Ops 激活 `rk3588-semantic-team-20260927-3`，SDK `df7d9e3`、Agent `29af959`、Channel `947de3a`，其余组件沿用上一版本明确 pin。doctor/app_ready 通过，无数据库迁移。Stop 的 capture_id 与 Receipt 的 error_code 需要这三个组件协调发布；本次没有 Mobile/ESP32 更新。506 项回归与跨项目 TCP 恢复测试通过，不能替代物理打断验收。

2026-09-27 续期恢复修复：Host `rk3588-semantic-team-20260927-4`、Channel `692d687` 已激活，其他 Host 组件沿用上一发布 pin。Mobile `6f22185` 已保留数据安装。三设备自动重连、旧团队退出确认、新团队开始/就绪/结束均已通过；ESP32 未刷机，数据库无迁移。此项不等于发声中打断验收。
