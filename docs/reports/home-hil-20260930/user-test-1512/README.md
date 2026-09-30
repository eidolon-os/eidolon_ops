# 2026-09-30 15:12 真机八轮分析

范围：Korvo 会话 `esp32-3b0f2d19-3e082a1c-00000001`，最终转写时间 15:12:57.932–15:13:31.581（北京时间）。8 次 Laya 请求，2 次额外 LLM 请求，8 次 executed。未重新调用模型、重放动作或更改配置。

## 调用与执行

Korvo 麦克风 → LiveKit → Channel 的百炼 FunASR 流式识别 + FireRed VAD → 最终转写 → Agent `/api/admin/smarthome/command`（owner/device/session/turn）→ Laya `/v1/systemone`（板上 RKNN c4）→ 必要时 DeepSeek-v4-flash 工具调用 → SDK 提案校验与 plan_action → Hub `/api/smarthome/v1/execute` → 注册设备的 virtual Provider → Hub 返回执行状态 → Agent 生成 VoiceResult → Channel Provider `/v1/smarthome/result` → LiveKit 面板消息与状态快照 → Korvo。

Hub 持有 provider、Owner 范围校验、命令能力校验、截止时间及幂等；Agent/模型不直接操作设备。本轮控制的是虚拟窗帘和电视，不是实体继电器。测试后只读 Hub snapshot：`living.curtain.position=100`，`living.tv.on=false`。

家居会话明确配置 `llm=None, tts=None, audio_output=False`；这里的 llm=None 指 Channel 语音 Agent，不影响业务 Agent 的 LLM 兜底。本轮没有 TTS，显示文字来自执行结果汇总，不是大模型自由生成的回复。

## 耗时（毫秒）

Laya 服务列是服务日志 total，非纯 NPU forward。Agent 总耗时含目录/上下文读取、理解与执行。最终转写到 delivered 是服务器完成结果转发，不是屏幕绘制 ACK。VAD 结束也不是物理麦克风上的精确语音结束。

|轮|最终识别|Laya 服务|LLM 流全程|理解|执行|Agent 总计|VAD结束→最终转写|最终转写→delivered|VAD结束→delivered|
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
|1|关闭窗帘。|444|—|454.4|26.5|559|187|713|900|
|2|打开窗帘。|270|—|277.5|37.8|386|225|447|672|
|3|关闭窗帘。|287|—|293.8|31.9|419|209|470|679|
|4|打开它。|283|—|296.5|46.9|425|628|503|1131|
|5|关闭它。|269|—|281.3|30.4|387|306|457|763|
|6|打开电视。|290|1094|1395.9|39.3|1508|230|1609|1839|
|7|关了它。|266|—|273.1|26.4|356|270|411|681|
|8|打开窗帘。|294|1134|1443.7|47.9|1575|235|1630|1865|

## 每轮模型输出与命令

以下明确区分日志原值和依据固定版本映射得到的执行含义。完整原始 HTTP 响应没有保存；运行时 interpretation recorder 未开启。续接日志遗漏了 choice，只记录最大项概率。因此无法给出第6/8轮的确切 choice，不能把重新调用模型的结果冒充原始结果。

|轮|Laya 记录及可确认含义|LLM 实际已记录的结构化结果|最终设备命令与回复|
|---|---|---|---|
|1|单句三题；日志 Proposal 为 control/resolved，targets=[living.curtain]，action={trait:position,command:close,slots:[]}；原始三题概率未存|未调用|living.curtain / position.close → 已关闭客厅窗帘|
|2|follow，p=.9767，接受；映射含义为打开既有窗帘|未调用|living.curtain / position.open → 已打开客厅窗帘|
|3|follow，p=.9860，接受；映射含义为关闭既有窗帘|未调用|living.curtain / position.close → 已关闭客厅窗帘|
|4|follow，p=.9900，接受；“它”沿用窗帘|未调用|living.curtain / position.open → 已打开客厅窗帘|
|5|follow，p=.9883，接受；“它”沿用窗帘|未调用|living.curtain / position.close → 已关闭客厅窗帘|
|6|follow，p=.9689，但未接受；旧焦点=窗帘，新请求=电视；不是超时，也不是低于.95。未存 choice，不能断言是哪一个拒绝出口|propose_home_action：control/resolved，targets=('living.tv',)，action=('on_off','on')；无完整原始参数串|living.tv / on_off.on → 已打开电视|
|7|follow，p=.9941，接受；“它”沿用上一轮 LLM 成功操作的电视|未调用|living.tv / on_off.off → 已关闭电视|
|8|follow，p=.5858，低于.95，未接受；旧焦点=电视，新请求=窗帘；未存 choice|propose_home_action：control/resolved，targets=('living.curtain',)，action=('position','open')；无完整原始参数串|living.curtain / position.open → 已打开客厅窗帘|

续接的题目只围绕上一次成功动作的设备（及上一句与 Agent 反馈）判断，候选包含“重新理解”。接管后的 Proposal 由 Agent 保留旧 targets、将选项映射为该设备支持的动作，再统一校验。第6轮概率高不等于一定执行：“重新理解”也可以具有高概率；此外动作映射失败也会拒绝，所以未记 choice 时不能精确归因。

第6/8轮续接未接管后，现有流程直接把原话、完整候选目录、当前上下文交给 LLM，不额外再跑一次 Laya 单句。第7轮证明 LLM 执行后的焦点能回到 Laya，未出现一旦进入 LLM 就持续使用 LLM。

## 两次 LLM 细分

|轮|连接/请求建立阶段|首块|工具就绪|流结束|输入 token|输出 token|缓存输入|
|---|---:|---:|---:|---:|---:|---:|---:|
|6|250|252|1084|1094|4233|91|2304|
|8|166|181|1123|1134|4232|91|2304|

这些是从请求开始计的累计时间，不可相加；connect_ms 包含 SDK/请求建立阶段，不是单独测得的 TLS 握手时间。工具产出等待约832/942ms，是两轮 LLM 的主要时间。configured_max_retries=2 是配置上限，不是发生了两次重试的证据。本轮日志无超时或重试事件。

## 结论与下一步边界

- 6轮 Laya 接管的 Agent 时间356–559ms，中位403ms；续接服务266–287ms，首次三题444ms。没有触及1秒 Laya预算，无截断。
- 切设备两轮 Agent 为1508/1575ms：先续接约297/303ms，再 LLM 1094/1134ms；设备执行仅39/48ms。慢在当前顺序判断路径，不在 Provider。
- “打开它”Agent 425ms，但 VAD结束到最终转写628ms，服务器结果转发到1131ms；这一轮体验延迟有明显识别/端点等待成分，不能归因于 Laya。
- 8轮 executed、最终状态一致，但没有收集这8轮设备端绘制 ACK；delivered不能等同用户看到屏幕的时刻。未持续采集串口，不能恢复过去的屏幕日志；本轮没有重启设备来冒充历史采集。
- 本次只分析，不为这8条增设规则。若优化切设备路由，应独立评审如何复用现有明确目标判断，保持指代、否定和取消行为；不能看到“电视”关键词就绕过上下文。
- 本轮暴露的记录缺口是续接 choice/拒绝原因、原始模型结构化输出、终端接收/绘制时间。当前报告保留已有证据；未引入新的观测框架或默认记录全部敏感上下文。
