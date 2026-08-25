# Eidolon OS Raspberry Pi deployment / operations audit

Audit updated: 2026-08-10 (Asia/Shanghai). The Mac product-source stack was restarted after isolated validation to
activate the deployment-owned development LAN ingress and discovery configuration. On the
Pi, the fixed legacy code/unit/config/staging namespace was removed without a data wipe, then the non-product
Foundation v2 was installed and verified. One obsolete candidate was uploaded and entered native preparation; it
failed closed on a PyPI timeout before sealing/activation, so no product service or authority data changed.

## Current code evidence

The multi-repository root is not Git.

> **Overturned (2026-08-25).** This section's design intent — "the selected release commits remain explicit even when
> sibling HEADs advance", and "current sibling branches and dirty working trees remain outside the release input" —
> no longer holds, and the table below is a historical record rather than a description of how a release is now
> selected. A release is sealed from each repository's HEAD; there is no second copy of the commit list to keep
> equal. The intent was overturned because it was the direct cause of an incident: a tested cross-repository change
> never reached the Pi, `deploy` reported success, and three subsequent deploys failed with `readiness timeout: hub,
> kernel, local-api` — all because the explicit commits were older than the code that had been tested. Explicitness
> survives where it carries information: `--revision`/`[sources.*].revision` reproduce an exact combination for one
> run and label themselves as doing so. Dirty working trees are still outside the release input, and are now refused
> rather than silently ignored (`--allow-dirty` to override, recorded in Host evidence). See `docs/runbook.md`,
> "What a release is built from".

The commits selected for the 2026-08-10 release were:

| Source | Selected exact release input | Role |
| --- | --- | --- |
| Kernel | `ab171cf9c8d2a1b837161dcddc080a641a3120c4` | FHS release authority + closed-shape ARM64 dependency bundle |
| Data | `d81086e2807f44ca0c0e43e31103cd85e6165a46` | Data V2 + Workspace/runtime authority |
| Hub | `4bab6a0c5201c6adda7ba0f68241297034326cae` | proof-bound onboarding + dedicated Provider port |
| Admin | `987c69282a5a91361b0e9d20144bb7163b8241b3` | Mobile admission orchestration + isolated FHS systemd fix |
| Agent | `309ba573f249f9376275e14a5cc2f5ea1049b022` | session-authorized Companion brain + authority E2E |
| Channel | `95164e3dbebb31fb6f6152a7da549f8d6f8c0f35` | voice runtime + formal Hub Channel Provider + explicit Mac LAN development transport |
| Memory | `303b6004c58abbf86eb311de1f4002748fa9457d` | supervisor/discovery, no direct Data integration |
| SDK | `8108970514d9fefd3d93e7466e91706a1681c331` | runtime-authority/session support source |

The selected matrix is now a deployable **candidate**: Admin commit `987c692` combines the formal Mobile admission
workflow with the isolated FHS systemd fix. The upload preflight reads all 15 units directly from
their Git objects and accepted the `/etc/eidolon/host.env`, `/opt/eidolon/current/eidolon_admin/` and FHS state-path
contracts. The 15-unit exact-object matrix and 14-file private-input contract passed locally. Bundle schema v2 also
passed a real Mac-prefetch/compress/extract/offline-build smoke; the final new-Kernel exact bundle is not yet uploaded. Current sibling
branches and dirty working trees remain outside the release input: Ops does not stage, overwrite or copy them —
though a dirty worktree is now a refusal rather than something quietly skipped past (see the note above). The
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
Pi device-conversation blocker is the deployment-owned LAN TLS/WSS ingress and certificate trust described below.

For Mac source development only, Ops now owns a stable self-signed Hub leaf certificate, a TLS relay from LAN 8443
to Hub loopback 8082, Local API trust-source configuration, `_eidolon-local-api._tcp` registration and an explicit
private-LAN `ws://` LiveKit client opt-in. This closes the host-side transport gate needed for local Pad testing while
retaining Hub SPKI verification. It is deliberately not part of the Pi product release contract.

The Mac Host profile may additionally override one exact source path and commit without changing the shared Pi
release matrix. The current Mac-only Admin input is `4b05f9f8d2e3aa484fa91c735b15b59be23fc64d`. It exposes a
development-only LAN commissioning transport when Bootstrap is in development mode with the disabled commissioning
adapter and memory network adapter. Discovery remains untrusted; Mobile verifies the Host-signed commissioning
endpoint, then uses the advertised SPKI pin for the existing short-code Controller claim. Production and Pi BLE
commissioning do not enable this route. The corresponding Mobile debug client is
`f7d2f51a52bf9d55755d1624130c5d64f4b19d9e`.

The release-owned Hub remains one plaintext authority process on `127.0.0.1:8082`. Ops now supplies the missing Host
configuration layer without mutating commit-pinned release assets: a Host-key-derived Hub ID/`.local` origin, stable
pinned P-256 leaf, generated Hub settings selected through a systemd environment overlay, and a hardened
`eidolon-hub-ingress.service` on 8443. SRV port, TXT descriptor, Hub descriptor, Local API target and certificate SAN
come from one contract. `app-ready` rejects the generic template identity, cross-Host resolution or an unreachable
advertised endpoint. Product WSS for LiveKit remains a separate release gate.

## Existing capability, previous gap, implemented closure

Kernel already owned exact commit bundle, target-native frozen environments, sealing, snapshot, ordered quiesce,
atomic asset/link switch, readiness, receipts, automatic restore and explicit rollback. The historical
`eidolon_pi_deploy` instead patched detached clones, restored the legacy database and ran one supervisord unit; it
conflicts with current Data V2/systemd authority and was rejected.

The previous Kernel contract covered only the core control path. This change extends the formal contract to 8 source
archives, 7 components, 22 system assets, 11 private prerequisites, 14 affected units and 13 readiness checks, with
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
| Offline cached retry | exact source and Python cache bundle may be resumed/reused | a new Pi needs network for apt/foundation artifacts, not PyPI during product prepare |
| Multiple Pis | one strict config per Pi, same CLI/release contract | no fleet fan-out/concurrent scheduler yet |
| App commissioning | `app-ready` proves Host-side Bootstrap/BLE/network/mDNS/TLS descriptor gate | real phone E2E remains |
| Mac device conversation | Hub approval obtains a real Channel Assignment; development LAN ingress is host-ready | real Pad onboarding/audio/Agent E2E remains |
| Pi device conversation | Hub approval obtains a real Channel Assignment from the Provider | LAN Hub HTTPS and LiveKit WSS trust remain hard blocked |

## Safety conclusion

The selected release matrix, exact bundle, private inputs and real Pi Foundation pass their respective gates. A
clean-install dry-run now detects only the three preserved old authority roots. The user authorized clean replacement,
but deletion remains gated behind final exact-bundle validation. Even after activation, it must not be called “all
devices ready” until Hub HTTPS and LiveKit WSS have a device-verifiable trust path. Target-native build, systemd
verification, activation health, network transition, reboot, rollback
injection, thermal/resource soak and real-phone commissioning remain hardware acceptance work.
