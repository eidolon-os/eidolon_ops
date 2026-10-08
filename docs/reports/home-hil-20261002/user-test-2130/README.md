# 2026-10-02 21:30 Korvo 五轮链路分析

范围：用户确认的「打开电视 → 调大音量20% → 关了它 → 开音箱 → 关了它」。北京时间 21:30:55.911–21:31:13.007 为最终转写时间，会话 `esp32-a496b482-a793b185-00000001`。只读历史日志、数据库与运行元数据，没有重放模型请求、执行家电命令、变更配置或重启服务。

## 结论

- 这五轮的服务端理解、上下文转移、执行结果与最终状态符合预期。5 次 Laya 请求，4 轮接管，1 轮追加一次 LLM；5 个 executed、5 次 Hub execute 200、5 次 Channel delivered，没有观察到额外执行或模型超时。
- Laya 运行在 opi5max 本机 `127.0.0.1:8771`，c4 `7b695ba8`，实际 backend=rknn/device=rk3588-npu。没有向 Mac Laya 发请求。服务耗时 272–440ms，符合已有 1000ms 解释预算。
- Laya 接管的 Agent 总耗时 399–585ms；VAD 结束到服务器完成回传 636–895ms。切换到音箱的一轮为 1560ms，其中理解 1104.7ms，主要是先续接、再 LLM 的顺序路径。1 秒 Laya 预算不能用于宣称整条语音链路达标。
- 本次控制的是 Hub 的 virtual 家电 Provider。设备状态已核对，但没有这五轮 Korvo 接收/绘制 ACK，因此 delivered 只代表服务器完成转发，不能当成屏幕显示时刻。五轮不能证明 p95/p99 或全部客户 demo 场景通过。

## 版本与身份边界

板上发布为 `rk3588-redeploy-20261002-1`，Agent `c22ddd1`、Channel `e68c152`、SDK `c375645`、Hub `18be4d7`、Models `646cbc2`。Agent、Channel、Hub 的四份关键路径源码哈希与本地分析源码一致。版本描述、RKNN 文件实际哈希及选定配置见 `version.json`、`details.json`；这些文件没有包含凭据。未以 manifest 声明的 Torch 权重哈希冒充对板上 Torch 权重的实际校验。

Korvo 当前设备为 `device-instance-d694b066b052cec0b813a2b699af99780c55b29796a9b6ce9fabe22299a2b795`，挂载有效。Kernel body assignment 在 21:30:33.649 更新：`selection_provenance=user_selected`，绑定 `c_129153685f855ff3b2062301fb3ceda0`，Owner 一致。Channel Provider 授权用途为 `home.command.v1`；会话在 21:30:52 启动，五轮 Agent 都记录同一 Companion。

每轮身份校验 13–21ms。家居仍走独立 SmartHomeApplication，复用身份授权，没有进入陪伴 TurnEngine；本会话及这五个 turn 在陪伴 `runtime_sessions` / `turns` 表中无记录，这是独立家居路径的预期行为。Companion 的 kind=conversational 不表示家居需要其 prompt 或 memory。

21:31:15 Provider 关闭会话，Channel 后续记录 user_left，并在 21:31:18 调用家居 session/end 返回 204。session_end 通知因房间已关闭未发送发生在五轮 delivered 之后，不是命令失败；后续 shutdown callback 重复清理不代表额外执行。

## 调用链路

Korvo 麦克风 → LiveKit → Channel 百炼 FunASR + FireRed VAD → 最终转写 → Agent `/api/admin/smarthome/command` → 验证 Owner/Companion/device/session → 当前设备目录与有界家居上下文 → Laya `/v1/systemone` → 必要时 DeepSeek-v4-flash → SDK Proposal / 设备能力校验与 plan_action → Hub `/api/smarthome/v1/execute` → 对应 virtual Provider → 执行状态 → Agent VoiceResult → Channel Provider `/v1/smarthome/result` → LiveKit 面板结果与最新状态。

家居媒体 Agent 为 `llm=None, tts=None, audio_output=False`。Channel 的全局配置日志出现 tts=bailian 不代表这五轮调用了 TTS；本链路回复来自执行结果汇总。业务 Agent 的 LLM 兜底只调用了第4轮。

Hub 的幂等记录在运行时内存中，SQLite 的 virtual_devices 保存当前状态，没有逐轮动作历史账本。本次没有进入进程读取内存，也没有用重放动作获取幂等回执。下表区分历史日志原值与依据固定部署代码重建的命令。

## 每轮理解与设备执行

|轮|最终转写|Laya 已记录的输出与含义|LLM 已记录的输出|重建命令与实际回复|
|---|---|---|---|---|
|1|打开电视。|单句三题；日志 Proposal 为 control/resolved，targets=[living.tv]，action={trait:on_off,command:on,slots:[]}。三题原始概率未保存|未调用|living.tv / on_off.on → 已打开电视|
|2|调大音量20%。|follow，p=.9958，接受；目标沿用电视，动作/数值按现有映射解析|未调用|living.tv / volume.step，delta=20 → 电视 音量已调到 70|
|3|关了它。|follow，p=.9934，接受；「它」沿用电视|未调用|living.tv / on_off.off → 已关闭电视|
|4|开音箱。|follow，p=.9651，未接管；日志原因 continuation_not_accepted。原始 choice 未保存，不能把该原因写成低置信度或确切「重新理解」|proposed，intent=control，target_status=resolved，targets=('living.speaker',)，action=('on_off','on')|living.speaker / on_off.on → 已打开智能音箱|
|5|关了它。|follow，p=.9953，接受；目标沿用上一轮 LLM 成功操作的音箱|未调用|living.speaker / on_off.off → 已关闭智能音箱|

第二轮的 delta=20 来自部署版本的词法与 SDK 映射，历史 execute HTTP body 没有保存。当前音量契约为 0–100，20% 表示增加 20 个刻度，回复与最终状态为 70；不能理解为按当前音量乘以 1.2。操作前 50 是由该映射及结果推导，未留本轮前置快照。

续接只对上一次成功操作的目标判断，包含「重新理解」出口。第4轮是从电视换到音箱，未接管后，现有流程直接交给 LLM 完整目录与上下文，没有再调用一次 Laya 单句。p=.9651 高于 .95；拒绝不一定源于低置信度，出口选项或动作映射也能导致拒绝。现有记录不足以精确归因。

第5轮证明 LLM 成功执行会更新家居焦点，下一轮仍能返回 Laya；没有「有上下文就全部交给 LLM」或「一旦进 LLM 就一直走 LLM」。

只读最终状态与最后写入时间对应本轮：

- `living.tv`：`on=false, volume=70, muted=false`，最后写入 21:31:04.709（第3轮）。
- `living.speaker`：`on=false, volume=20, muted=false`，最后写入 21:31:13.417（第5轮）。

电视与音箱在 System Data 的 provider 均为 virtual。实体语音终端已连通，但本报告不证明真实电视或音箱执行了操作。

## 分阶段耗时

单位 ms。VAD 结束是服务器检测事件，不是物理麦克风的精确语音结束；delivered 是服务器完成转发，不是设备绘制 ACK。使用 Channel 行内事件时间，避免 journald 入队延迟污染差值。Laya 日志没有 turn_id，按唯一请求顺序和时间窗关联；不能称其具备跨服务完整 trace ID。

|轮|Laya 服务 total|续接阶段|LLM 流结束|理解合计|Hub 执行阶段|身份校验|Agent 总计|VAD→最终转写|最终转写→delivered|VAD→delivered|
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
|1|440|—|—|468.6|26.2|21|585|246|649|895|
|2|272|282.3|—|283.5|30.9|17|400|172|464|636|
|3|276|282.7|—|283.3|28.6|21|399|248|456|704|
|4|274|283.9|818|1104.7|23.9|13|1228|275|1285|1560|
|5|296|301.4|—|301.7|26.1|16|401|352|462|814|

理解合计包含续接/LLM，不能将表中这些列再次相加。Agent 总计还包括身份校验、目录与上下文等开销。Hub 执行阶段 23.9–30.9ms，Provider 回传结果并刷新面板快照的 HTTP 服务耗时 37.8–46.7ms，均不是主要瓶颈。四轮 Laya 接管 Agent 中位 400.5ms；五轮总体中位 401ms，仅为本样本统计。

Laya 服务第1轮 q=3/tokens=460，其余 q=1/tokens=189/196/188/192；全部 skipped=0、truncated=-，HTTP 200。forward=435/270/274/272/295ms，包括实际后端与 CPU 头部等处理，不能作为独立 NPU 内核耗时。启动记录确认 RKNN、FP16、NPU buckets=128/256/384/512，smart_home 配置使用核心1/2；FireRed 日志中的 CPUExecutionProvider 指 VAD，不是 Laya 退回 CPU。这些请求发生在启动约28分钟后，不能用于宣称冷启动性能。

### 第4轮 LLM

DeepSeek-v4-flash 完成一次工具提案：connect_ms=264，raw_ttft_ms=298，首个有效工具增量=623，tool_ready_ms=804，stream_end_ms=818；输入4232、输出93、缓存输入2304 token。

这些是从请求开始计的累计时间，不可相加。connect_ms 不是独立 TLS 握手耗时，configured_max_retries=2 是配置上限，不是发生两次重试。Laya 续接约284ms + LLM约818ms，解释合计1104.7ms；执行仅23.9ms。主要等待发生在理解链路，不能归因于 NPU 未工作或设备执行慢。

## 是否符合预期与后续范围

本轮基本控制、相对音量、指代、跨设备后更新焦点、共用 Companion 身份和独立家居执行边界符合预期。五次最终转写均与确认的用户意图一致；中间识别曾出现「挂」「Can you」等临时片段，最终修正后才执行，没有观察到临时片段误触发。

性能只能确认 Laya 的1秒预算通过、快路径在本样本低于1秒；若按此前800ms整链路目标，第1/5轮分别895/814ms，不能算全部达标，LLM轮为1560ms。不能依据本样本擅自调整整链路预算。

仍缺这五轮的原始 Laya choice/完整响应、LLM 原始工具参数串、逐轮执行请求体及终端接收/绘制 ACK。interpretation_record_path 当前为空，标准 Channel timeline 也没有本家居会话记录；本次保留缺口，没有新增观测框架或打开全量敏感上下文记录。

下一步沿用既有真机验收清单覆盖澄清选择、取消、更换动作、多设备及否定/转述；本轮不产生新的实现需求。若要优化切设备等待，应先评审现有有界续接与完整目录理解的职责，不能增加针对「音箱」「电视」的特例绕行。全部场景与终端回执完成验收后才能收敛客户 demo。

证据：`evidence.json` 为固定窗口筛选日志和状态表结构/结果；`details.json` 为最终/临时识别、绑定和发布元数据；`version.json` 为模型进程、发布 SDK、RKNN 文件与关键源码哈希；`turns.json` 为计算后的逐轮明细及证据等级。
