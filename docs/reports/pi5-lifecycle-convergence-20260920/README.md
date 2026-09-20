# Pi5 服务生命周期统一协调验收

本轮替代上一轮网络专用的内存补偿。Kernel 提交 `09cda386d0c9f775a091923865d695a353eb1d47`。

执行规则：先持久化操作意图，再提交给进程管理器；以 job 和进程实例的实际观察确认完成。启动、停止、手动重启及网络刷新走同一协调器。systemd 使用原生 `--no-block` 提交及 `Job` 状态；监督器重启后恢复未完成意图，不凭超时推断“没有执行”。

架构说明见 Kernel `docs/adr/0019-observed-service-operation-convergence.md`。

## 本地验证

- 系统、发布、运维契约测试合计 **337 通过、1 跳过**。
- 10 项 import-linter 分层契约通过，全部修改 Python 文件 Ruff 通过。
- 确定性故障覆盖：三种操作的慢 job／丢回复、重建监督器与重开数据库、提交前崩溃、完成后落盘失败、旧 revision、重复请求、实例未替换、操作中反向启停、连续网络变化、健康探测期间进程／网络变化。
- SQLite 延续原 v1 表结构，在请求 JSON 中保存私有执行元数据；部分唯一索引保证每服务一个未完成操作。使用上次发布的真实 SQLite 实现打开新数据，验证 desired state／请求 replay 可读；再次升级后未完成意图仍在。

## 实机验证

标准发布 `pi5-lifecycle-convergence-20260920a` 已 activated。只变更 Kernel revision，其余组件沿用上次发布的精确版本。发布期间真实 LiveKit 曾处于 `deactivating/stop-sigterm`、`Job=35352`；applier 只提交一次自动刷新，Hub 的设备授权入口持续返回 200。

部署后连续 91.21 秒（4 次间隔 30 秒的采样）保持同一个 LiveKit PID／InvocationID，12 个服务全部 ready，LiveKit `network_current=true`，未完成意图为 0；设备授权入口解析返回 200。Owner authority、10 个 Claim、16 个 Proposal、10 个 ACK 和全部 Mobile Claim 检查点与部署前一致。

证据：[发布](deployment.json)、[服务与操作记录](runtime-checks.json)、[稳定性](stability.json)、[授权不变](checks.json)、[发布前](before-authority.json)、[发布后](after.json)。

## 2026-09-21 真机交错验收完成

用户明确授权短暂中断服务后，执行了此前被自动审批拦截的故障交错验收。本次没有修改产品代码或重新部署。

- LiveKit 重启请求 0.328 秒返回时，旧 PID 32956 仍处于 `deactivating/stop-sigterm`，systemd job 38483 尚未完成。
- 确认 job 尚在执行后重启 eidolond；监督器从 PID 32353 变为 74599，InvocationID 也改变。重启后的监督器观察并恢复已有操作。
- LiveKit 最终从 PID 32956 变为 74815。91.36 秒观测窗口内，只记录一次 applier 重启提交、一次进程实例替换，未发生重复刷新。
- 监督器重启前、恢复后各重放同一个请求，均返回 `replayed=true`，保持原 audit position 14，未追加重启。
- 首次观测全部 12 个服务 ready 在约 24.77 秒；最终未完成操作为 0。媒体重启期间 Hub 仍 ready；恢复后设备授权入口解析返回 200。
- Owner authority、10 个 Claim、16 个 Proposal、10 个 ACK 和全部 Mobile Claim 检查点在本次故障注入前后完全一致。

证据：[交错与重放完整记录](host-job-recovery.json)、[授权和入口检查](hil-checks.json)、[验收前](before-hil.json)、[验收后](after-hil.json)、[实机验收脚本](exercise-host-job.py)。脚本会中断服务，只能在得到明确授权的 Host 上执行。


这不是 exactly-once 执行承诺；进程管理器是外部执行者。覆盖的是已列明的可观察时序及收敛规则。物理 Wi-Fi 换网和双向音频质量不在本轮实机验收范围内。
