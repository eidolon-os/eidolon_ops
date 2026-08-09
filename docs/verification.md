# Verification report

Updated: 2026-08-09 (Asia/Shanghai). No Pi state, formal database, Mac service lifecycle or sibling working tree was
modified. Password authentication to `192.168.100.15` succeeded and the only remote command executed was the
read-only `/usr/bin/id`; the session was closed immediately when the release matrix changed, before environment
probing, upload or activation.

## Workstation operations project

```text
uv run ruff check .
All checks passed

uv run pytest --cov=eidolon_ops --cov-report=term-missing --cov-fail-under=90 -q
167 passed, 0 failed, 0 skipped
branch-aware coverage: 91.91%
revised-matrix pytest runtime reported: 1.15 seconds
marker breakdown: 60 unit, 102 component, 4 Kernel-contract, 1 subprocess integration

uv build --out-dir /private/tmp/eidolon-ops-build-final2
sdist: 84,591 bytes; wheel: 32,942 bytes

python3 -m venv /private/tmp/eidolon-ops-wheel-smoke
.../pip install --no-deps .../eidolon_ops-0.1.0-py3-none-any.whl
.../eidolon-pi --help
wheel install and all 13 CLI operation parsers: passed
```

Tests cover strict config, shell-free SSH/SCP construction, Python-missing bootstrap, foundation platform/package/
artifact/service gates, digest mismatch, safe tar handling, idempotent managed links, foundation failure evidence,
first-install/resume/lock/secret cleanup, Data V2 baseline, App gate failure/recovery, lifecycle, rollback plan, bounded
logs, redacted diagnosis and compatibility with the live Kernel release constants.

## Kernel release boundary

```text
uv run ruff check .
All checks passed

uv run pytest --cov=eidolon_deploy --cov-report=term --cov-fail-under=90 -q
247 passed, 0 failed, 0 skipped
eidolon_deploy branch-aware coverage: 90.32%
```

The contract result is 8 source archives, 7 components, 22 assets, 11 required secrets, 13 affected units and 12
readiness checks. Tests include exact Git object verification, exclusion of working-tree changes, Channel unhydrated
LFS pointer rejection, target preparation cleanup, snapshot/automatic/explicit rollback, concurrency, TCP/systemd/
generic-2xx probes and systemd verification command failure injection.

## Exact repository bundle smoke

The final smoke used the committed Kernel revision plus these selected commits:

```text
Kernel   cf668a338c0f6305164cccd49df16eaa7e92aa04
Data     d81086e2807f44ca0c0e43e31103cd85e6165a46
Hub      96438a2507fb76ad025824873a99b213a99016ad
Admin    02f96b7ca4fc0b662dcfdfdb0c8d2293d3cfd8d0
Agent    309ba573f249f9376275e14a5cc2f5ea1049b022
Channel  3bc7e3303fa2c06bcfe0a82dacacd390f7deb372
Memory   303b6004c58abbf86eb311de1f4002748fa9457d
SDK      8108970514d9fefd3d93e7466e91706a1681c331
```

The revised-matrix command completed in 1.98 seconds and produced 8 exact-commit source archives plus the preparer,
totalling 480 MiB under `/private/tmp/eidolon-full-product-bundle-matrix2`. Channel accounts for 467 MiB after
hydrating its 8 Git LFS model objects from the exact commit pointers. Each object was checked against the pointer
SHA-256 and size;
the final archive was scanned again and contained no LFS pointer payload. Four changed Admin production files were
independently compared with their `02f96b7` Git blobs and matched byte-for-byte. This smoke stopped after local bundle
validation; it performed no upload or Pi-native preparation.

The revised matrix was followed by `14 passed in 7.65s` for Kernel's focused `tests/deploy/test_bundle.py` suite.

## Foundation artifact evidence

The four pinned arm64 inputs were cross-checked against their upstream release metadata. NATS 2.14.0 and Node
22.23.2 were downloaded and matched the code-owned SHA-256 values. PyPI metadata maps the pinned uv 0.11.15 digest
to the 23,066,178-byte aarch64 wheel and reports no known vulnerability for that release; 0.11.14 was rejected because
PyPI reports its entry-point path traversal advisory. The complete 15,478,055-byte LiveKit v1.11.0 arm64 archive was
also downloaded, matched the profile's `6741466b...e5a87ff` digest and contained the expected `livekit-server`.
A deliberately interrupted transfer failed both archive and digest validation, confirming that the installer must
reject incomplete cache content.

## Not executed; hardware acceptance remains

- Real SCP/sudo and Raspberry Pi foundation mutation; SSH authentication and `/usr/bin/id` only were exercised.
- `apt` and fixed artifact downloads on Raspberry Pi OS 12/13; package availability on both versions.
- Pi-native `uv sync` for all 7 environments, model load time, disk/RAM/thermal profile.
- Real `systemd-analyze verify`, 14-unit start order, BlueZ/NetworkManager/Avahi and NetworkManager SSH continuity.
- First install, full reboot recovery, concurrent operator race, disk-full/power-loss/failure auto-restore and explicit
  rollback on isolated hardware.
- Real phone BLE/Host proof/TLS SPKI/claim/Wi-Fi/Workspace flow and long-running voice/Memory/Agent/Channel path.
- Artifact signatures/trust root, A/B image rollback and authority-owned schema/data backup workflows.

Therefore the code is suitable for review and local isolation, but is not yet safe-approved for a formal Raspberry
Pi until those tests pass with explicit authorization.
