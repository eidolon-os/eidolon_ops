# opi5max Memory 修复发布验收（2026-10-08）

用户授权通过 USB 网口更新并确认实际可用性。正式发布版本为 `rk3588-memory-policy-20261008-2`，Memory 源码 `adcbf2da5a7d0403e0f26d9787fc9fea2bd3ed22`，包含修复 `7336ee5`。上传和部署绑定 `en7`，目标 `169.254.192.66`，使用既有 SSH 主机信任及正式 Ops 更新入口；未重刷、清空用户数据。

## 发布与数据保留

- 更新前为 `rk3588-panel-sync-20261002-1`。正式 converge-inputs 补齐新版声明的两个缺失凭据，未覆盖已有值。
- 普通 reversible 预检因 Data schema 升级而拒绝，未激活。核查 0004 为增列，0005 仅清理有独立声音快照证明的冗余字段；目标 3 个伙伴无冲突、无不支持的偏好。
- 先按已激活版本的状态声明备份 SQLite。新版状态声明包含旧版尚无的 Hub integration 库，不能直接拿新版清单要求旧 Host 备份它。
- 旧版 Memory 快照接口报告缺少 palace/chroma.sqlite3，不能算成功；通过 Ops stop 停机后补存完整 memory 目录，确认无 Memory worker 在运行，再恢复原版本。压缩包包含 38 项，100426 bytes，SHA-256 见 JSON。备份位于本机 gitignored `.eidolon-ops/opi5max/backups/pre-memory-policy-20261008/`。这不是全磁盘备份：声纹、媒体对象和 JetStream 等仍按既有声明排除，未在此次更新中删除。
- 使用 forward-only 正式激活新版本，避免升级后的数据库自动退回旧解释器。发布事务、运行进程版本收敛、doctor 和 app-ready 均成功；Data 实际版本为 `0005_conversation_preferences`。
- 升级前后均为：3 个伙伴、11 条 Chroma 投影、11 条 canonical assertions、5 条图谱关系、164 个抽取决策。

发布准备与补充备份的生命周期动作曾争用发布锁；锁拒绝了候选 dry-run，未清锁或绕过检查。停机备份及恢复完成后，串行 resume/activate 成功。后续同一 Host 的准备/启停/激活操作也应串行执行。

## 真实验收

一次性测试以 eidolon 服务账号运行，由 systemd 按正式服务方式注入 memory.env；不改变凭据文件权限。测试使用独立临时 NATS、Realm、数据库和端口，实际安装的 Memory wheel、真实模型和 Host 的嵌入服务；输入为合成事实，不进入用户伙伴空间。

实际加载 `atomic-claims-v2` / `llm:73ceb36e927aea14`。原始 turn 经 JetStream → steward → canonical/vector/KG 后，鉴权 browse/graph 返回 1 条记忆、2 个节点、1 条关系；单样本约 15.547 秒可读。再次投递复用决策、数量不增加；另一 companion 的 browse 为 0。测试成功退出并清理临时进程和数据。这是功能验收，不是 p95/SLO 或完整对话质量验收。

正式管理后端（Mobile 使用的内部 library/graph 链路），按实际 Owner 鉴权读回：11 条记忆、6 个节点、5 条关系，无截断。未操作手机 UI、未在正式伙伴记忆写入合成事实；既有原数据保留，不自动重写历史抽取决策。

本次证明修复已可发布到另一架构的 Host，运行服务和真实抽取均生效。跨轮指代补全仍是独立未解决项，不能由这次单轮事实验收推导为已解决。详细机器可读证据见同名 JSON；凭据和用户正文不入库。
