#!/usr/bin/env bash
# Home Assistant Core as a development bench for the smart-home HA Provider.
#
# One venv (Python 3.13, uv), one configuration directory with the `demo`
# integration (fake lights, covers, climate, fans, media players, sensors),
# and a long-lived access token written to <config>/ha.token by
# bootstrap_token.py once the instance is up. Everything lives under the
# Mac product profile so it is beside the stack it serves and nowhere else.
#
#   deploy/dev/homeassistant/run_ha_core.sh install   # venv + pip install homeassistant
#   deploy/dev/homeassistant/run_ha_core.sh start     # foreground, Ctrl-C stops
#   deploy/dev/homeassistant/run_ha_core.sh token     # onboard + write ha.token (needs a running instance)
#
# Not a product component. The product path is the Ops-managed HA Core
# component (P5.4); this script exists so the Provider can be developed and
# tested against a real Home Assistant without a device.
set -euo pipefail

ROOT="${EIDOLON_HA_BENCH_ROOT:-$HOME/ai/eidolon/.eidolon/mac-product/homeassistant}"
VENV="$ROOT/.venv"
CONFIG="$ROOT/config"
PORT="${EIDOLON_HA_BENCH_PORT:-8123}"
HERE="$(cd "$(dirname "$0")" && pwd)"

install() {
  mkdir -p "$ROOT" "$CONFIG"
  [ -x "$VENV/bin/python" ] || uv venv --python 3.13 "$VENV"
  uv pip install --python "$VENV/bin/python" "homeassistant" "home-assistant-frontend" >/dev/null
  if [ ! -f "$CONFIG/configuration.yaml" ]; then
    cat > "$CONFIG/configuration.yaml" <<YAML
# Development bench for the Eidolon smart-home HA Provider: the demo
# integration provides fake devices of every type the Provider maps.
homeassistant:
  name: Eidolon HA Bench
  unit_system: metric
  time_zone: Asia/Shanghai
http:
  server_host: 127.0.0.1
  server_port: $PORT
frontend:
api:
config:
demo:
YAML
  fi
  "$VENV/bin/python" -c "import homeassistant.const as c; print('Home Assistant', c.__version__)"
}

start() {
  [ -x "$VENV/bin/hass" ] || install
  # Not --skip-pip: demo and conversation install their own requirements on first start.
  exec "$VENV/bin/hass" -c "$CONFIG"
}

token() {
  "$VENV/bin/python" "$HERE/bootstrap_token.py" --url "http://127.0.0.1:$PORT" --out "$CONFIG/ha.token"
}

case "${1:-}" in
  install) install ;;
  start) start ;;
  token) token ;;
  *) echo "usage: $0 {install|start|token}" >&2; exit 2 ;;
esac
