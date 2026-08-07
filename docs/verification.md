# Verification report

Date: 2026-08-07 (Asia/Shanghai)

No command in this verification connected to a Raspberry Pi, changed a formal database, or started/stopped/restarted
the existing Mac stack.

## Workstation project

Commands:

```bash
UV_CACHE_DIR=/private/tmp/eidolon-ops-uv-cache uv lock --check
UV_CACHE_DIR=/private/tmp/eidolon-ops-uv-cache uv run ruff check .
UV_CACHE_DIR=/private/tmp/eidolon-ops-uv-cache uv run ruff format --check .
PYTHONPYCACHEPREFIX=/private/tmp/eidolon-ops-pyc \
  UV_CACHE_DIR=/private/tmp/eidolon-ops-uv-cache \
  uv run python -m compileall -q src tests
UV_CACHE_DIR=/private/tmp/eidolon-ops-uv-cache \
  uv run pytest -q --cov=eidolon_ops --cov-branch --cov-report=term-missing
UV_CACHE_DIR=/private/tmp/eidolon-ops-uv-cache uv build
```

Results:

- Ruff lint and format: passed.
- Compileall: passed.
- Tests: **136 passed, 0 failed, 0 skipped** in **0.96 s**;
  branch-aware total coverage **90.66%** (90% gate).
- Build: sdist `eidolon_ops-0.1.0.tar.gz` and universal wheel
  `eidolon_ops-0.1.0-py3-none-any.whl` built successfully in **3.53 s**.
- Installed entrypoint help exposed all 11 operations and the wheel contained only the seven runtime modules plus
  distribution metadata.

The first sandboxed build could not resolve the isolated `hatchling` backend because DNS was denied. The explicitly
approved network retry succeeded. No application dependency is downloaded at target-operation time by this project;
Pi-native dependency resolution remains in the existing fixed `uv sync --frozen` preparer.

Test classes include unit, component, contract and real-local-subprocess integration coverage. Failure injection
exercises prepare abort, install phase failure, secret cleanup failure, mismatched resume input, mode drift, existing
authority refusal, non-blocking install lock, service-start recovery and idempotent completed re-entry.

## Existing Kernel release boundary

Baseline deployment-only result before implementation:

```text
tests/deploy: 85 passed in 4.33 s
```

Final full Kernel/System/Deploy regression (isolated loopback/Unix sockets explicitly permitted):

```text
239 passed, 0 failed, 0 skipped in 23.56 s
branch-aware coverage: 91.55% (90% gate)
```

The first sandboxed full run had exactly two `PermissionError` failures while binding isolated loopback ports;
235 tests passed and 2 were skipped. The permitted rerun passed all 239. No existing service port or database was used.

## Real repository bundle smoke

The existing `eidolon-release bundle` archived these exact committed objects, not working-tree content:

```text
Kernel  27dd8c9ed47ca8eeb0776abdadb3ff3dd0631e9e
Data    e3afb78ccd0b42b01614fe3d6e9d89733798ebc9
Hub     96438a2507fb76ad025824873a99b213a99016ad
Admin   41b15d14b8597b87849664cb35a63d985e02d727
SDK     d76fe046bc6eb21d584c20c6613d0918acbf76e6
```

Result: five verified source archives plus the standalone preparer, **5.7 MiB**, **0.58 s** wall time. The smoke
stopped before any SSH transfer or target preparation.

## Read-only environment observation

The Mac supervisord observation remained: Admin and Agent STOPPED; Hub FATAL; Audit, Channel, Client Web, Memory,
NATS and LiveKit RUNNING. Sibling Data/Agent/Channel/SDK changes were preserved. Parallel work added further Admin
documentation and Channel lock/config changes during this task; they were not staged or edited here.

## Not executed / acceptance still required

- SSH/SCP against the real Pi, target-native dependency download timing and prepare/seal.
- First install on a clean Raspberry Pi image, real user/group/Polkit/Avahi/BlueZ/NetworkManager integration.
- Real dry-run, activation, readiness, injected failure auto-restore, explicit rollback and concurrent operator race.
- Power interruption, whole-host reboot recovery, filesystem-full behavior and long soak/resource/thermal diagnostics.
- Product systemd contracts for Agent, Channel, Memory, NATS, LiveKit and Web client.
- Artifact signing/trust root, A/B partitions and database migration/backup semantics.

Therefore the implementation is suitable for review and isolated validation, but is **not yet approved for a formal
Raspberry Pi**.
