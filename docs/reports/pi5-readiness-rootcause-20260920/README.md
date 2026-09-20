# Pi5 设备清单 503：根因与修复验收

## 已确认的故障链

1. 主机重启／网络输入变化需要刷新 LiveKit。其参与者排空持续至 systemd 的 `TimeoutStopSec=20s`，随后进程确实重启成功。
2. eidolond 调用 applier 的等待窗口也是 20 秒，在 systemd 完成并送回结果前先超时断开。applier 日志明确成对出现 `applied restart` 和 `Broken pipe`。
3. 旧监督器只在同步调用成功后记录已消费的网络输入，没再观察超时操作的真实结果，故每约 30 秒又重启成功启动的新进程。
4. manifest 错把 Hub 整体就绪绑定到 LiveKit/channel-provider，导致 Hub 自身健康、配置拉取 200，但服务目录的设备授权入口仍为 blocked。Mobile 设备清单最终得到 503。

本次启动的循环从 22:09:49 已出现，Mobile 新 APK 安装在 22:55，所以不是此次 Mobile 当前 Claim 重构引入的回归。`generation` 与授权库均不是本故障原因。伙伴接口实际上返回 200，设备关联查询失败使对话准备也受阻。

## 修复

Kernel `6ae0752`：保留一次刷新尝试的网络输入与旧进程实例；下一轮协调先观察完成结果，只有新实例、同一网络输入且健康检查通过才恢复就绪。真实失败继续退避；网络不可用或改变不能冒认成功。不靠延长超时解决结果不确定性。

移除 Hub 对媒体整体就绪的错误依赖，设备身份／Claim 查询由 Hub 自身健康决定；媒体操作继续在实际调用边界处理媒体服务不可用。未改 Mobile 提示，未重建授权。

## 发布与验证

- 标准 Ops 可回退发布：`pi5-readiness-rootcause-20260920a`，结果 `activated`。只有 Kernel revision 改变；Admin、Hub、Data、Agent、Channel、Memory 和 SDK 全部显式固定在原发布版本。
- system 测试：136 通过，1 跳过；修改文件 Ruff 通过。
- 新监督器在 23:27:58 也遇到一次 applier 20 秒超时，applier 随即记录实际成功；后续观察到了新实例并恢复就绪。不是因为部署时碰巧没遇到慢重启而通过。
- Mobile 实际 `GET /api/management/v1/devices` 在 23:28:38、23:29:50、23:29:53 均返回 200；首页红字消失，设备页列出 Stackchan、Mobile、BOX-3。
- 对比修复前后：Owner authority、10 个 Claim、16 个 Proposal、10 个 ACK、全部 Mobile Claim 检查点完全一致。
- 连续稳定性观测见 `stability.json`：同一 LiveKit PID/InvocationID、全部 12 个服务 ready，`network_current=true`。本记录只验收设备清单与服务协调故障；不把它当作双向语音质量验收。

证据：[发布结果](deployment.json)、[稳定性观测](stability.json)、[授权不变检查](checks.json)、[修复前授权](before-authority.json)、[修复后授权](after.json)、[平板页面](mobile-after-fix.png)。
