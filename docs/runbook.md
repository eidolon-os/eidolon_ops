# Eidolon Pi operations runbook

All examples use the unified host entrypoint. The Pi Host profile references
the lower-level commit/SSH release configuration:

```bash
uv run eidolon-ops --config /absolute/path/hosts/pi5.toml <operation>
```

## New Pi: one command from SSH-ready OS to App-ready backend

Preconditions: reviewed Debian/Raspberry Pi OS 13, known SSH host key, non-root account with non-interactive sudo, outbound
package/download access, and the 14 local private inputs declared in config.

Create those inputs once before the first plan. This is local-only and never contacts the Pi:

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
  -> exact 8-commit bundle -> digest-guarded resumable SSH/rsync staging
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
`--wipe-authority-data` additionally removes those authority roots and the old `/var/lib/eidolon-admin` root. It is
irreversible. Foundation packages/artifacts and service identities are preserved. Reset stops and disables only the
fixed 15 units, refuses an active unit that cannot stop, refuses mounted deletion roots, reloads systemd and is
idempotent when units or paths are already absent.

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
