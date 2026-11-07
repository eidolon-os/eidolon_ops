# Host TLS 证书与长期信任模型对齐：现状与方案（2026-11-07，第二版）

- **问题**：Owner 信任材料已经改成签发 99 年、只装一次（eidolon_ops `1a1198c`）。但 Host 的两张 TLS 证书还是有到期日，并且要等过期才续签（其中一张根本不续签）。产品交给用户后，没有人会去跑 ops。
- **为什么有第二版**：第一版只是给两张证书各加了一个"剩余 1/3 时续签"的门槛，属于打补丁。它保留了续签逻辑，续签后还要靠 Host 重启才生效，没有回答"这两张证书在长期信任模型里应该是什么"。这一版从信任锚出发重新设计。另外，用户已经确认不需要兼容旧版本，手机可以重新配对。
- **工作方式**：只读代码；对 Mac Host 上两张证书做了只读检查，只解析了证书部分。没有改代码，没有重启服务，没有部署。
- **代码版本**：eidolon_ops `1a1198c`；eidolon_admin `875dc4f`；eidolon_client_mobile、eidolon_client_reachy_mini、eidolon-client-esp32 均为当前 main。

## 0. 结论

**Host 上的每一张 TLS 证书都挂在一个长期信任锚下，签发一次、管一辈子（99 年）。客户端只信任锚本身，不认具体某张证书，也不靠证书的有效期来判断。代码里不再保留任何续签逻辑。** 只有身份本身变了才会重签，例如 Hub 的主机名变了，或者 Host 被重置。

| 通道 | 谁连 | 信任锚（长期） | 下面的 TLS 证书 | 改动 |
|---|---|---|---|---|
| Hub（8443，Admission、Device Control、descriptor） | Reachy、ESP32、手机配网设备时 | **Owner root**（99 年，设备和手机配网时装一次） | `hub.crt`，由 Owner root 签发，DNS SAN 是 `eidolon-hub-<id>.local` | 从 825 天、过期才续签，改为 **99 年**，有效期不超过 Owner root |
| Local API（9002，Host 管理面） | 手机 | **Host 身份 Ed25519 密钥**（本身不过期，配对时经 PAKE 认证） | `commissioning_tls.pem`，自签，只是包着 Host 传输公钥的一层外壳，手机 pin 的是公钥 SPKI | 从 10 年、到期就起不来，改为 **99 年**；已有的证书**用原私钥重签**，SPKI 不变 |

**为什么不把 Local API 也并到 Owner root 下：**
- 手机配对 Host 时还没有 Owner root。Owner root 是配对之后通过 Local API 拿到的（`/api/local/v1/device-onboarding/target`）。
- Local API 一律按 IP 直连（`local_api_base_urls` 只发 IPv4 地址），而 IP 会随 DHCP 变化，Owner root 签发的证书没办法带一个稳定的 SAN。
- 对这种按 IP 访问、身份就是一把公钥的通道，"由 Host 身份背书的公钥 pin"才是合适的原语，本质上就是 TLS 的裸公钥认证（RFC 7250 讲的就是这种思路）。证书在这里只是一层外壳，它的有效期没有安全含义，所以应该和 pin 住的公钥活得一样久。

## 1. 现状（已核实）

### 1.1 两张证书（Mac Host 实测）

| 证书 | 签发方 | 有效期 | 当前的签发和续签逻辑 |
|---|---|---|---|
| `hub.crt` | Owner root | 2026-08-25 → 2028-11-27（825 天） | eidolon_ops [`_host_tls`](../../../src/eidolon_ops/owner_domain_assets.py)（第 466 行）：只在已过期或 SAN 变化时重签，并换一把新密钥（第 502 行）。LAN ingress 只在启动时加载一次证书（[`lan_ingress.py:202`](../../../src/eidolon_ops/lan_ingress.py)） |
| `bootstrap/state/commissioning_tls.pem` | 自签，CN 为 `ehost-…`，SAN 为 `urn:eidolon:host:<id>` | 2026-08-09 → 2036-08-07（3651 天） | eidolon_admin `bootstrap/tls_identity.py`：只在文件不存在时生成（第 110–130 行）；到期后 `load()` 直接报错（第 83–90 行），Local API 和 BLE 配网都起不来 |

### 1.2 客户端各自怎么校验

| 客户端 | 校验方式 | 是否检查主机名 | 是否 pin 叶证书 |
|---|---|---|---|
| Reachy | `load_verify_locations(cadata=Owner root)`（`owner_https.py:32`） | 是 | 否 |
| ESP32 | 拿 Owner root PEM 做 `HubHttpRequest`（`hub_onboarding_client.cc:332`） | 是 | 否 |
| 手机连 Hub | 用 OkHttp + 只含 Owner root 的 TrustManager，按 PKIX 链校验；通过 `addressHints` 连 IP，但按 `.local` 主机名校验（`PinnedHttpsClient.kt:51–57`、`PinnedHttpsProtocol.kt:35–51`） | 是 | 否 |
| 手机连 Local API | `PinnedSpkiTrustManager` 只比对 SPKI 的 sha256，外加 `checkValidity()`（`PinnedSpkiTrustManager.kt:42–54`），不查主机名，不查证书链 | 否 | **pin 的是公钥**（来自 Host 签名的 endpoint 文档，并经过 PAKE 认证） |

另外：移动端只支持 Android（`pinned_http_client.dart:139` 在非 Android 平台直接返回 `unsupportedPlatform`，仓库里也没有 `ios/` 目录）；eidolon_client_web 和 admin 的 Web 控制台都只走本机的 HTTP，不经过这两条 TLS 通道。也就是说，目前**没有任何 Apple 客户端或浏览器**连接这两张证书。

## 2. 方案

### 2.1 `hub.crt`（eidolon_ops `_host_tls`）

- 有效期改用和 Owner root 相同的 99 年策略（`OWNER_TRUST_LIFETIME_YEARS`），并且不超过 Owner root 的到期时间。
- 复用条件改成"按长期策略签发，且 SAN 与本机一致"，和 root、signer 两张证书用同一个判断函数 `_issued_for_lifetime`。删掉"已过期才重签"这条路径。按旧的 825 天签发的证书、SAN 变了的证书会重签，重签时换新密钥（没有客户端 pin 这张证书的密钥）。
- 测试：新签发的证书是 99 年，且不超过 Owner root；825 天的旧证书会被重签；之后再执行一次不会再写文件；SAN 变化时会重签。

### 2.2 `commissioning_tls.pem`（eidolon_admin `CommissioningTlsIdentityManager`）

- `_generate` 的有效期从 3650 天改成 99 年。
- `load()` 发现现有证书短于长期策略时，**用原私钥**重签证书，CN 和 SAN 不变，然后原子替换文件，权限保持 0640。SPKI 不变，所以 Host 签名的 endpoint 文档和手机里的 pin 都不变，手机不需要重新配对。
- 测试：新生成的证书是 99 年；10 年期的证书会被重签成 99 年，并且 SPKI 和 CN 不变；之后再执行一次不会再写文件；权限、CN、密钥配对这几项原有校验照旧。

### 2.3 设计文档

在 §3.6.1 上次补的那一条后面加上：Host 的 TLS 证书也遵循长期策略。Hub 的证书挂在 Owner root 下；Local API 的证书是 Host 身份密钥背书的公钥外壳。两者都签一次、不续签，只有身份变化时才重签。同时去掉上次写的"TLS 叶证书受 Apple 825 天上限约束，不在此列"。

### 2.4 不做的事

- **不改移动端**。手机已有的"Local API 换了密钥就改 pin"逻辑（`_adoptRotatedTlsIdentity`）和有效期无关，处理的是 Host 丢失 TLS 状态、Local API 重装这类情况，所以保留。
- 不加常驻进程的定时续签，也不依赖重启来续签：既然不再有续签，这两样就都不需要了。
- 不把 Local API 并到 Owner root 下，原因见 §0。

## 3. 代价和以后要注意的

- **长期密钥一旦泄露，影响时间更长。** 如果 `hub.key` 或 Local API 的私钥泄露，只要不重签，攻击者就可以一直冒充 Hub 或 Local API。这和 Owner root、signer 私钥泄露是同一级别的事件，处理方式也一样：重置 Owner 或 Host，然后重新配网、重新配对。
- **以后加 Apple 客户端时需要再做决定。** Apple 支持文档 103769 写的是"All TLS server certificates"，有效期必须不超过 825 天。到那时有两个选择：一是 iOS/macOS 客户端自己按 Owner root 或 SPKI 做校验，不走系统的 TLS 证书策略；二是把这两张证书降回 825 天以内，并由 Host 在 `start` 时自动续签。这一点会写进设计文档，作为已知的决策点。

## 4. 实施（2026-11-07，已获确认，未提交）

- **eidolon_ops `owner_domain_assets.py`**：
  - `_lifetime_end` 新增 `within` 参数，签出的证书一律不会比为它背书的证书活得更久。Directory Signer 和 `hub.crt` 都以 Owner root 为上限。
  - `_host_tls` 复用证书的条件改成"SAN 一致，且按长期策略签发"，删掉了"已过期才重签"。825 天的旧证书会被重签一次，签成 99 年。
  - 测试：`test_owner_domain_assets.py` 新增"叶证书与 Owner root 同时到期"这一断言；"签发策略改变之前的材料"夹具里加入 825 天的 `hub.crt`，验证它会被重签为 99 年、仍由 Owner root 签发。相关的 325 个测试全部通过，ruff 检查通过。
- **eidolon_admin `bootstrap/tls_identity.py`**：
  - 新增 `TLS_IDENTITY_LIFETIME_YEARS = 99`。生成证书的逻辑抽成 `_issue(host_id, private_key)`。
  - `load()` 发现证书有效期短于 99 年时，用原私钥重签，然后重新走一遍完整校验。这条逻辑放在有效期检查之前，所以已经过期的旧证书也会被重签，不会直接报错。
  - 新增测试 `server/tests/test_bootstrap_tls_identity.py`，4 个用例：新证书为 99 年；10 年期证书（未过期和已过期两种）都会用原密钥重签，SPKI、CN、SAN、0640 权限保持不变，再执行一次不会再写文件；属于别的 Host 的证书和不安全的文件权限照旧会被拒绝。
- **设计文档 §3.6.1**：新增一条，写明 Host TLS 证书的长期策略、两个信任锚的分工，以及 Apple 客户端这个已知决策点。

生效时机：`./eidolon` 直接用 eidolon_ops 工作区的源码运行，所以下一次 Mac `restart` 时，`hub.crt` 会被重签成 99 年。admin 下次启动时（同样在 `restart` 里）会用原密钥重签 Local API 证书，SPKI 不变，手机不需要重新配对。

## 资料来源

- RFC 5280 §4.1.2.5（设备生命周期证书）：https://www.rfc-editor.org/rfc/rfc5280#section-4.1.2.5
- RFC 7250（TLS 中直接使用公钥而不依赖证书）：https://www.rfc-editor.org/rfc/rfc7250
- Apple TLS 服务器证书 825 天上限：https://support.apple.com/en-us/103769
- 上一轮 Owner 信任改为长期签发的报告：`eidolon_client_reachy_mini/docs/reports/owner-directory-refresh-20261106/README.md`
