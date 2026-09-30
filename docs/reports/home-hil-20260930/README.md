# 智能家居真机测试版本

2026-09-30，用户授权固定版本并安装手机、Korvo、opi5max。此版本用于工程实测，尚未通过客户 demo 验收。

## 已完成

- opi5max：`rk3588-home-hil-20260930-1` 已激活，release doctor=healthy，app_ready=app_ready。九个组件均显式固定提交；只推进 Agent 到 `b0124c5`，其他组件沿用已运行的 c4 发布版本，未打包其他工作区改动。完整提交见 `versions.json`。
- Agent 运行进程确认加载 Laya 单句与 `7b695ba8` 续接开关，地址为板上 `127.0.0.1:8771`；NPU readyz=ready/backend=rknn，默认解释预算 1000ms。参与决策配置不变。
- 通过现有 `/etc/eidolon/agent.env` 配置入口开启上述三项非敏感设置，本机 Ops 私有输入同步；未修改凭据。部署前配置分别保存在板上 `/root/agent.env.before-home-hil-20260930` 与本机 `.eidolon-ops/opi5max/agent.env.before-home-hil-20260930`。Ops deploy 不传输 env，故这一步独立于代码发布，回滚代码时也应恢复这三项配置。
- 手机：`2d183d7` debug APK 以 install -r 安装成功并启动，保留数据；实际界面确认已安全连接 opi5max，能显示家居目录。旧 pi5 条目的身份不匹配提示与本次 opi5max 连接无关，未删除用户旧记录。
- Korvo：`a1772c864`，标准 Korvo 脚本构建/刷写/分区回读/启动指纹校验通过，owner_trust=ready，IDF 6.1。未擦除 NVS 或信任。后续经用户物理开窗、手机标准配网与认领，已切到 opi5max；见下文。
- 板上原家居目录为空，通过既有示例公寓 API 装载 18 个虚拟设备、7 个区域和 4 个场景；手机已读到。执行 Provider 为 virtual，没有实体家电执行器。

APK 与应用固件的固定副本保存在 `.eidolon-ops/frozen/home-hil-20260930/`，SHA256 见版本文件。服务检查见 `readiness.json`；仅保存本次测试话语的阶段日志见 `smoke.log`。

## 部署后冒烟

直接调用板上 Agent 标准命令入口，复用同一测试 session；不是麦克风端到端测试。

| 话语 | 结果 | HTTP 总耗时 |
| --- | --- | ---: |
| 打开客厅窗帘 | Laya 单句，打开 | 581ms |
| 关闭客厅窗帘 | c4 follow，关闭 | 371ms |
| 打开它 | c4 follow，打开同一窗帘 | 382ms |
| 算了 | LLM 兜底，取消，无新设备操作 | 1348ms |

日志确认 follow 的 model=7b695ba8；取消轮使用 DeepSeek-v4-flash。结果不能推广为 p95/p99，也不包含 ASR、TTS、网络音频及屏幕回执。

## Korvo 接入完成与待现场语音测试

用户长按 SET 开窗并在手机输入 Wi-Fi 密码，15:05:48 手机日志确认配置事务 committed 及 terminal ACK。手机随后等待设备登记；串口诊断打开触发设备重启后，设备完成 EnrollmentProposal、ClaimGrant ACK，并连接 `ws://192.168.3.230:7880`。手机显示“设备已接入这台主机”。未篡改 NVS、伪造 Owner 目录或绕过认领。

串口证据：启动约 16.5 秒取得 approved 配置，17.3/23.0 秒出现两次 Reconnecting，26.2 秒 Connected、operational_ready=1、UI READY，26.4 秒收到 revision=1 的家居快照（18 设备、7 区域、4 场景）。节选见 `korvo-admission.log`。首次配置提交后的自动登记为何没有及时推进尚未定因；不能把重启后恢复视为首次接入体验已验证无问题。

当前可开始现场语音测试，配网成功和面板同步不代表 ASR/TTS、语义正确性及长尾延迟已验收。

Korvo 已确认指向 opi5max、通道连接及面板同步。现场语音测试顺序：

1. 明确单句控制与设备状态回显。
2. “打开客厅窗帘 → 关闭客厅窗帘 → 打开它”。
3. 模糊目标的澄清、选择及改动作。
4. 取消、取消后的新请求、会话结束后重新开始。
5. 记录异常原话、时间与屏幕结果；结合已有分阶段日志区分识别、理解、执行、播报问题。

未来请求/转述的误执行、c4 已知错误取消仍未闭环，详见 Agent `docs/reviews/2026-09-30-home-npu-integration/README.md`。本次没有增加模型语义补丁，也没有宣称这些问题解决。多角色 demo 不在本轮范围。
