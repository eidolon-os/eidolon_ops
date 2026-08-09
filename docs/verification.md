# Verification report

Updated: 2026-08-09 (Asia/Shanghai). Superseding evidence: release upload now uses digest-guarded resumable rsync
staging. A 494-MiB obsolete candidate reached Pi-native preparation without activation: Kernel prepared, then Data
dependency installation failed closed after five PyPI retries for `packaging==26.3` timed out. No product unit,
current link, secret or database was activated by that attempt. A dedicated Ed25519 SSH key passes BatchMode with strict checking against the
recorded key for `eidolon-pi5@192.168.100.15`; no password was persisted. No Mac service lifecycle or dirty sibling
working tree was changed. The Pi was changed only inside the reviewed cleanup/Foundation boundaries described below;
no new Eidolon release was uploaded, prepared or activated, and no database contents were read or written.

The target is a Raspberry Pi 5 Model B running Debian 13 arm64 with systemd PID 1, 8-GB-class RAM and more than 200
GiB free on the NVMe root. The no-data-wipe reset was applied after its dry-run: it stopped the reconciler before
workers, disabled the fixed product units, removed `/srv/eidolon`, `/etc/eidolon`, the old unit/polkit/Avahi assets,
runtime paths and three stale release staging directories. A repeated reset plan returned `detected=[]` and
`staging=[]`. The authority roots were deliberately preserved.

Foundation v2 was then installed and re-proved idempotently. All 34 exact Debian/Raspberry Pi packages are present;
BlueZ, NetworkManager and Avahi are enabled and active; NATS 2.14.0, LiveKit 1.11.0, Node 22.23.2 and uv 0.11.15 all
match the pinned versions. The final read-only doctor returned `status=healthy`, including exact artifact evidence in
`/var/lib/eidolon-ops/foundation-v2.json`. Temporary apt sources did not modify `/etc/apt`. Direct upstream downloads
were unreliable from this network, so exact artifacts were cache-seeded only after SHA-256 verification; the target
installer still uses code-owned upstream URLs and re-verifies the cache.

The post-reset product state is intentionally empty: every one of the 14 Eidolon units is `not-found/inactive`, all
seven `/opt/eidolon/current` component links are absent, and TCP listeners are only SSH 22 and rpcbind 111. Local API
9002 refuses connections; both `_eidolon-local-api._tcp` and `_eidolon-hub._tcp` Avahi queries are empty. `app-ready`
returns structured `degraded` evidence rather than crashing. Wi-Fi remains a hardware-acceptance risk: observed SSH
timeouts and multi-second ICMP delivery require retry/resume testing before production use.

The local-only `init-inputs` command then created the real 14-file Pi input set under the ignored operator-private
directory. It imported only allowlisted external provider variables, generated internally consistent cross-service
tokens and a 32-byte raw Ed25519 identity, transformed the three exact settings Git objects for product FHS paths,
wrote all files mode 0600 under a mode-0700 directory, and returned `already_initialized` on a second run without
reading secret values. No input content was printed or committed.
The independent pre-install validator returned `compatible pi-private-inputs-v1 14` for the real set after re-reading
all env key sets, fixed paths, cross-service token relationships, Host identity length and all three pinned settings
Git objects. Required Agent/Channel/Memory provider credentials, plus optional Channel provider credentials when
present, were re-compared byte-for-byte with the current Mac source `.env` files without printing any value.

## Workstation operations project

```text
uv run ruff check .
All checks passed

uv run ruff format --check .
35 files already formatted

pytest --cov=eidolon_ops --cov-branch --cov-report=term-missing --cov-fail-under=90 -q
312 passed, 0 failed, 0 skipped
branch-aware coverage: 90.06%
pytest runtime reported: 3.76 seconds

uv build --out-dir /private/tmp/eidolon-ops-build-20260809-final-1
sdist: 158,426 bytes; wheel: 62,154 bytes

python3 -m venv /private/tmp/eidolon-ops-wheel-20260809-final-1
.../pip install --no-deps .../eidolon_ops-0.1.0-py3-none-any.whl
.../eidolon-ops --help
.../eidolon-pi --help
wheel install plus unified and lower-level Pi parser smoke: passed; neither public parser exposes legacy `expand`
```

Tests cover strict config, shell-free SSH/SCP construction, Python-missing bootstrap, foundation platform/package/
artifact/service gates, digest mismatch, safe tar handling, idempotent managed links, foundation failure evidence,
first-install/resume/lock/secret cleanup, atomic 14-input initialization/provider allowlists/token relationships/
exact-settings overlays/idempotency/drift refusal, Data V2 baseline, core-to-full expansion/input drift, post-
activation doctor/App failure recovery, `/srv` replacement/abort/interrupted cleanup/nested-mount refusal, generated
mode-0600 LiveKit credentials and redacted mismatch logs, unified Mac/Pi adapters, lifecycle, rollback plan, bounded
logs, redacted diagnosis and compatibility with the pinned Kernel release constants.

## Kernel release boundary

```text
uv run ruff check .
All checks passed

pytest --cov=eidolon_deploy --cov-branch --cov-report=term-missing --cov-fail-under=90 -q
244 passed, 0 failed, 6 skipped
eidolon_deploy branch-aware coverage: 90.12%
pytest runtime reported: 14.15 seconds
```

The selected contract is 8 source archives, 7 components, 23 assets, 11 required secrets, 14 affected units and 13
readiness checks. Tests include exact Git object verification, exclusion of working-tree changes, Channel unhydrated
LFS pointer rejection, target preparation cleanup, snapshot/automatic/explicit rollback, concurrency, TCP/systemd/
generic-2xx probes and systemd verification command failure injection.

## Exact repository bundle and systemd matrix smoke

The next exact bundle smoke is configured with these selected commits:

```text
Kernel   267d5dad38e2c2a83d56f0f8e2bf6fb42897fb61
Data     d81086e2807f44ca0c0e43e31103cd85e6165a46
Hub      4bab6a0c5201c6adda7ba0f68241297034326cae
Admin    987c69282a5a91361b0e9d20144bb7163b8241b3
Agent    309ba573f249f9376275e14a5cc2f5ea1049b022
Channel  8843de6c1268bf01bf8e303ce26fb209df3e033b
Memory   303b6004c58abbf86eb311de1f4002748fa9457d
SDK      8108970514d9fefd3d93e7466e91706a1681c331
```

Local preflight proves that the release CLI itself is a clean worktree at the exact pinned Kernel commit, all 15 unit
blobs pass the FHS matrix, and the regenerated 14-file input set passes the new Hub/Admin/Provider token relations.
The selected matrix's 480-MiB exact bundle passed local digest validation and guarded resumable upload. A resumed
target-native attempt completed Kernel, Data, Hub and Admin, then spent more than the original 30-minute controller
window populating the Agent environment. The controller timed out while the Pi process continued under PID 1; that
process later exited without sealing and the preparer's failure path removed the unsealed release root. Immutable
staging and 437 MiB of verified uv cache remained, while zero product units were installed. Ops now requires an
explicit credential-free HTTPS default index plus bounded uv timeout/retry values, records them in preflight evidence,
and allows 60 minutes for the complete native phase. Frozen locks can retain direct PyPI artifact URLs, so this is not
an offline wheel bundle and must not be reported as one.

## Foundation artifact evidence

The four pinned arm64 inputs were cross-checked against their upstream release metadata. NATS 2.14.0 and Node
22.23.2 were downloaded and matched the code-owned SHA-256 values. PyPI metadata maps the pinned uv 0.11.15 digest
to the 23,066,178-byte aarch64 wheel and reports no known vulnerability for that release; 0.11.14 was rejected because
PyPI reports its entry-point path traversal advisory. The complete 15,478,055-byte LiveKit v1.11.0 arm64 archive was
also downloaded, matched the profile's `6741466b...e5a87ff` digest and contained the expected `livekit-server`.
A deliberately interrupted transfer retains only a stable `.partial` for resumable download; incomplete or
digest-mismatched content is never promoted. The uv install was additionally changed from ambient pip-index
resolution to the exact official aarch64 wheel, verified in cache and installed with `--no-index --require-hashes`.

## Product hard gates found by exact-commit audit

The former 8090 Provider and missing Admin consumer-contract gates are closed in the selected commits. Channel owns
an authenticated Provider on 8767; Kernel owns its unit, readiness and lifecycle; Hub consumes it. Admin owns the
Controller-authenticated onboarding target/admission workflow and derives Owner/controller authority server-side.
These are product implementations, not Ops compatibility handlers.

Pinned transport assets retain a trust blocker: Hub listens on plaintext loopback `127.0.0.1:8082`, but its public
base URL requires HTTPS. The 15-unit matrix contains no TLS terminator, Hub certificate/key input or LAN HTTPS
readiness probe. LiveKit likewise has no device-verifiable WSS origin. Mobile can consume only an installation-verified
Hub leaf SPKI, while the ESP firmware requires a trusted public CA or an authenticated provisioning channel. Neither
client may learn trust from mDNS or disable certificate verification.

## Not executed; hardware acceptance remains

- Permanent deletion of the three old authority roots; the user authorized clean replacement, but the final matrix
  gate has not yet reached the destructive install phase. Their metadata-only inventory includes old Data/Hub/Kernel/eidolond SQLite files, release receipts,
  Admin job roots and Bootstrap identity/TLS/database files.
- Complete Pi-native preparation for all seven component environments. The selected matrix completed four and reached
  Agent before the old 30-minute controller window expired. A retry can reuse the exact staging and 437-MiB uv cache;
  direct artifact URLs in frozen locks still require their recorded CDN to be reachable.
- Real `systemd-analyze verify`, 15-unit start order and health after clean install; Local API/Hub/provider ports and
  mDNS must be re-measured after activation.
- Full reboot recovery, concurrent operator race, disk-full/power-loss/failure auto-restore and explicit rollback on
  the Pi.
- Real phone BLE/Host proof/TLS SPKI/claim/Wi-Fi/Workspace flow and long-running voice/Memory/Agent/Channel path.
- Artifact signatures/trust root, A/B image rollback and authority-owned schema/data backup workflows.

Therefore the Pi Foundation and Ops clean-install mechanics are ready for the next authorized destructive test, but
the whole product is not yet safe-approved or device-conversation-ready. Clean replacement is authorized; the open
product blockers are complete target-native preparation plus device-verifiable Hub HTTPS/LiveKit WSS, followed by
activation and hardware acceptance.
