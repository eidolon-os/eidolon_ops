# Eidolon Pi operations runbook

## Release/update state machine

```text
validate config + exact commits
  -> git-archive bundle
  -> upload to unique /var/tmp path
  -> Pi-native prepare + seal
  -> activation dry-run
  -> review gate (default stop)
  -> snapshot -> quiesce -> install assets -> switch links -> start -> readiness
       -> activated + doctor
       -> failure -> restore snapshot -> rolled_back receipt
       -> restore failure -> rollback_failed; preserve evidence and stop automation
```

`--resume` skips bundle/upload/prepare and re-runs sealed preflight. `--activate` is always explicit. A release is
identified by a safe release ID plus five complete Git object IDs; working-tree changes cannot enter the archive.

## First-install state machine

```text
local + SSH host preflight
  -> bundle/upload/native prepare/seal
  -> stage seven private files (values never logged)
  -> target install lock + durable phase journal
  -> prove clean/owned target namespace
  -> create eidolon identities and directories
  -> install exact prerequisite bytes without overwrite
  -> create fresh Data V2 Alembic baseline as eidolon
  -> install descriptor-allowlisted system assets and current links
  -> daemon-reload; enable only Bootstrap/eidolond/Local API/Admin
  -> ordered start; eidolond starts Data/Data Workspace/Hub/Kernel
  -> release doctor -> completed receipt
```

Re-running the same completed release is a doctor-only idempotent success. A partial journal may resume only the
same release and same staged input digests. Existing databases, identities, secrets, links or units without a
matching journal fail closed; the installer never imports an old Data database. Failures preserve the phase
journal and service diagnostics but remove the temporary secret staging bytes.

## Daily commands

1. Update the five reviewed revisions in TOML (or use `--revision source=40hex`).
2. Run `eidolon-pi deploy --release-id <id>` and review the JSON dry-run.
3. Run the identical command with `--resume --activate`.
4. Require `status=activated` followed by `status=healthy`.
5. Use `status`, `logs` and `diagnose`; never inspect authority SQLite through this tool.

`start`, `stop` and `restart` use the current descriptor and Kernel host adapter. Stop order removes product
ingress before eidolond and child units; start order is Bootstrap, eidolond, Local API, Admin. Data/Hub/Kernel are
not independently enabled or given a competing desired-state source.

## Rollback semantics

Rollback restores only the fixed system assets and four component links recorded in a selected activation
snapshot. Secret content, Host identity and all databases are untouched. A descriptor with database migrations is
rejected. Bootstrap code/current DB schema equality is a pre-activation gate. `rollback_failed` means host state is
unknown and requires manual diagnosis; do not loop retries.
