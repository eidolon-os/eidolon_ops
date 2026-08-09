# Eidolon OS Raspberry Pi deployment / operations audit

Audit updated: 2026-08-09 (Asia/Shanghai). No command changed the Raspberry Pi or the running Mac stack.

## Current code evidence

The multi-repository root is not Git. The selected release commits remain explicit even when sibling HEADs advance:

| Source | Selected exact release input | Role |
| --- | --- | --- |
| Kernel | `7de97bd8d87b7e3a84109053d1dd98e5f19b0058` | FHS release authority |
| Data | `d81086e2807f44ca0c0e43e31103cd85e6165a46` | Data V2 + Workspace/runtime authority |
| Hub | `96438a2507fb76ad025824873a99b213a99016ad` | Device/Hub authority |
| Admin | `1ac5c733811c785e70992f527e516fd715b86f5b` | control-plane baseline plus isolated FHS Pi systemd fix |
| Agent | `309ba573f249f9376275e14a5cc2f5ea1049b022` | session-authorized Companion brain + authority E2E |
| Channel | `3bc7e3303fa2c06bcfe0a82dacacd390f7deb372` | Data/Kernel resolver + LiveKit v5 token E2E |
| Memory | `303b6004c58abbf86eb311de1f4002748fa9457d` | supervisor/discovery, no direct Data integration |
| SDK | `8108970514d9fefd3d93e7466e91706a1681c331` | runtime-authority/session support source |

The selected matrix is now a deployable **candidate**: Admin commit `1ac5c73` was created from `02f96b7` in an
isolated worktree and changes only its three Pi systemd units. The upload preflight read all 14 units directly from
their Git objects and accepted the `/etc/eidolon/host.env`, `/opt/eidolon/current/eidolon_admin/` and FHS state-path
contracts. The exact 8-source bundle and the 14-file private-input contract also passed locally. Current sibling
branches and dirty working trees remain outside the release input: Ops does not stage, overwrite or copy them. The
candidate is not production-qualified until target-native preparation, activation and hardware acceptance pass.

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

## Existing capability, previous gap, implemented closure

Kernel already owned exact commit bundle, target-native frozen environments, sealing, snapshot, ordered quiesce,
atomic asset/link switch, readiness, receipts, automatic restore and explicit rollback. The historical
`eidolon_pi_deploy` instead patched detached clones, restored the legacy database and ran one supervisord unit; it
conflicts with current Data V2/systemd authority and was rejected.

The previous Kernel contract covered only the core control path. This change extends the formal contract to 8 source
archives, 7 components, 22 system assets, 11 private prerequisites, 13 affected units and 12 readiness checks, with
Channel model hydration fail-closed. The independent Ops layer adds strict workstation config, Raspberry Pi
foundation provision, first install, 14-unit lifecycle/status/logs/diagnostics and a Host-side App gate.

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
| Newly flashed Pi | one `install --apply` provisions foundation, installs and starts full backend | SSH/known_hosts/sudo and 14 private inputs exist |
| Environment audit only | `provision` and `doctor` are read-only and return nonzero when degraded | Pi reachable |
| First install interruption | durable foundation/install phases; same commit/input digests resume | retain release ID and inputs |
| Existing Pi replacement | reset plan, then explicit data-wiping clean install; no `/srv` migration/adoption | exact matrix must pass before install deletes anything |
| Daily commit update | bundle/prepare/dry-run, then explicit resume+activate | schema gate must remain compatible |
| Activation/health gate failure | exact system asset/link snapshot auto-restored; evidence retained | `rollback_failed` requires manual stop |
| Explicit rollback | restores selected code/assets snapshot only | never restores DB/secrets |
| Offline cached retry | pinned artifact cache and existing release may be reused | a truly new Pi still needs apt/PyPI/Git dependency network |
| Multiple Pis | one strict config per Pi, same CLI/release contract | no fleet fan-out/concurrent scheduler yet |
| App commissioning | `app-ready` proves Host-side Bootstrap/BLE/network/mDNS/TLS descriptor gate | real phone E2E still required |

## Safety conclusion

The selected release matrix, exact bundle and private inputs pass the Mac-side gates, so the candidate is ready for
an explicitly authorized clean-install test on the Pi. It is not yet safe to call production-ready: target-native
build, systemd verification, activation health, network transition, reboot, rollback injection, thermal/resource soak
and real-phone commissioning remain hardware acceptance work.
