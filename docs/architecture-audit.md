# Eidolon OS Raspberry Pi deployment / operations audit

Audit updated: 2026-08-09 (Asia/Shanghai). No command changed the Raspberry Pi or the running Mac stack.

## Current code evidence

The multi-repository root is not Git. The selected release commits remain explicit even when sibling HEADs advance:

| Source | Selected exact release input | Role |
| --- | --- | --- |
| Kernel | `cf668a338c0f6305164cccd49df16eaa7e92aa04` | release authority/system assets |
| Data | `d81086e2807f44ca0c0e43e31103cd85e6165a46` | Data V2 + Workspace/runtime authority |
| Hub | `96438a2507fb76ad025824873a99b213a99016ad` | Device/Hub authority |
| Admin | `7ed63835f04a45b15496609681601bc65bfe2960` | Bootstrap, Local API, owner runtime projection |
| Agent | `2ae449982efe8cf8fede6a32d950111e1290ad15` | session-authorized Companion brain |
| Channel | `fdf7dd42f5d38e14ae05be6fbcf7febf10908397` | Data/Kernel runtime resolver + LiveKit session binding |
| Memory | `303b6004c58abbf86eb311de1f4002748fa9457d` | supervisor/discovery, no direct Data integration |
| SDK | `8108970514d9fefd3d93e7466e91706a1681c331` | runtime-authority/session support source |

These exact commits form one compatible runtime-session set: Data publishes runtime snapshots, SDK consumes them,
Channel resolves Data/Kernel state, and Agent/SDK bind access to immutable LiveKit sessions. Dirty Agent/Channel test
changes and Admin `.coverage` are not staged, overwritten or copied. Bundle construction uses
`git archive <exact commit>`, never the working tree. The earlier Mac observation (`admin-api` and Agent stopped,
Hub fatal, other development processes running) was not modified and is not used as the Pi product topology.

## Authority and dependency topology

```text
Mac eidolon-pi -> strict SSH/SCP -> target installer -> Kernel eidolon-release

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
| Independent `eidolon-pi` composing Kernel contracts | maximal release reuse; isolated tests/config | preserves all authority boundaries | selected |

## Scenario support matrix

| Scenario | Supported behavior | Remaining condition |
| --- | --- | --- |
| Newly flashed Pi | one `install --apply` provisions foundation, installs and starts full backend | SSH/known_hosts/sudo and 14 private inputs exist |
| Environment audit only | `provision` and `doctor` are read-only and return nonzero when degraded | Pi reachable |
| First install interruption | durable foundation/install phases; same commit/input digests resume | retain release ID and inputs |
| Daily commit update | bundle/prepare/dry-run, then explicit resume+activate | schema gate must remain compatible |
| Activation failure | exact system asset/link snapshot auto-restored; evidence retained | `rollback_failed` requires manual stop |
| Explicit rollback | restores selected code/assets snapshot only | never restores DB/secrets |
| Offline cached retry | pinned artifact cache and existing release may be reused | a truly new Pi still needs apt/PyPI/Git dependency network |
| Multiple Pis | one strict config per Pi, same CLI/release contract | no fleet fan-out/concurrent scheduler yet |
| App commissioning | `app-ready` proves Host-side Bootstrap/BLE/network/mDNS/TLS descriptor gate | real phone E2E still required |

## Safety conclusion

The implementation is complete enough for review and isolated execution. It is not yet approved for a formal Pi:
full native build, systemd verify, network transition, reboot, rollback injection, thermal/resource soak and real-phone
commissioning must pass on isolated hardware after explicit authorization.
