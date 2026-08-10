# Eidolon Pi operations runbook

All examples use the one Mac entrypoint:

```bash
uv run eidolon-pi --config /absolute/path/eidolon-pi.toml <operation>
```

## New Pi: one command from SSH-ready OS to App-ready backend

Preconditions: supported Raspberry Pi OS, known SSH host key, non-root account with non-interactive sudo, outbound
package/download access, and the 14 local private inputs declared in config.

```bash
uv run eidolon-pi --config /absolute/path/eidolon-pi.toml \
  install --release-id 20260807-product-1

uv run eidolon-pi --config /absolute/path/eidolon-pi.toml \
  install --release-id 20260807-product-1 --apply
```

The first command is a no-mutation plan. The second is the single authorized operation. Its state machine is:

```text
validate Mac commands, SSH files, 8 repos/commits, 14 private inputs
  -> probe Python without mutation
  -> foundation platform/capacity/package/artifact/service doctor
  -> if needed: Python bootstrap -> apt -> hash-pinned NATS/LiveKit/uv/Node -> BlueZ/NM/Avahi
  -> exact 8-commit bundle -> SSH upload -> Pi-native prepare/seal
  -> private staging -> exclusive first-install lock -> clean namespace proof
  -> identities/directories -> exact 14 input bytes -> fresh Data V2 baseline
  -> 22 assets + 7 links -> enable 4 top-level units -> ordered start
  -> 12 release readiness -> Host-side App commissioning gate -> completed journal
```

An existing unowned Eidolon database/link/secret/unit namespace fails closed. A partial install can resume only with
the same release ID and identical input digests. Temporary secret staging is removed after success or failure.

## Managed core Host: expand to the full App backend

An older Host whose Kernel/Data/Hub/Admin links all point into one managed release is not a new install. Inspect and
then expand it explicitly:

```bash
uv run eidolon-pi --config /absolute/path/eidolon-pi.toml \
  expand --release-id 20260809-product-full

uv run eidolon-pi --config /absolute/path/eidolon-pi.toml \
  expand --release-id 20260809-product-full --apply
```

The read-only plan rejects a missing/mixed core link, any partial Agent/Channel/Memory link set, or an existing
unowned new input. Apply provisions missing foundation dependencies, prepares the exact release, installs only the
seven new Agent/Channel/Memory/LiveKit env/settings inputs, then activates through the normal sealed transaction.
Its snapshot contains the four old core links and the system assets that really existed. If readiness or the
App-ready gate fails, rollback restores those links/assets and removes links/assets introduced by the failed
expansion. New private inputs remain as an auditable, digest-bound retry prerequisite; rollback never deletes secrets
or authority data.

## App-ready meaning

```bash
uv run eidolon-pi --config /absolute/path/eidolon-pi.toml app-ready
```

Exit 0 requires Bootstrap preflight, Bootstrap/Local API/BlueZ/NetworkManager/Avahi active, exact identity/TLS file
ownership/mode, Bootstrap control socket, Local API HTTPS health + descriptor and Avahi service definition. It proves
the Pi side only. A real phone must still validate BLE discovery, Host proof, TLS SPKI, Controller claim, Wi-Fi
checkpoint and Workspace setup.

## Daily update

1. Change only reviewed 40-hex revisions (or pass `--revision source=40hex`).
2. Prepare and inspect without service switch:

   ```bash
   uv run eidolon-pi --config ... update --release-id 20260808-product-2
   ```

3. Activate the already prepared release:

   ```bash
   uv run eidolon-pi --config ... update --release-id 20260808-product-2 \
     --resume --activate
   ```

4. Require `status=activated`, `doctor status=healthy`, then `app-ready status=app_ready`.

Activation is: sealed preflight → snapshot → quiesce → assets/links → start → 12 readiness → receipt. Failure runs
exact snapshot restore. `--resume` never means “ignore a failed gate”; it skips only already-created bundle/upload/
prepare and re-proves the sealed release. A degraded post-activation doctor or App-ready result also restores the
exact activation snapshot; invalid or failed rollback evidence is surfaced as a hard failure and never reported as
recovered.

## Lifecycle, logs and diagnosis

- `start|stop|restart --dry-run` shows the fixed 14-unit scope; without dry-run it uses the current descriptor.
- `status` is read-only unit/link/receipt observation.
- `logs` is bounded to fixed unit names, line count and optional time filter.
- `diagnose` writes a new redacted archive and refuses overwrite. It omits env/key/DB/process-environment content;
  journal business data may still be sensitive.

## Rollback and data

```bash
uv run eidolon-pi --config ... rollback --release-id <id> \
  --snapshot /var/lib/eidolon/deployments/<id>-<tx>

uv run eidolon-pi --config ... rollback --release-id <id> \
  --snapshot /var/lib/eidolon/deployments/<id>-<tx> --apply
```

Rollback restores only 22 allowlist system assets and component links that existed in that exact snapshot; a normal
update has seven, while a core-to-full expansion has four and removes the three newly introduced links. It never
restores secret, Host identity or database. Schema changes and data backup are independent authority-owned procedures;
descriptor requires `database_migrations=[]`. If automatic restore reports `rollback_failed`, stop automation and
collect status/logs/diagnose instead of retrying blindly.
