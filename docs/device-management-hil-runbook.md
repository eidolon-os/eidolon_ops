# Device-management release and HIL gate

This gate closes one product slice only: commission and approve a Box3, observe
its active Claim and Mount, remove it online and offline, and prove platform
revocation and the independently acknowledged device erase. It does not add a
deployment platform and it does not cover PH3 or PH6 product behavior.

## 1. Capture one source identity

Run from a workspace whose nine participating repositories are clean and on
`main`. Repository discovery and exclusions come from the canonical SDK P0
Foundation inventory. Capture fails if any Git project is added or omitted;
formal non-participants remain explicit with `participation=false` and a
reason. It records branch, exact HEAD, dirty state, the Foundation
`sha256(git-ls-tree-r-z-full-tree-output)` digest and required test slots.

```sh
uv run eidolon-device-management-gate template \
  --workspace-root /Users/manson/ai/eidolon \
  --release-id DEVICE_MANAGEMENT_RELEASE_ID \
  --output /ABSOLUTE/EVIDENCE/release-inputs.json
```

Run every named test at the captured commit. Change each receipt from `pending`
to `passed` only after saving its output at an absolute, non-symlink
`evidence_path` and putting the real SHA-256 of that file in
`evidence_digest`. The verifier opens and hashes the file. Build in this order,
passing the manifest's
`release_identity` into each build's existing version/metadata input:

1. seal the Pi5 native release from the captured SDK, Hub, Kernel, Admin,
   Channel and Ops commits;
2. build the Mobile APK from the captured SDK and Mobile commits;
3. build the Box3 firmware from the captured SDK and ESP32 commits.

For every build, emit a provenance JSON object containing at least `contract`,
`artifact_sha256`, `release_identity`, and `sources`. Record absolute,
non-symlink artifact and provenance paths, both SHA-256 values, the same
`release_identity`, and the exact source mapping in each artifact entry. The
verifier opens the provenance object and compares all four required fields instead of
trusting the release manifest's self-report. Then:

```sh
uv run eidolon-device-management-gate verify \
  --workspace-root /Users/manson/ai/eidolon \
  /ABSOLUTE/EVIDENCE/release-inputs.json
```

Do not deploy unless the result is `ready_for_device_management_release`.
Re-run verification immediately before Pi5 activation, APK installation and
firmware flash; a changed HEAD, dirty tree, source archive, test receipt,
artifact byte or source mapping fails closed.

## 2. Activate and create a paused HIL checklist

Use the existing `eidolon-ops deploy` transaction for Pi5. Install the APK and
flash firmware only after all three bytes have passed the same release gate.
Create the device-specific checklist:

```sh
uv run eidolon-device-management-gate hil-template \
  --device-id BOX3_DEVICE_ID \
  --output /ABSOLUTE/EVIDENCE/box3-hil.json \
  /ABSOLUTE/EVIDENCE/release-inputs.json
```

Execute the listed steps in order. For every step, save process/API/device log
evidence at an absolute, non-symlink `evidence_path`, record its real SHA-256,
and set only that step to `passed`. The verifier opens and hashes every file.
Stop on any unexpected state; never skip or reorder a step.

The two `confirm_*_remove` steps are hard pauses immediately before a removal
request may cause device-local erase. A human must inspect the device ID and
release identity and preserve the exact confirmation phrase already written in
the checklist. Without it the final gate refuses the run.

The ordered scenarios are:

1. commissioning → proposal → explicit approval → GrantAck → ClaimActive → Mount;
2. confirmed online remove → platform revoke → unmount → signed erase ACK;
3. rejoin the erased device and take it offline;
4. confirmed offline remove → platform revoke/unmount while erase is pending;
5. reconnect → signed erase ACK;
6. retry the old request and old generation, both of which must be rejected.

Finish with:

```sh
uv run eidolon-device-management-gate hil-verify \
  /ABSOLUTE/EVIDENCE/release-inputs.json \
  /ABSOLUTE/EVIDENCE/box3-hil.json
```

Only `device_management_hil_passed` closes the overall hardware gate.
