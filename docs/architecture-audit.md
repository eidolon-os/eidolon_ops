# Eidolon OS Raspberry Pi deployment / operations audit

Audit date: 2026-08-07 (Asia/Shanghai). All process and repository checks were read-only; no Raspberry Pi command
was executed and no Mac service was changed.

## Evidence baseline

| Repository | Audited commit | Workspace state relevant to this work |
| --- | --- | --- |
| `eidolon_kernel` | `27dd8c9ed47ca8eeb0776abdadb3ff3dd0631e9e` | clean; 4 commits ahead of `origin/main` |
| `eidolon_data` | `e3afb78ccd0b42b01614fe3d6e9d89733798ebc9` | parallel runtime-snapshot changes; read-only |
| `eidolon_hub` | `96438a2507fb76ad025824873a99b213a99016ad` | clean; 9 commits ahead |
| `eidolon_admin` | `41b15d14b8597b87849664cb35a63d985e02d727` | `.coverage` untracked; read-only |
| `eidolon_sdk` | `d76fe046bc6eb21d584c20c6613d0918acbf76e6` | parallel System Data changes; read-only |

The current Mac supervisord observation was `admin-api=STOPPED`, `agent=STOPPED`, `hub-api=FATAL`; Audit,
Channel, Client Web, Memory, NATS and LiveKit were already running. This task did not start, stop or restart them.

## Current product topology and authority direction

```text
Mac operator: eidolon-pi
  -> BatchMode SSH/SCP with an explicit known_hosts file
  -> target-native eidolon-release / target installer

systemd
  -> eidolon-bootstrapd       # Admin-owned Host identity / commissioning authority
  -> eidolon-local-api        # authenticated product ingress
  -> eidolon-admin            # loopback operator orchestration; no sibling DB access
  -> eidolond                 # sole Data/Hub/Kernel desired-state authority
       -> eidolon-data
       -> eidolon-data-workspace
       -> eidolon-hub
       -> eidolon-kernel

Admin -> eidolond directory -> Data / Hub / Kernel public application contracts
```

The deployer never opens or copies Data, Hub, Kernel, Bootstrap or eidolond SQLite. Data V2 is created only from
the clean `0001_system_data_v2` Alembic baseline. Legacy `eidolon.sqlite3` and old migrations are rejected.

## Existing capability and gap

Kernel already owns the hard transaction: exact `git archive` for five commits, safe tar validation, Pi-native
`uv sync --frozen`, source/lock/environment sealing, immutable descriptor, complete preflight, non-blocking host
lock, snapshots, ordered quiesce, atomic asset/link switch, readiness gates, receipts, automatic restore and
explicit rollback. The current code contract is 4 service components + SDK, 15 assets, 7 prerequisite files,
7 affected child/operator units and 7 readiness checks. Earlier 14/6 documentation is stale.

Missing before this project: one strict Mac configuration and CLI, first Eidolon installation, whole-host
lifecycle, read-only status, journal access, redacted diagnostics and resumable orchestration. Artifact signing,
power-loss atomicity, target package bootstrap and product unit contracts for Agent/Channel/Memory/NATS/LiveKit
remain outside the verified boundary.

The sibling `eidolon_pi_deploy` directory is a v0.1.0 historical path. It applies patches onto detached clones,
creates an old `eidolon.sqlite3`, runs the whole stack under one supervisord unit and manages Caddy/dnsmasq/UFW.
Those choices conflict with Data V2 and the current eidolond/systemd authority model, so it is not reused.

## Option matrix

| Option | Reuse / safety | Boundary fit | Decision |
| --- | --- | --- | --- |
| Extend historical `eidolon_pi_deploy` | low; old schema and patched trees | conflicts with current authority | reject |
| Grow Kernel's shell driver into all remote operations | high release reuse | mixes target root transaction with workstation UX and first install | reject |
| Put remote deployment in Admin | would expose convenient UI | Admin must not become root deployer or DB aggregator | reject |
| Independent Mac operator CLI calling Kernel release contracts | maximal transaction reuse; isolated config/tests | preserves Kernel/Admin/Data/Hub ownership | selected |

## Safety conclusion

This CLI can be used against mock SSH and an isolated filesystem without touching a formal environment. It must
not be described as production-ready for a real Pi until the exact pinned revisions complete install/update,
fault rollback, explicit rollback, concurrency, full reboot and long-running diagnostics on isolated hardware.
