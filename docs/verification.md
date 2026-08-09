# Verification report

Updated: 2026-08-09 (Asia/Shanghai). No Pi state, formal database, Mac service lifecycle or sibling working tree was
modified. A dedicated existing Ed25519 SSH key now passes BatchMode with strict checking against the recorded key for
`192.168.100.15`; no password was persisted. No upload, package install, systemd mutation, database access or
activation was performed.

The probe verified a Raspberry Pi 5 Model B, Debian 13 arm64, systemd PID 1, 8-GB-class RAM, NVMe root with more than
200 GiB free, non-interactive sudo, active NetworkManager/BlueZ/Avahi, an unblocked Bluetooth controller, healthy
Bootstrap preflight and Local API HTTPS descriptor, and correct Host identity/TLS modes. It also found that this is
not a blank Host: the active release manages Kernel/Data/Hub/Admin under the legacy `/srv/eidolon` namespace. The six
new NATS/LiveKit/Memory/Agent/Channel units are not installed, and seven foundation packages plus the pinned
uv/Node/NATS/LiveKit artifacts remain absent. Consequently a normal first install is correctly inapplicable. The
selected operational policy is now a clean reset/reinstall, not core expansion or `/srv` migration. The refreshed
`status` observed eight loaded/active legacy units, no `/opt` current links, and no NATS/LiveKit/Memory/Agent/Channel
units. The foundation plan observed healthy BlueZ/NetworkManager/Avahi but missing pinned uv/Node/NATS/LiveKit and
several apt prerequisites. `app-ready` fails because the `/opt` Admin preflight executable does not exist. A reset plan
listed the fixed `/srv`, unit/config/runtime, authority-data and stale staging paths; it did not apply the deletion.

The local-only `init-inputs` command then created the real 14-file Pi input set under the ignored operator-private
directory. It imported only allowlisted external provider variables, generated internally consistent cross-service
tokens and a 32-byte raw Ed25519 identity, transformed the three exact settings Git objects for product FHS paths,
wrote all files mode 0600 under a mode-0700 directory, and returned `already_initialized` on a second run without
reading secret values. No input content was printed or committed.
The independent pre-install validator then returned `compatible pi-private-inputs-v1 14` for the real set after
re-reading all env key sets, fixed paths, cross-service token relationships, Host identity length and all three
pinned settings Git objects.

## Workstation operations project

```text
uv run ruff check .
All checks passed

uv run ruff format --check .
35 files already formatted

pytest --cov=eidolon_ops --cov-branch --cov-report=term-missing --cov-fail-under=90 -q
286 passed, 0 failed, 0 skipped
branch-aware coverage: 90.10%
pytest runtime reported: 2.34 seconds

committed isolated clone (no sibling repositories): 284 passed, 2 skipped; branch-aware coverage 90.10%; 4.03
seconds. The two skips are the optional Kernel and Data/Hub cross-repository contract modules.

uv build --out-dir /private/tmp/eidolon-ops-build-2746d4e
sdist: 153,195 bytes; wheel: 59,764 bytes

python3 -m venv /private/tmp/eidolon-ops-wheel-2746d4e
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

The contract result is 8 source archives, 7 components, 22 assets, 11 required secrets, 13 affected units and 12
readiness checks. Tests include exact Git object verification, exclusion of working-tree changes, Channel unhydrated
LFS pointer rejection, target preparation cleanup, snapshot/automatic/explicit rollback, concurrency, TCP/systemd/
generic-2xx probes and systemd verification command failure injection.

## Exact repository bundle and systemd matrix smoke

The final smoke used the committed Kernel revision plus these selected commits:

```text
Kernel   7de97bd8d87b7e3a84109053d1dd98e5f19b0058
Data     d81086e2807f44ca0c0e43e31103cd85e6165a46
Hub      96438a2507fb76ad025824873a99b213a99016ad
Admin    1ac5c733811c785e70992f527e516fd715b86f5b
Agent    309ba573f249f9376275e14a5cc2f5ea1049b022
Channel  3bc7e3303fa2c06bcfe0a82dacacd390f7deb372
Memory   303b6004c58abbf86eb311de1f4002748fa9457d
SDK      8108970514d9fefd3d93e7466e91706a1681c331
```

The candidate bundle produced 8 exact-commit source archives plus the preparer and manifest under
`/private/tmp/eidolon-release-bundles/20260809-full-r2`, totalling 503,483,477 bytes (480 MiB on disk). Channel accounts
for 490,188,800 bytes after hydrating its 8 Git LFS model objects from the exact commit pointers. The bundler verified
each source revision and archive digest and rejected all working-tree content. The Admin archive contains the three
corrected unit blobs with SHA-256 values `bf6955a7...43e9eb1`, `755a0474...555405` and
`127245ef...83ea65`, exactly matching the independent 14-unit matrix gate. This smoke stopped after local bundle and
private-input validation; it performed no upload or Pi-native preparation.

The final matrix was followed by 14/14 passing Kernel focused `tests/deploy/test_bundle.py` tests.

The prior `02f96b7` archive was not a release candidate because its three Admin units failed the FHS gate. That defect
is resolved only by the pinned `1ac5c73` commit above. A real Pi install dry-run now reports
`release_matrix.status=compatible` for 14 units and `pi-private-inputs-v1` validates all 14 private files. The same
dry-run reports the Pi foundation as degraded because 9 apt packages and pinned uv/Node/NATS/LiveKit binaries are
absent; `install --apply` must provision those before native release preparation.

## Foundation artifact evidence

The four pinned arm64 inputs were cross-checked against their upstream release metadata. NATS 2.14.0 and Node
22.23.2 were downloaded and matched the code-owned SHA-256 values. PyPI metadata maps the pinned uv 0.11.15 digest
to the 23,066,178-byte aarch64 wheel and reports no known vulnerability for that release; 0.11.14 was rejected because
PyPI reports its entry-point path traversal advisory. The complete 15,478,055-byte LiveKit v1.11.0 arm64 archive was
also downloaded, matched the profile's `6741466b...e5a87ff` digest and contained the expected `livekit-server`.
A deliberately interrupted transfer failed both archive and digest validation, confirming that the installer must
reject incomplete cache content.

## Not executed; hardware acceptance remains

- Real SCP/upload and Raspberry Pi foundation mutation; SSH/sudo were exercised only by bounded read-only probes.
- `apt` and fixed artifact downloads on Raspberry Pi OS 12/13; package availability on both versions.
- Pi-native `uv sync` for all 7 environments, model load time, disk/RAM/thermal profile.
- Real `systemd-analyze verify`, 14-unit start order, BlueZ/NetworkManager/Avahi and NetworkManager SSH continuity.
- First install, full reboot recovery, concurrent operator race, disk-full/power-loss/failure auto-restore and explicit
  rollback on isolated hardware.
- Real phone BLE/Host proof/TLS SPKI/claim/Wi-Fi/Workspace flow and long-running voice/Memory/Agent/Channel path.
- Artifact signatures/trust root, A/B image rollback and authority-owned schema/data backup workflows.

Therefore the code is suitable for review and local isolation, but is not yet safe-approved for a formal Raspberry
Pi until those tests pass with explicit authorization.
