# Pi5 本轮优化与真机验证

用户确认范围：先修复最近评审的 4 个问题及部署链路，再验证当前 USB 网口连接的 Pi5。本轮不包含统一发布事务、模型 CAS 迁移或增量构建重写。起点为 Ops `e77dd28`；工作期间已有的 LAN 观测改动由另一提交 `f1cd4b2` 纳入，保留并在其上修复歧义处理。

## 网络选择

建议使用现有普通局域网完成日常更新，USB 直连保留为可选的首次起机和大文件加速通道。Pi5 已有千兆 Ethernet 和双频 802.11ac Wi-Fi，无需为支持两种连接新增部署框架。[Raspberry Pi 官方规格](https://www.raspberrypi.com/products/raspberry-pi-5/)

| 方式 | 适用场景 | 需要考虑的限制 |
| --- | --- | --- |
| Pi 与 Mac 接入已有 Wi-Fi | 日常更新，减少接线与换板操作 | 实际吞吐取决于信号、AP、共享带宽和 VPN/路由，必须测量 |
| Pi 网口接路由器/交换机 | 稳定工作台，常规 DHCP 和设备发现 | Mac 走 Wi-Fi 时整条链路仍可能受无线限制；全程有线需 Mac 也接 LAN |
| Mac USB 网卡直连 Pi | 无现成 LAN 的起机、临时快速大制品传输 | 需要处理 link-local、接口绑定、多网卡和 Pi 的独立出网路径；物理链路存在不保证 mDNS 记录还在 |

本轮已将 Pi5 的 `require_wired_release_upload` 改为 false。有可达有线候选时继续优先使用；需要强制有线的 Host 仍可显式设为 true。OPi 的显式策略保持原样。链路分类不是速度保证，旧的“120 MB / 2 秒 vs 205 秒”不再作为当前发布提示。

显式 IP 表达固定端点选择，传输层不再将它替换成 Host 的另一条网卡地址。临时 Wi-Fi 验证 profile 存放于忽略入库的 `.eidolon-ops/pi5/validation-20260910/`，本地路径已解析成绝对路径，并比较确认迁移前后 operations 配置等价；保持产品身份和源码，仅指定 `192.168.100.15` 及相同已确认 key 的临时信任条目；未混入活动 Host inventory。实际 status 已经通过这条无线链路返回。

历史证据：`config/hosts/runs/pi5.jsonl` 中 59 次成功 deploy/update，时间覆盖 2026-08-27 至 2026-09-10（UTC）；总耗时中位数 219.6 秒、prepare 67.2 秒（42 个样本）、activate 106.5 秒（59 个样本）、app_ready 21.3 秒（59 个样本）。不同阶段的中位数不能相加，样本包含 resume 和不同版本。upload_finalize 中位数 0.8 秒不包含全部传输，不能由此断言 Wi-Fi 没有影响。

最近一次历史成功更新为 `20260910-pi5-network-lifecycle`：159.3 秒，其中 activate 106.7 秒、host_application 18.2 秒、app_ready 20.5 秒；没有记录新的 upload_finalize / prepare 阶段。它说明缓存命中后的某些更新主要消耗在切换与检查；它不是当前更换板子的测量。

耗时判断应使用：准备与连通排障时间 + 必须传输的字节 / 实际吞吐 + 构建与激活时间。只优化中间一项而增加频繁的手工排障，可能使实际操作更慢。首装与重复更新分别记录，不混用冷缓存和热缓存结果。

## 已实现的修复

1. **SSH 信任**：使用 OpenSSH 查找 known_hosts，识别哈希条目；替换单 Host 时保留同行其他 alias。匹配的 wildcard / negation / revoked / CA 规则拒绝自动改写，不能当成首次信任；原指纹确认和原子写入保留。
2. **authority 恢复兼容性**：新增只读 readiness-compatibility。在暂存、导入 Owner 材料、刷新 Host 配置之前，确认当前运行时可报告 LiveKit 网络事实。不要求恢复前 App 整体健康；普通 deploy 的 App-ready 仍是观测，不能重新成为回滚条件。
3. **LAN fallback**：无可用默认路由时，唯一有效地址仍可使用；多个候选则报告歧义（Mac 观测返回无可确定地址），由已有 app.lan_ipv4 显式指定。不再按地址大小选中虚拟网络。默认路由仍是当前的优先依据；本轮没有实现自动识别任意 VPN 或设备网络的框架。
4. **起机脚本**：网络激活失败返回 75 并报告待重连验证；从目标账号检查无交互 root 提权；分别校验 ssh/mDNS 与有线 IPv4。通过 fake commands 执行完整生成脚本，验证失败退出、声明公钥集合的新增/撤销和重复运行。

## 本地验证

- 最终完整回归：**1,097 passed，83.41 秒**；变更文件 Ruff 检查与 `git diff --check` 通过。之后针对测试正则表达式的 lint 修正，单独重跑对应多网卡用例，1 passed。
- 新增覆盖：哈希旧 key 的换板确认、共享 alias 保留、特殊 SSH 策略拒绝、孤立多接口歧义、旧/新 runtime 的恢复条件、生成脚本真实执行结果、显式 IP 不被自动替换为 USB 地址。
- 中间两次失败已处理：旧链路文案断言仍要求 cable；临时 Wi-Fi 配置放在可发布目录触发本机路径检查。前者更新断言，后者迁入私有忽略目录并验证解析结果等价，没有放宽路径检查。最终全量日志保存为私有验证目录下的 `local-tests.log`。
- 真机测试只能验证本次所用的 Pi5 和发布组合；Mac/OPi 未经真机操作的部分保留为本地回归覆盖，不能称全平台真机通过。

## 真机执行次序与现场边界

1. 只读确定 SSH identity、OS、架构、内存、磁盘、网络、sudo、当前 release 与既有业务状态。当前板子可能装过产品，不依据“换板”推断可清盘。
2. 先保存来源版本和既有部署证据。已安装时做保留数据更新；只有证明是未安装机器才走首次安装。需要重置身份/数据时另作明确操作。
3. 验证 foundation 与私密输入的兼容性，封装确定的源码组合，然后通过现有发布路径部署。
4. 检查 release doctor、App 就绪分项、systemd 与网络状态；重启恢复、再次更新和中断恢复在可控场景下逐项验证。不以单元测试或 App-ready 代替手机实际对话、插话和 Memory 验证。
5. 在可用链路上记录真实 SSH 传输与部署各阶段耗时。Wi-Fi 未接入时，不填写虚构对比，也不把 USB 的结果外推到 Wi-Fi。

当前只读网络发现：工作站默认路由为 Wi-Fi en0；USB 10/100/1000 LAN 为 en7；`eidolon-pi5.local` 当次解析到 `169.254.181.137`。严格 SSH 检查报告该板子与当前专属 known_hosts 的 key 不同；用户已明确确认该指纹，随后使用项目 trust-host-key 命令更新记录，全程保留严格检查。

## 现场记录

- 板子为 Debian 13/aarch64，8 GiB 级 RAM、NVMe 根盘；检查时约 203 GiB 可用。USB 地址 `169.254.181.137`，Wi-Fi 地址 `192.168.100.15`。
- 当前已有 `20260903-memory-verbatim-off-1`，7 个 component 链接指向该 release；主要服务运行中。产品 identity 与工作站输入相同（只比较相等性，不公开私密材料摘要），因此选择保留数据更新。
- 基础环境检查 healthy；新 Ops 的 Host 路径配置检查不通过，unit-applier 尚未运行，作为旧版本现状记录，不把它误说成此次优化造成。
- 同一 SSH 配置、关闭压缩，32 MiB 零字节流发送到目标 `/dev/null`：USB 0.43 秒、约 73.9 MiB/s；Wi-Fi 8.00 秒、约 4.0 MiB/s。每条链路一次小样本，包含建连开销；不等同于真实 rsync 吞吐，不据此断言 VPN 是慢速原因。
- 用户授权的换板信任更新已完成；既有权威数据通过项目 backup 保存。其报告未覆盖 Memory/NATS/objects/voiceprints，所以另使用旧 release 自带的排他锁与启停原语，停服备份 `/etc/eidolon`、`/var/lib/eidolon`、`/var/lib/eidolon-bootstrap` 和 current 链接，然后立即启动原 release 并通过它的 readiness。冷快照 98,347,673 字节，下载到 `.eidolon-ops/pi5/validation-20260910/` 的私密目录并核对摘要；它是本次保护性快照，不宣称已测试完整恢复或跨版本逆迁移。
- 首个候选 `20260910-pi5-ops-validation-1` 的 reversible dry-run 正确拒绝 Bootstrap schema 7→9，未激活；旧服务继续运行，candidate 被自动清理。该次总耗时 275.8 秒，bundle 178.4 秒、target prepare 73.7 秒、release_artifacts 13.7 秒、upload_finalize 0.8 秒。首次本地封装与目标准备比传输耗时更大。
- 以同一组确定提交重建 `20260910-pi5-ops-validation-forward-1`，只做 forward-only dry-run；二次 bundle 缓存命中约 6 秒。真正激活涉及不可自动回滚的 schema 升级，须在候选检查完成后明确确认。
- 补充观测：当次 Pi Wi-Fi 位于 5785 MHz（5 GHz），信号 -59 dBm，显示 rx 108 / tx 24 Mbit/s；Ethernet 1000 Mb/s 全双工；Mac 到 Pi 无线地址的路由为 en0。协商速率为瞬时观测，不能将旧记录中的 VPN 原因当成当前结论。
- forward-only dry-run 通过后，额外执行只暂存配置的新/旧解释器检查；旧 Channel 拒绝 `llm.extra_body`，此时没有覆盖 live 配置。继续验证移除该字段及两个可选 interrupt intent 字段的过渡模板，旧/新 Hub、Agent、Channel、Memory 均通过。
- 在隔离 worktree `/private/tmp/eidolon-pi5-channel-bridge` 创建过渡提交 `3b09d0f5629b0a8f84437d2461d1bceaca176caf`，相对当前 Channel `cecc1f9abf3f` 仅删除模板中的 8 行，不改运行代码或原工作树分支。过渡阶段 Channel direct LLM 暂不携带该 thinking override；interrupt intent 使用新代码既有的 none / 1500 默认值。随后须部署原定正式提交以恢复完整配置，不将过渡版本作为最终产品方案。
- 已清理不兼容的未激活 candidate，过渡候选 `20260910-pi5-ops-bridge-1` 的 forward-only dry-run 通过。当前仍未激活新版本；保留数据升级的不可逆边界来自 release 工具对 Bootstrap schema 7→9 的实际检查。
- 对实际过渡提交重新渲染后的配置，再次执行新旧解释器兼容性校验，通过；并清理私密暂存。已向用户提交具体激活确认，待确认后执行过渡升级和正式配置发布。过渡后正式发布计划使用无线固定端点，以验证日常更新可以脱离 USB 端点选择。

后续执行所需的正式与过渡源码 pin、backup 报告及过渡预检日志已保存到 `.eidolon-ops/pi5/validation-20260910/`。当前状态仍为“代码优化与部署预检完成，实际升级激活及重启验证待确认”，不能表述为整体真机部署测试已通过。
