# Verification report

Updated: 2026-08-09 (Asia/Shanghai). A dedicated Ed25519 SSH key passes BatchMode with strict checking against the
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
292 passed, 0 failed, 0 skipped
branch-aware coverage: 90.09%
pytest runtime reported: 3.44 seconds

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
is resolved only by the pinned `1ac5c73` commit above. The latest real Pi clean-install dry-run reports
`release_matrix.status=compatible` for all 14 units and Foundation `status=healthy`. It detects exactly
`/var/lib/eidolon`, `/var/lib/eidolon-admin` and `/var/lib/eidolon-bootstrap` for an explicitly authorized permanent
wipe before a fresh Data V2 baseline.

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

The eight pinned Git objects were searched independently for the Hub Channel Provider routes. Hub contains only the
outbound client for `POST /v1/device-channels/provision` and `revoke`; the other seven release inputs contain no
production handler. `channel_provider.contract_url=http://127.0.0.1:8090/v1` therefore has no deployable owner in
this matrix. Exact Kernel config assigns 8090 to `eidolond`'s unrelated `/api/system/v1` service-directory API, so
the configured call targets the wrong contract. Hub process/readiness alone cannot prove device conversation, and no
compatibility Provider was added.

The current empty target confirms no listener on 8090. Until a real Provider commit, process owner and health
contract are added to the reviewed release matrix, onboarding cannot return a usable Channel Assignment. This is a
hard gate for “all services + App ready”, independent of the clean-install mechanics.

Pinned Admin `1ac5c73` was also searched for Mobile's Controller-authenticated Local API consumption routes. It has
neither `GET /api/local/v1/device-onboarding/target` nor
`PUT /api/local/v1/device-admissions/{setup_id}`. The latter appears only in an uncommitted document in the current
dirty Hub working tree, which is outside the fixed `96438a2` release input and is not executable Admin code; the
former has no workspace match. Ownership belongs to the Admin Local API/control orchestration boundary, not Ops.
Until committed, pinned and exercised, Mobile must not establish Hub TLS trust from mDNS or reuse the Host SPKI.

Pinned transport assets have a separate trust blocker: Hub listens on plaintext loopback `127.0.0.1:8082`, but its
public base URL and `_eidolon-hub._tcp` advertiser claim HTTPS port 443. The 14-unit matrix contains no TLS terminator,
Hub certificate/secret input or readiness probe for 443. A future activation could therefore publish mDNS while no
usable Hub HTTPS endpoint exists; Mobile is correct to require a Local API-supplied, verified Hub SPKI instead of
trusting that advertisement.

## Not executed; hardware acceptance remains

- Permanent deletion of the three old authority roots; no backup was created and explicit authorization remains
  required. Their metadata-only inventory includes old Data/Hub/Kernel/eidolond SQLite files, release receipts,
  Admin job roots and Bootstrap identity/TLS/database files.
- Pi-native preparation for the seven component environments, model load time and disk/RAM/thermal profile.
- Real `systemd-analyze verify`, 14-unit start order and health after clean install; Local API/Hub/provider ports and
  mDNS must be re-measured after activation.
- Full reboot recovery, concurrent operator race, disk-full/power-loss/failure auto-restore and explicit rollback on
  the Pi.
- Real phone BLE/Host proof/TLS SPKI/claim/Wi-Fi/Workspace flow and long-running voice/Memory/Agent/Channel path.
- Artifact signatures/trust root, A/B image rollback and authority-owned schema/data backup workflows.

Therefore the Pi Foundation and Ops clean-install mechanics are ready for the next authorized destructive test, but
the whole product is not yet safe-approved or App-conversation-ready. It remains blocked by the authority-data wipe
decision and the missing production Channel Provider, followed by target-native activation and hardware acceptance.
