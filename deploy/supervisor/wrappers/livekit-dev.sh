#!/usr/bin/env bash
# Render credentials and an optional operator NAT override. Network changes
# are reconciled by eidolond, the same lifecycle owner as on Linux.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TEMPLATE="${EIDOLON_LIVEKIT_TEMPLATE_CONFIG:-${ROOT}/deploy/livekit/livekit.yaml}"
GENERATED="${EIDOLON_LIVEKIT_GENERATED_CONFIG:-${EIDOLON_RUNTIME_ROOT}/livekit/livekit.generated.yaml}"
LIVEKIT_BIN="${EIDOLON_LIVEKIT_BIN:-livekit-server}"

if [[ -z "${LIVEKIT_API_KEY:-}" || -z "${LIVEKIT_API_SECRET:-}" ]]; then
  echo "livekit-dev: LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required" >&2
  exit 1
fi
if [[ ! "$LIVEKIT_API_KEY" =~ ^[A-Za-z0-9_-]{16,64}$ ]] \
  || [[ ! "$LIVEKIT_API_SECRET" =~ ^[A-Za-z0-9_-]{32,128}$ ]]; then
  echo "livekit-dev: generated credential shape is invalid" >&2
  exit 1
fi

# An explicit value is an operator's NAT/address policy. An absent value lets
# LiveKit gather native ICE candidates; never persist an observed LAN address.
NODE_IP="${EIDOLON_LIVEKIT_NODE_IP:-}"

mkdir -p "$(dirname "$GENERATED")"
python3 - "$TEMPLATE" "$GENERATED" "$NODE_IP" <<'PY'
import os
import ipaddress
from pathlib import Path
import sys
import uuid

template = Path(sys.argv[1])
generated = Path(sys.argv[2])
node_ip = sys.argv[3].strip()
if node_ip:
    node_ip = str(ipaddress.ip_address(node_ip))
api_key = os.environ["LIVEKIT_API_KEY"]
api_secret = os.environ["LIVEKIT_API_SECRET"]

lines = template.read_text(encoding="utf-8").splitlines(keepends=True)
if any(line.strip() == "keys:" for line in lines):
    raise SystemExit("livekit-dev: source template must not contain credentials")
out: list[str] = []
inserted = False

for line in lines:
    stripped = line.strip()
    if stripped.startswith("node_ip:"):
        continue
    out.append(line)
    if stripped == "rtc:":
        if node_ip:
            out.append(f"  node_ip: {node_ip}\n")
        inserted = True

if not inserted and node_ip:
    out.extend(["\n", "rtc:\n", f"  node_ip: {node_ip}\n"])

out.extend(["\n", "keys:\n", f"  {api_key}: {api_secret}\n"])
temporary = generated.with_name(f".{generated.name}.{uuid.uuid4().hex}.tmp")
try:
    temporary.write_text("".join(out), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(generated)
finally:
    temporary.unlink(missing_ok=True)
PY

echo "livekit-dev: generated ${GENERATED} with ICE address policy=${NODE_IP:-native}" >&2

if [[ "${EIDOLON_LIVEKIT_GENERATE_ONLY:-}" == "1" ]]; then
  printf '%s\n' "$GENERATED"
  exit 0
fi

exec "$LIVEKIT_BIN" --config "$GENERATED" "$@"
