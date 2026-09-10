# Eidolon Pi operations runbook

All examples use the unified host entrypoint. The Pi Host profile references
the lower-level commit/SSH release configuration:

```bash
uv run eidolon-ops --config /absolute/path/hosts/pi5.toml <operation>
```

## New Pi: one command from SSH-ready OS to App-ready backend

Preconditions: reviewed Debian/Raspberry Pi OS 13, outbound package/download access, and the 14 local
private inputs declared in config.

Everything that used to be listed here as well — the Host's name, a non-root account with non-interactive
sudo, the deploy key in it, and a wired link that does not spend its life waiting for a DHCP server that
cannot answer — is no longer the operator's to remember. `bring-up` renders it from the Host profile, down
whichever channel the board leaves open: its boot medium if it has never booted, a shell on it if it is
already running. The host key is recorded by `trust-host-key`, in the profile's own `known_hosts`. Both are
in the README under "从刷好的盘到可以被操作" and "换板子换的是信任".

```bash
uv run eidolon-ops --config /absolute/path/hosts/pi5.toml \
  bring-up --via boot-medium --output /Volumes/bootfs --apply

uv run eidolon-ops --config /absolute/path/hosts/pi5.toml trust-host-key --apply
```

Create the private inputs once before the first plan. This is local-only and never contacts the Pi:

```bash
uv run eidolon-ops --config /absolute/path/hosts/pi5.toml init-inputs
```

The initializer imports only the required provider credential names from the Agent/Channel/Memory source
directories' local `config/.env` files. It generates every internal cross-service token and the raw 32-byte Host
identity together, derives product settings from exact Git objects, applies fail-closed FHS overlays, writes a
mode-0700 directory with 14 mode-0600 files and refuses overwrite or template drift.

```bash
uv run eidolon-ops --config /absolute/path/hosts/pi5.toml \
  install --release-id 20260807-product-1

uv run eidolon-ops --config /absolute/path/hosts/pi5.toml \
  install --release-id 20260807-product-1 --apply
```

The first command is a no-mutation plan. The second is the single authorized operation. Its state machine is:

```text
validate Mac commands, SSH files, 8 repos/commits, 14 private inputs
  -> probe Python without mutation
  -> foundation platform/capacity/package/artifact/service doctor
  -> if needed: Python bootstrap -> apt -> hash-pinned NATS/LiveKit/uv/Node -> BlueZ/NM/Avahi
  -> exact Foundation evidence outside the product authority namespace
  -> require wired release endpoint -> exact 8-commit thin bundle
  -> query Pi CAS -> transfer only missing digest-guarded artifacts by resumable SSH/rsync
  -> explicit HTTPS Python index + bounded timeout/retries -> frozen Pi-native prepare/seal
  -> private staging -> exclusive first-install lock -> clean namespace proof
  -> identities/directories -> exact 14 input bytes -> fresh Data V2 baseline
  -> 23 assets + 7 links -> enable 4 top-level units -> ordered start
  -> 13 release readiness -> Host-side App commissioning gate -> completed journal
```

An existing unowned Eidolon database/link/secret/unit namespace fails closed. A partial install can resume only with
the same release ID and identical input digests. Temporary secret staging is removed after success or failure.

## Existing Host: clean reinstall, not `/srv` migration

The product path does not migrate or adopt an older deployment. First inspect the fixed deletion boundary:

```bash
uv run eidolon-ops --config /absolute/path/hosts/pi5.toml \
  reset --wipe-authority-data
```

For a complete replacement, the same install command performs the exact-commit local gates before any deletion,
then resets the old Host, re-provisions the foundation and installs the full product:

```bash
uv run eidolon-ops --config /absolute/path/hosts/pi5.toml \
  install --release-id 20260809-product-full \
  --reset-existing --wipe-authority-data --apply
```

`reset --apply` without the wipe flag removes the fixed code, unit, config, runtime, log and private staging paths,
but preserves `/var/lib/eidolon` and `/var/lib/eidolon-bootstrap`; it is an uninstall boundary, not a schema migration.
`--wipe-authority-data` creates a new Host/Owner (Mobile and devices must pair again) and additionally removes those authority roots and the old `/var/lib/eidolon-admin` root. It is
irreversible. Foundation packages/artifacts and service identities are preserved. Reset stops and disables only the
fixed 15 units, refuses an active unit that cannot stop, refuses mounted deletion roots, reloads systemd and is
idempotent when units or paths are already absent.

## A Host no phone can finish setting up

`app-ready` fails on `host_setup_completable`, and
`GET /api/local/v1/setup/readiness` answers `unknown`.

That fact means one thing now: this Host's Workspace authority did not answer.
Not that a phone is at fault, and not that anything on the Host has to be
repaired — the Data plane could not be asked, so the gate refuses rather than
score a check it could not make. Look at whether `data` and `data-workspace`
are up and answering; `backend_healthy` and the service health beside it are
usually already saying so.

```bash
uv run eidolon-ops --config /absolute/path/hosts/pi5.toml app-ready
```

The evidence under `setup` says which failure it was. `state: "unknown"` with
an error naming `/api/local/v1/setup/readiness` is a Local API still running
the build it started with — restart it rather than repair anything. Any other
`unknown` is the Data plane.

**There used to be a third answer here, and a verb to repair it.** Bootstrap
kept its own record that the Data plane held a Workspace for this Host's Owner,
so a data restore or a `reset --wipe-authority-data` could leave the record
behind and the Workspace gone. Every phone ever claimed onto such a Host
inherited that Owner scope and was refused at setup, identically and forever,
and nothing on any phone changed it. That is gone: Bootstrap no longer records
anything about the Data plane, the Owner a request is scoped to is resolved
from the plane that holds the Workspace, and a Host whose Workspace is missing
simply reports `absent` and can be set up again. There is nothing left to
withdraw, so there is no `owner-reset`.

## A Kernel that will not open its own authority

The Kernel performs no migrations, on purpose. After a Kernel schema change a Host that carries the old
database will not start, and `status`/`logs` show `eidolon-kernel.service` failing with
`kernel SQLite schema is partial or unknown` or `... does not match schema vN`. That refusal is correct;
what follows it is a repair, not a migration.

```bash
uv run eidolon-ops --config /absolute/path/hosts/pi5.toml kernel-schema-reset
```

Read-only. It asks the installed Kernel whether it will open the database, and how many **Owner Companion
selections** — which Eidolon answers through each Body — setting it aside would destroy. That number is the
one fact in the Kernel authority nothing on the Host can give back: device mounts are replayed from the Hub
Claim stream on the next start, but only an explicit Owner command ever writes a selection, and a backup of
this file is a backup at the schema the Kernel just refused.

```bash
uv run eidolon-ops --config /absolute/path/hosts/pi5.toml \
  kernel-schema-reset --apply --forget-selections <the number the plan reported>
```

Run the plan first and use the command it prints: the acknowledgement depends on what the census found.
`--forget-selections N` is required when the count is non-zero and must equal it. A database old enough to
predate `kernel_body_assignments` cannot be counted at all — the most common case, since a Host one schema
version behind has no such table — and then the acknowledgement is `--forget-uncounted-selections`, which
says "the loss is real and unmeasured, and I accept it". The two are not interchangeable: the uncounted
flag is refused when the Kernel did manage a count, so it can never be used to skip typing the number.

The operation refuses unless the installed Kernel itself says it will not open the file, stops
`eidolond` with the Kernel so nothing restarts it mid-rename, **renames** the database and its `-wal`/`-shm`
sidecars to `<name>.stale-schema-<UTC timestamp>` beside themselves, and starts the reconciler again. Nothing
is deleted, and no other authority is touched — in particular the Owner Domain generation does not advance,
so every Claim and credential stays valid. Do not reach for `reset --wipe-authority-data` for this: it
destroys every authority on the machine and creates a new Host/Owner requiring pairing.

The same command exists on the Mac source run, where it moves that profile's own
`<state root>/eidolon-kernel.sqlite3`.

## App-ready meaning

```bash
uv run eidolon-ops --config /absolute/path/hosts/pi5.toml app-ready
```

Exit 0 requires Bootstrap preflight, Bootstrap/Local API/BlueZ/NetworkManager/Avahi active, exact identity/TLS file
ownership/mode, Bootstrap control socket and Local API HTTPS health + descriptor. With a unified Host `[app]`
contract it additionally requires a Hub identity derived from the Bootstrap Host key, matching certificate SAN/key,
LAN 8443 health, exact Hub descriptor, Local API target coherence, unique mDNS SRV/TXT resolution and the configured
LiveKit client origin.

`app-ready` does not replace the final device conversation E2E. The App must still obtain a real Channel Assignment,
connect to LiveKit, and reach Channel/Agent. Private `ws://` LiveKit is accepted only when the Host profile explicitly
opts into development LAN transport; product WSS remains a release gate. Never derive Hub trust from mDNS, substitute
the Host SPKI, or disable Mobile/ESP certificate verification. mDNS carries routing hints while Local API carries the
verified Hub certificate pin.

## What a release is built from

A release is sealed from `git rev-parse HEAD` in each of the eight source repositories the operations config names.
There is nothing to edit before a deploy: commit in the repositories, then deploy. The operator profile lists paths,
not commits.

This replaced a model where each `[sources.*]` table also declared a 40-hex `revision`, and it was replaced because
that model shipped a release nobody meant to ship — a cross-repository change was written and tested on every HEAD,
`deploy` reported success, and the Pi went on running the commits in the file. Three later deploys failed for the same
reason and said only `readiness timeout: hub, kernel, local-api`. Requiring the two copies to *agree* was considered
and rejected: it keeps the duplicate and turns it into a prompt to edit a file, which is a tax that carries no
information. So does rewriting the file automatically, which would leave a "declaration" that declares nothing.

Two things are still explicit, and only two:

- **Reproduction.** `--revision source=40hex` (repeatable), or a `revision` written in a `[sources.*]` table, ships
  that exact commit for that source. Nothing is written back to any file. The run's output labels itself a
  reproduction deployment and states how many commits behind HEAD each pinned source is — an operator who did not
  mean to reproduce anything has to be able to see that they are.
- **Uncommitted changes are refused.** A release is sealed with `git archive`, so anything uncommitted is not in it:
  the tree that was tested would not be the tree that ships. `--allow-dirty` overrides this; it still ships only the
  committed HEAD, and the dirty state is recorded in the Host's release evidence so it is visible afterwards.
  `doctor` reports a dirty worktree instead of refusing — diagnosis reports, shipping refuses.

`status` prints, per recent release, the commit / branch / HEAD / pinned / dirty facts of every source it was built
from, read from `/var/lib/eidolon/deployments/*/cutover.json` — which reclamation never touches, unlike the release
descriptor that used to be the only place these facts existed. A failed deploy additionally names how many commits
each source advanced since the last release this Host activated. That prevents nothing: a combination of commits that
is not self-consistent cannot be detected before it runs. It replaces "debug from zero" with "suspect these three".

`--resume` re-proves the sealed bundle against the commits the repositories resolve to *now*, so a resume after a
commit is refused. That is correct — a release id names one exact combination — and the refusal names both ways out:
`--revision` to continue the original combination, or a new `--release-id`.

## Daily update

正式 Pi profile 默认要求有线 release upload。若 USB Ethernet 未连接或 endpoint discovery 选中 Wi-Fi，
命令会在 seal 和上传前失败；不要关闭该门禁来绕过故障。连接线缆后重试即可。相同 lock 与 Channel 模型
已存在于 Pi CAS 时，日常更新只传 source/manifest 和真正变化的大对象。

1. Commit in the source repositories. Nothing in the operator profile needs editing.
2. Prepare and inspect without service switch:

   ```bash
   uv run eidolon-ops --config ... update --release-id 20260808-product-2
   ```

3. Activate the already prepared release:

   ```bash
   uv run eidolon-ops --config ... update --release-id 20260808-product-2 \
     --resume --activate
   ```

4. Require `status=activated`, `doctor status=healthy`, then `app-ready status=app_ready`.

Activation is: sealed preflight → snapshot → quiesce → assets/links → start → 13 readiness → receipt. Failure runs
exact snapshot restore. `--resume` never means “ignore a failed gate”; it skips only already-created bundle/upload/
prepare and re-proves the sealed release. A degraded post-activation doctor or App-ready result also restores the
exact activation snapshot; invalid or failed rollback evidence is surfaced as a hard failure and never reported as
recovered.

## Lifecycle, logs and diagnosis

- `start|stop|restart --dry-run` shows the fixed 15-unit scope; without dry-run it uses the current descriptor.
- `status` is read-only unit/link/receipt observation.
- `logs` is bounded to fixed unit names, line count and optional time filter.
- `diagnose` writes a new redacted archive and refuses overwrite. It omits env/key/DB/process-environment content;
  journal business data may still be sensitive.

## Rollback and data

```bash
uv run eidolon-ops --config ... rollback --release-id <id> \
  --snapshot /var/lib/eidolon/deployments/<id>-<tx>

uv run eidolon-ops --config ... rollback --release-id <id> \
  --snapshot /var/lib/eidolon/deployments/<id>-<tx> --apply
```

Rollback restores only 22 allowlist system assets and component links that existed in that exact snapshot. It never
restores secret, Host identity or database. Schema changes and data backup are independent authority-owned procedures;
descriptor requires `database_migrations=[]`. If automatic restore reports `rollback_failed`, stop automation and
collect status/logs/diagnose instead of retrying blindly.
