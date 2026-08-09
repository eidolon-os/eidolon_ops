#!/usr/bin/env bash
# Ops-owned dependency gate: wait for TCP, then exec the component command.
#
#   wait-tcp.sh --host 127.0.0.1 --port 7880 --timeout 120 -- <cmd...>
#
# Used by channel-worker so it does not spam connection errors while livekit-server
# is still starting after a supervisord restart.
set -euo pipefail

host="127.0.0.1"
port=""
timeout=120

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) host="$2"; shift 2 ;;
    --port) port="$2"; shift 2 ;;
    --timeout) timeout="$2"; shift 2 ;;
    --)
      shift
      break
      ;;
    *)
      echo "wait-tcp: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$port" ]]; then
  echo "wait-tcp: --port is required" >&2
  exit 2
fi
if [[ $# -lt 1 ]]; then
  echo "wait-tcp: expected '-- <cmd...>'" >&2
  exit 2
fi

# Use bash's /dev/tcp builtin instead of ``nc``: it's always available
# (no external dependency, no BSD-vs-GNU netcat semantic skew on the
# ``-z`` flag), exits in <50ms on connect refusal, and works inside the
# minimal supervisord exec environment where PATH may not include /usr/bin.
probe() {
  # 2-second connect timeout via bash; redirect both directions to /dev/null.
  (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null
}

# Exponential backoff: start at 0.1s, double up to 2s ceiling. Compared
# to the old fixed 0.5s poll, this means faster detection when the
# upstream comes up early (livekit-server takes 0.3-2s normally) without
# hammering the port when it's taking longer.
deadline=$((SECONDS + timeout))
delay_ms=100  # current backoff in milliseconds
echo "wait-tcp: waiting for ${host}:${port} (timeout ${timeout}s)..." >&2
while (( SECONDS < deadline )); do
  if probe; then
    echo "wait-tcp: ${host}:${port} is up" >&2
    exec "$@"
  fi
  # bash ``sleep`` accepts fractional seconds on macOS + Linux coreutils.
  sleep "$(awk "BEGIN { print ${delay_ms}/1000 }")"
  delay_ms=$(( delay_ms * 2 ))
  (( delay_ms > 2000 )) && delay_ms=2000
done

echo "wait-tcp: timed out waiting for ${host}:${port}" >&2
exit 1
