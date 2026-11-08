# 清理与未合入提交核查（2026-10-30）

时间均为 Asia/Shanghai。未合入指提交不是本地 main 的祖先，不等于功能一定缺失。未按标题判断代码等价，也未自动合并试验代码。

## 日志处理

删除 562 个未打开且超过七天的普通日志，合计 4,615,183,300 字节（约 4.30 GiB）；保留 13 个仍打开的旧日志。清理后 df 可用空间仍约 30 GiB，未将逻辑文件大小当成实际新增空闲空间。

## 未合入提交明细

### conversation-person-20261027

| 仓库 | 提交 | 提交时间 | 内容 | main 上的同名提交（不等于补丁等价） |
|---|---|---|---|---|
| eidolon_client_mobile | `9cdeaa4` | 2026-10-27 23:47 | 对话页展示说话人识别结果 | 未发现 |
| eidolon_client_mobile | `c0f88f2` | 2026-10-28 00:45 | 区分语音不足与身份未匹配 | 未发现 |
| eidolon_client_mobile | `6db7050` | 2026-10-28 00:56 | 评测标签仅保留在诊断信息 | 未发现 |
| eidolon_data | `a6545f3` | 2026-10-27 17:56 | 声纹归属 Person，淘汰旧分配方式 | 未发现 |
| eidolon_data | `70ce3f7` | 2026-10-27 19:01 | 固化个人记忆 Realm 验证版本 | 未发现 |
| eidolon_data | `9c038f9` | 2026-10-27 23:47 | 提供有界、Owner 隔离的识别人选 | 未发现 |
| eidolon_data | `3cfe41f` | 2026-10-27 23:51 | 个人 Realm 生命周期增加修订保护 | 未发现 |
| eidolon_models | `68c1ae4` | 2026-10-27 23:47 | 版本化对话身份识别试验策略 | 未发现 |
| eidolon_models | `6930bc1` | 2026-10-27 23:51 | 随固定模型制品交付识别策略 | 未发现 |
| eidolon_models | `a4061bd` | 2026-10-28 00:45 | 增加短语音识别试验策略 | 未发现 |
| eidolon_models | `7823c49` | 2026-10-28 00:56 | 评测标签仅保留在诊断信息 | 未发现 |

### voice-capture-20261030

| 仓库 | 提交 | 提交时间 | 内容 | main 上的同名提交（不等于补丁等价） |
|---|---|---|---|---|
| eidolon_admin | `9606b09` | 2026-10-30 11:45 | 声纹录入复用 LiveKit 对话音频 | 79e6345 |
| eidolon_channel | `7ad8dbb` | 2026-10-30 11:45 | 声纹录入复用 LiveKit 对话音频 | 84d2bba |
| eidolon_client_mobile | `aca6904` | 2026-10-30 11:45 | 声纹录入复用 LiveKit 对话音频 | 84bad4b |
| eidolon_sdk | `4399d8c` | 2026-10-30 11:45 | 声纹录入复用 LiveKit 对话音频 | 96b1cc6 |

### owned-voiceprints-20261027

| 仓库 | 提交 | 提交时间 | 内容 | main 上的同名提交（不等于补丁等价） |
|---|---|---|---|---|
| eidolon_sdk | `44dd77c` | 2026-10-28 22:45 | 区分 Person 归档和永久删除 | f0a047f |

### team-lifecycle-20261030

| 仓库 | 提交 | 提交时间 | 内容 | main 上的同名提交（不等于补丁等价） |
|---|---|---|---|---|
| eidolon_admin | `9606b09`（上表已列） | 2026-10-30 11:45 | 声纹录入复用 LiveKit 对话音频 | 79e6345 |
| eidolon_admin | `0cf0dd3` | 2026-10-30 12:24 | 团队持久化生命周期与按需试听 | 未发现 |
| eidolon_admin | `8a3d547` | 2026-10-30 12:53 | 清理完成后保留团队启动拒绝原因 | 未发现 |
| eidolon_admin | `f2789f1` | 2026-10-30 13:01 | 统一通过已保存团队启动 | 未发现 |
| eidolon_admin | `090503d` | 2026-10-30 13:10 | 提供鉴权分页的团队运行历史 | 未发现 |
| eidolon_channel | `7ad8dbb`（上表已列） | 2026-10-30 11:45 | 声纹录入复用 LiveKit 对话音频 | 84d2bba |
| eidolon_channel | `00dc135` | 2026-10-30 12:24 | 团队持久化生命周期与按需试听 | 未发现 |
| eidolon_channel | `96555e3` | 2026-10-30 13:01 | 诊断工具复用已保存团队准入 | 未发现 |
| eidolon_channel | `6fcc942` | 2026-10-30 15:12 | 取消团队回复时保持 Hub 协议一致 | 未发现 |
| eidolon_data | `7818b0e` | 2026-10-30 12:24 | 团队持久化生命周期与按需试听 | 未发现 |
| eidolon_data | `1f4a131` | 2026-10-30 12:51 | 将团队快照不匹配报告为业务冲突 | 未发现 |
| eidolon_data | `5c590fa` | 2026-10-30 13:10 | 增加 Owner 隔离、有界团队运行历史 | 未发现 |
| eidolon_sdk | `4399d8c`（上表已列） | 2026-10-30 11:45 | 声纹录入复用 LiveKit 对话音频 | 96b1cc6 |
| eidolon_sdk | `c4a1282` | 2026-10-30 12:24 | 团队持久化生命周期与按需试听 | 未发现 |
| eidolon_sdk | `a9c334c` | 2026-10-30 13:10 | 增加团队运行历史分页游标 | 未发现 |

## 为什么存在两套历史

- conversation-person 是 10 月 27–28 日的独立候选分支。main 后续以其他提交实现并继续调整部分功能，例如 Mobile 78ef047（10 月 28 日 09:46）和 Data d21e994（同日 09:46）。不能从 Git 历史推断所有差异已经有意取舍，尤其 Models 的识别试验策略仍不在 main 历史中；需要功能级对比才能决定集成。
- voice-capture 在 10 月 30 日 11:45 的副本和 19:05 的 main 分别提交。SDK 补丁等价，其余三个补丁不完全等价，不能直接宣布原提交全部合入。
- team-lifecycle 是 10 月 30 日部署验证时分步形成的发布提交；main 后来按仓库整理提交，原来的发布提交没有通过 merge 纳入。这是发布与开发两套历史尚未收敛，不应靠删除目录解决。
- owned-voiceprints 的 SDK 44dd77c 与 main 的 f0a047f 补丁等价，是提交身份不同。

## 不丢代码的清理方式

所有 25 个实际工作副本的 HEAD 均已被本地分支或标签保护；原来孤立的 HEAD 增加 archive/cleanup-20261030-* 分支。保留这些引用、仓库 .git 和 LFS 对象后，用 git worktree remove 移除副本不会删除提交。归档未推送。忽略的构建产物可重建；两个副本还含测试 SQLite 文件，正式删除前应另存这些小文件以保留证据。此轮仅删除日志，未删除候选代码目录。

## 保留引用

| 目录 | 仓库 | HEAD | 保留引用 |
|---|---|---|---|
| conversation-person-20261027 | eidolon_agent | `0ec6a13` | `codex/conversation-person`, `main`, `ed_v0.3.1` |
| conversation-person-20261027 | eidolon_channel | `94e6b28` | `codex/conversation-person`, `main`, `ed_v0.3.1` |
| conversation-person-20261027 | eidolon_client_mobile | `6db7050` | `codex/conversation-person` |
| conversation-person-20261027 | eidolon_data | `3cfe41f` | `codex/conversation-person` |
| conversation-person-20261027 | eidolon_models | `7823c49` | `codex/conversation-person` |
| conversation-person-20261027 | eidolon_sdk | `9dc18a5` | `codex/conversation-person`, `main`, `ed_v0.3.1` |
| voice-capture-20261030 | eidolon_admin | `9606b09` | `archive/cleanup-20261030-voice-capture-20261030` |
| voice-capture-20261030 | eidolon_channel | `7ad8dbb` | `archive/cleanup-20261030-voice-capture-20261030` |
| voice-capture-20261030 | eidolon_client_mobile | `aca6904` | `archive/cleanup-20261030-voice-capture-20261030` |
| voice-capture-20261030 | eidolon_sdk | `4399d8c` | `archive/cleanup-20261030-voice-capture-20261030` |
| owned-voiceprints-20261027 | eidolon_admin | `b208123` | `archive/cleanup-20261030-voice-capture-20261030`, `main`, `ed_v0.3.1` |
| owned-voiceprints-20261027 | eidolon_agent | `a97386b` | `main`, `ed_v0.3.1` |
| owned-voiceprints-20261027 | eidolon_channel | `a5617ea` | `archive/cleanup-20261030-voice-capture-20261030`, `main`, `ed_v0.3.1` |
| owned-voiceprints-20261027 | eidolon_data | `9a8f1b9` | `main`, `ed_v0.3.1` |
| owned-voiceprints-20261027 | eidolon_hub | `7c63353` | `main`, `ed_v0.2.10`, `ed_v0.2.11`, `ed_v0.2.12`, `ed_v0.2.13`, `ed_v0.3.0`, `ed_v0.3.1` |
| owned-voiceprints-20261027 | eidolon_kernel | `1d7deaf` | `main`, `ed_v0.3.1` |
| owned-voiceprints-20261027 | eidolon_memory | `6f8638f` | `main`, `ed_v0.3.1` |
| owned-voiceprints-20261027 | eidolon_ops | `03d0930` | `main`, `ed_v0.3.1` |
| owned-voiceprints-20261027 | eidolon_sdk | `44dd77c` | `archive/cleanup-20261030-owned-voiceprints-20261027` |
| team-lifecycle-20261030 | eidolon_admin | `090503d` | `archive/cleanup-20261030-team-lifecycle-20261030` |
| team-lifecycle-20261030 | eidolon_channel | `6fcc942` | `archive/cleanup-20261030-team-lifecycle-20261030` |
| team-lifecycle-20261030 | eidolon_data | `5c590fa` | `archive/cleanup-20261030-team-lifecycle-20261030` |
| team-lifecycle-20261030 | eidolon_sdk | `a9c334c` | `archive/cleanup-20261030-team-lifecycle-20261030` |
| person-20261027 | eidolon_data | `cf86801` | `archive/cleanup-20261030-team-lifecycle-20261030`, `codex/conversation-person`, `codex/person-memory-validation-20261027`, `codex/person-owned-voiceprints`, `main`, `ed_v0.3.1` |
| person-20261027 | eidolon_memory | `ec9cdc1` | `codex/person-memory-validation-20261027`, `main`, `ed_v0.2.10`, `ed_v0.2.11`, `ed_v0.2.12`, `ed_v0.2.13`, `ed_v0.2.7`, `ed_v0.2.8`, `ed_v0.2.9`, `ed_v0.3.0`, `ed_v0.3.1` |
