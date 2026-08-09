#!/usr/bin/env bash
# Render a runtime LiveKit config with the current LAN IP before exec'ing
# livekit-server. Keep deploy/livekit/livekit.yaml portable: rtc.node_ip is a
# machine/network fact, not source configuration.
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

detect_lan_ip() {
  if [[ -n "${EIDOLON_LIVEKIT_NODE_IP:-}" ]]; then
    printf '%s\n' "${EIDOLON_LIVEKIT_NODE_IP}"
    return 0
  fi

  python3 - <<'PY'
import re
import socket
import subprocess


def usable(ip: str) -> bool:
    return bool(ip) and not ip.startswith(("127.", "169.254."))


def run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""


def ips_for_device(device: str) -> list[str]:
    out = run(["ifconfig", device])
    if "status: active" not in out:
        return []
    return [ip for ip in re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\b", out) if usable(ip)]


def mac_primary_wifi_ip() -> str | None:
    out = run(["networksetup", "-listallhardwareports"])
    blocks = re.split(r"\n\s*\n", out)
    preferred: list[str] = []
    fallback: list[str] = []
    for block in blocks:
        device_match = re.search(r"^Device:\s*(\S+)", block, re.MULTILINE)
        if not device_match:
            continue
        device = device_match.group(1)
        port_match = re.search(r"^Hardware Port:\s*(.+)", block, re.MULTILINE)
        port = port_match.group(1).lower() if port_match else ""
        if "wi-fi" in port or "airport" in port:
            preferred.append(device)
        else:
            fallback.append(device)
    for device in [*preferred, *fallback]:
        ips = ips_for_device(device)
        if ips:
            return ips[0]
    return None


def active_ifconfig_ip() -> str | None:
    out = run(["ifconfig"])
    candidates: list[tuple[int, str, str]] = []
    for block in re.split(r"\n(?=\S)", out):
        name_match = re.match(r"^([^:\s]+):", block)
        if not name_match:
            continue
        name = name_match.group(1)
        if name.startswith(("lo", "utun", "awdl", "llw", "gif", "stf")):
            continue
        if "status: active" not in block:
            continue
        priority = 0 if name == "en0" else 1 if name.startswith("en") else 2
        for ip in re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\b", block):
            if usable(ip):
                candidates.append((priority, name, ip))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


ip = mac_primary_wifi_ip()
if usable(ip or ""):
    print(ip)
    raise SystemExit(0)

ip = active_ifconfig_ip()
if usable(ip or ""):
    print(ip)
    raise SystemExit(0)

for target in ("8.8.8.8", "1.1.1.1"):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 80))
        ip = sock.getsockname()[0]
        if usable(ip):
            print(ip)
            raise SystemExit(0)
    except OSError:
        pass
    finally:
        sock.close()
raise SystemExit(1)
PY
}

NODE_IP="$(detect_lan_ip)"
if [[ -z "$NODE_IP" ]]; then
  echo "livekit-dev: failed to detect LAN IP; set EIDOLON_LIVEKIT_NODE_IP" >&2
  exit 1
fi

mkdir -p "$(dirname "$GENERATED")"
python3 - "$TEMPLATE" "$GENERATED" "$NODE_IP" <<'PY'
import os
from pathlib import Path
import sys
import uuid

template = Path(sys.argv[1])
generated = Path(sys.argv[2])
node_ip = sys.argv[3]
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
        out.append(f"  node_ip: {node_ip}\n")
        inserted = True

if not inserted:
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

echo "livekit-dev: generated ${GENERATED} with rtc.node_ip=${NODE_IP}" >&2

if [[ "${EIDOLON_LIVEKIT_GENERATE_ONLY:-}" == "1" ]]; then
  printf '%s\n' "$GENERATED"
  exit 0
fi

exec "$LIVEKIT_BIN" --config "$GENERATED" "$@"
