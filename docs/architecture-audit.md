# Eidolon OS Raspberry Pi deployment / operations audit

Audit updated: 2026-08-09 (Asia/Shanghai). The running Mac stack and sibling working trees were not changed. On the
Pi, the fixed legacy code/unit/config/staging namespace was removed without a data wipe, then the non-product
Foundation v2 was installed and verified. One obsolete candidate was uploaded and entered native preparation; it
failed closed on a PyPI timeout before sealing/activation, so no product service or authority data changed.

## Current code evidence

The multi-repository root is not Git. The selected release commits remain explicit even when sibling HEADs advance:

| Source | Selected exact release input | Role |
| --- | --- | --- |
| Kernel | `267d5dad38e2c2a83d56f0f8e2bf6fb42897fb61` | FHS release authority + Channel Provider topology |
| Data | `d81086e2807f44ca0c0e43e31103cd85e6165a46` | Data V2 + Workspace/runtime authority |
| Hub | `4bab6a0c5201c6adda7ba0f68241297034326cae` | proof-bound onboarding + dedicated Provider port |
| Admin | `987c69282a5a91361b0e9d20144bb7163b8241b3` | Mobile admission orchestration + isolated FHS systemd fix |
| Agent | `309ba573f249f9376275e14a5cc2f5ea1049b022` | session-authorized Companion brain + authority E2E |
| Channel | `8843de6c1268bf01bf8e303ce26fb209df3e033b` | voice runtime + formal Hub Channel Provider |
| Memory | `303b6004c58abbf86eb311de1f4002748fa9457d` | supervisor/discovery, no direct Data integration |
| SDK | `8108970514d9fefd3d93e7466e91706a1681c331` | runtime-authority/session support source |

The selected matrix is now a deployable **candidate**: Admin commit `987c692` combines the formal Mobile admission
workflow with the isolated FHS systemd fix. The upload preflight reads all 15 units directly from
their Git objects and accepted the `/etc/eidolon/host.env`, `/opt/eidolon/current/eidolon_admin/` and FHS state-path
contracts. The exact 8-source bundle and the 14-file private-input contract also passed locally. Current sibling
branches and dirty working trees remain outside the release input: Ops does not stage, overwrite or copy them. The
candidate is not production-qualified until the product contract blockers below are resolved and target-native
preparation, activation and hardware acceptance pass.

## Authority and dependency topology

```text
eidolon-ops -> Host profile -> local supervisord | strict SSH/systemd -> Kernel eidolon-release

Bootstrap -> Local API / Admin                  (Admin-owned commissioning/control)
eidolond -> NATS -> Memory -> Agent -> Channel (machine desired state)
         -> LiveKit -> Hub / Channel
         -> Data -> Workspace / Agent / Channel
         -> Hub
         -> Kernel -> Channel

Admin -> public eidolond/Data/Hub/Kernel application contracts
Deployer -> no sibling SQLite and no cross-database transaction
```

Data/Hub/Kernel/Admin/Agent/Channel/Memory keep their existing public contracts and authority. The deployer only
manages code, fixed system assets, lifecycle and health. It never copies an authority table or claims distributed
atomicity. Data V2 begins only at its tracked baseline; old migrations and old `eidolon.sqlite3` are outside scope.

The selected contract now owns both former consumer gaps. Channel provides authenticated, idempotent
`/v1/device-channels/provision|revoke` on 8767 with LiveKit health, and Admin provides Controller-authenticated Local
API onboarding target/admission with forward-only Hub claim → Kernel mount → optional Companion attach. The remaining
device-conversation blocker is the deployment-owned LAN TLS/WSS ingress and certificate trust described below.

The public Hub transport is also incomplete in the release assets. `eidolon-hub.service` binds plaintext HTTP only to
`127.0.0.1:8082`, while the pinned Hub config and mDNS advertiser claim `https://eidolon-hub.local` on port 443. No
unit, reverse proxy, Hub certificate input or readiness check owns that TLS endpoint. The Local API certificate/SPKI
cannot be reused by assumption. A started Hub may therefore advertise an unreachable/untrusted endpoint; process and
mDNS health are not sufficient onboarding evidence.

## Existing capability, previous gap, implemented closure

Kernel already owned exact commit bundle, target-native frozen environments, sealing, snapshot, ordered quiesce,
atomic asset/link switch, readiness, receipts, automatic restore and explicit rollback. The historical
`eidolon_pi_deploy` instead patched detached clones, restored the legacy database and ran one supervisord unit; it
conflicts with current Data V2/systemd authority and was rejected.

The previous Kernel contract covered only the core control path. This change extends the formal contract to 8 source
archives, 7 components, 23 system assets, 11 private prerequisites, 14 affected units and 13 readiness checks, with
Channel model hydration fail-closed. The independent Ops layer adds strict workstation config, Raspberry Pi
foundation provision, first install, 15-unit lifecycle/status/logs/diagnostics and a Host-side App gate.

## Option matrix

| Option | Reuse and safety | Boundary fit | Decision |
| --- | --- | --- | --- |
| Revive historical `eidolon_pi_deploy` | old schema, patched trees, supervisord monolith | violates current authority | reject |
| Grow the Kernel shell driver into all operations | reuses release code but mixes root transaction and workstation UX | first-install/foundation are not Kernel domain | reject |
| Put root deployment into Admin | convenient UI | makes Admin privileged deployer/aggregator | reject |
| Unified `eidolon-ops` with host adapters composing Kernel contracts | one lifecycle/path model; reuses the release authority | preserves all authority boundaries | selected |

## Scenario support matrix

| Scenario | Supported behavior | Remaining condition |
| --- | --- | --- |
| Newly flashed Pi | one `install --apply` provisions foundation, installs and starts the selected 15-unit backend | SSH/known_hosts/sudo and 14 private inputs exist; device conversation still requires trusted LAN TLS/WSS |
| Environment audit only | `provision` and `doctor` are read-only and return nonzero when degraded | Pi reachable |
| First install interruption | durable foundation/install phases; same commit/input digests resume | retain release ID and inputs |
| Existing Pi replacement | reset plan, then explicit data-wiping clean install; no `/srv` migration/adoption | exact matrix must pass before install deletes anything |
| Daily commit update | bundle/prepare/dry-run, then explicit resume+activate | schema gate must remain compatible |
| Activation/health gate failure | exact system asset/link snapshot auto-restored; evidence retained | `rollback_failed` requires manual stop |
| Explicit rollback | restores selected code/assets snapshot only | never restores DB/secrets |
| Offline cached retry | pinned artifact cache and existing release may be reused | a truly new Pi still needs apt/PyPI/Git dependency network |
| Multiple Pis | one strict config per Pi, same CLI/release contract | no fleet fan-out/concurrent scheduler yet |
| App commissioning | `app-ready` proves Host-side Bootstrap/BLE/network/mDNS/TLS descriptor gate | target-selection/admission Local API contracts and real phone E2E remain |
| Device conversation | Hub approval obtains a real Channel Assignment from the Provider | LAN Hub HTTPS and LiveKit WSS trust remain hard blocked |

## Safety conclusion

The selected release matrix, exact bundle, private inputs and real Pi Foundation pass their respective gates. A
clean-install dry-run now detects only the three preserved old authority roots. The user authorized clean replacement,
but deletion remains gated behind final exact-bundle validation. Even after activation, it must not be called “all
devices ready” until Hub HTTPS and LiveKit WSS have a device-verifiable trust path. Target-native build, systemd
verification, activation health, network transition, reboot, rollback
injection, thermal/resource soak and real-phone commissioning remain hardware acceptance work.
