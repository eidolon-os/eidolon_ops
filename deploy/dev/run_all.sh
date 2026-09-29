#!/usr/bin/env bash
# Eidolon macOS host adapter — the supervisord half of `./eidolon mac ...`.
#
# Not an operator entry point. `eidolon_ops/src/eidolon_ops/adapters/supervisord.py`
# runs `run_all.sh product-source <operation>` and nothing else; every manual
# operation starts from `./eidolon` (README, Ops 总纲 §1.5):
#
#   ./eidolon mac start|stop|restart|status     the whole product, one boundary action
#   ./eidolon mac service restart SERVICE       one service, through eidolond
#   ./eidolon mac debug web-start|web-stop|...  the admin web (vite) program
#
# What this script does that supervisord cannot do for itself: first-run
# bootstrap (venv + uv install, pnpm install, log dirs), render and start the
# product-source supervisord, and stop it cleanly (Channel first, so the worker
# does not log LiveKit refusing it on the way down).
#
# There is deliberately no supervisorctl passthrough here. A single service is
# restarted by eidolond, which reconciles every service it manages every 5 s; a
# second writer beside it is how a restart came back "already started".
#
set -euo pipefail
OPS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$OPS_ROOT"

# Resolve both a normal checkout and a linked Git worktree back to the sibling
# repository root. Ops is the lifecycle authority; Admin is one component.
export EIDOLON_OPS_ROOT="$OPS_ROOT"
export EIDOLON_WORKSPACE_ROOT="${EIDOLON_WORKSPACE_ROOT:-${EIDOLON_ROOT:-$(cd "${OPS_ROOT}/.." && pwd)}}"
export EIDOLON_ROOT="$EIDOLON_WORKSPACE_ROOT"
export EIDOLON_ADMIN_ROOT="${EIDOLON_ADMIN_ROOT:-${EIDOLON_WORKSPACE_ROOT}/eidolon_admin}"
export EIDOLON_CONFIG_ROOT="${EIDOLON_CONFIG_ROOT:-${OPS_ROOT}/config}"
export EIDOLON_STATE_ROOT="${EIDOLON_STATE_ROOT:-${HOME}/eidolon/data}"
export EIDOLON_RUNTIME_ROOT="${EIDOLON_RUNTIME_ROOT:-${HOME}/eidolon/run}"
export EIDOLON_LOG_ROOT="${EIDOLON_LOG_ROOT:-${HOME}/eidolon/logs}"
export EIDOLON_CACHE_ROOT="${EIDOLON_CACHE_ROOT:-${HOME}/eidolon/cache}"
export EIDOLON_BOOTSTRAP_STATE_ROOT="${EIDOLON_BOOTSTRAP_STATE_ROOT:-${HOME}/eidolon/bootstrap}"
export EIDOLON_BOOTSTRAP_RUNTIME_ROOT="${EIDOLON_BOOTSTRAP_RUNTIME_ROOT:-${EIDOLON_RUNTIME_ROOT}/bootstrap}"
export EIDOLON_BOOTSTRAP_STATE_DIR="$EIDOLON_BOOTSTRAP_STATE_ROOT"
export EIDOLON_BOOTSTRAP_RUNTIME_DIR="$EIDOLON_BOOTSTRAP_RUNTIME_ROOT"
export EIDOLON_PORTS_FILE="${EIDOLON_PORTS_FILE:-${EIDOLON_CONFIG_ROOT}/ports.yaml}"
export EIDOLON_ADMIN_SERVICES_FILE="${EIDOLON_ADMIN_SERVICES_FILE:-${EIDOLON_ADMIN_ROOT}/config/services.yaml}"
export EIDOLON_ADMIN_STATE_DIR="${EIDOLON_ADMIN_STATE_DIR:-${EIDOLON_STATE_ROOT}/admin}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
header(){ echo -e "${CYAN}==== $* ====${NC}"; }

# --- Paths ------------------------------------------------------------------
VAR_DIR="${EIDOLON_RUNTIME_ROOT}/ops"
LOG_DIR="$EIDOLON_LOG_ROOT"
export EIDOLON_LOG_ROOT="$LOG_DIR"
RUN_DIR="$EIDOLON_RUNTIME_ROOT"

# Log layout: $EIDOLON_LOG_ROOT/<project>/<file>.log
#   admin/      supervisord + gateway api (api) + vite (web)
#   nats/       server
#   livekit/    server
#   memory/     supervisor + discovery
#   hub/        api
#   agent/      main
#   channel/    worker
#   client-web/ dev
# supervisord refuses to spawn a program if its log dir doesn't exist; pre-
# create everything our own configs reference so the user never sees a phantom
# "no such file" on first start.
LOG_PROJECTS=(admin audit nats livekit memory data hub kernel agent channel laya client-web mementos admin/esp32-tools/jobs)
for _p in "${LOG_PROJECTS[@]}"; do
  mkdir -p "${LOG_DIR}/${_p}"
done
mkdir -p \
  "$VAR_DIR" \
  "$RUN_DIR" \
  "${LOG_DIR}/admin/childlogs" \
  "${LOG_DIR}/ops/childlogs" \
  "${EIDOLON_STATE_ROOT}/audit" \
  "${EIDOLON_STATE_ROOT}/admin" \
  "${EIDOLON_STATE_ROOT}/agent" \
  "${EIDOLON_STATE_ROOT}/memory/mempalaces-v3.8" \
  "${EIDOLON_STATE_ROOT}/nats/jetstream" \
  "${EIDOLON_STATE_ROOT}/voiceprints" \
  "${EIDOLON_RUNTIME_ROOT}/agent" \
  "${EIDOLON_RUNTIME_ROOT}/channel" \
  "${EIDOLON_RUNTIME_ROOT}/memory" \
  "${EIDOLON_CACHE_ROOT}/debug/agent" \
  "${EIDOLON_CACHE_ROOT}/debug/channel" \
  "${EIDOLON_CACHE_ROOT}" \
  "${EIDOLON_BOOTSTRAP_STATE_ROOT}" \
  "${EIDOLON_BOOTSTRAP_RUNTIME_ROOT}"

# Vite dev server pid/log — admin-api's pid is owned by supervisord now.
WEB_PID_FILE="${RUN_DIR}/eidolon-admin-gateway-web.pid"
LEGACY_WEB_PID_FILE="${RUN_DIR}/eidolon-admin-web.pid"
WEB_LOG_FILE="${LOG_DIR}/admin/gateway-web.log"
API_FOREGROUND_LOG_FILE="${LOG_DIR}/admin/gateway-api.foreground.log"
WEB_FOREGROUND_LOG_FILE="${LOG_DIR}/admin/gateway-web.foreground.log"

API_HOST="${EIDOLON_ADMIN_API_HOST:-127.0.0.1}"
API_PORT="${EIDOLON_ADMIN_API_PORT:-9000}"
WEB_PORT="${EIDOLON_ADMIN_WEB_PORT:-9001}"

VENV="${OPS_ROOT}/.venv"
export EIDOLON_OPS_VENV="$VENV"
export EIDOLON_NATS_SERVER="${EIDOLON_NATS_SERVER:-$(command -v nats-server || true)}"
export EIDOLON_LIVEKIT_BIN="${EIDOLON_LIVEKIT_BIN:-$(command -v livekit-server || true)}"
WEB_DIR="${EIDOLON_ADMIN_ROOT}/web"
VITE_BIN_REL="node_modules/.bin/vite"

# The one supervisord configuration; configure_supervisor_profile fills in
# the socket, pid and include for the product-source profile.
SV_PROFILE_CONF="${OPS_ROOT}/deploy/dev/supervisord.profile.conf"
SV_CONF="$SV_PROFILE_CONF"
SV_PID="${VAR_DIR}/supervisord.pid"
SV_SOCK="${VAR_DIR}/supervisor.sock"
SV_PROFILE=""

# shellcheck source=../supervisor/wrappers/livekit-credentials.sh
source "${OPS_ROOT}/deploy/supervisor/wrappers/livekit-credentials.sh"

configure_supervisor_profile() {
  local profile=$1
  case "$profile" in
    product-source)
      local profile_env="${EIDOLON_CONFIG_ROOT}/product-source.env"
      if [[ ! -f "$profile_env" || -L "$profile_env" ]]; then
        error "product-source profile is not prepared: $profile_env"
        # The likely cause is running this script directly. Its own default for
        # EIDOLON_CONFIG_ROOT is this repository's config directory, while the
        # profile's generated config lives under the Host's config root -- which
        # only eidolon-ops knows, and exports when it invokes this script.
        if [[ "$EIDOLON_CONFIG_ROOT" == "${OPS_ROOT}/config" ]]; then
          error "  EIDOLON_CONFIG_ROOT is this script's default, not a Host's config root."
          error "  Go through the single entry point, which exports the whole path set:"
          error "    uv run eidolon-ops --config config/hosts/<host>.toml debug status"
          error "  A supervisorctl passthrough is 'debug' too; see --help."
        fi
        exit 1
      fi
      local name value
      while IFS='=' read -r name value || [[ -n "$name" ]]; do
        [[ -z "$name" ]] && continue
        if [[ ! "$name" =~ ^EIDOLON_[A-Z0-9_]+$ || -z "$value" ]]; then
          error "unsafe product-source profile entry: $name"
          exit 1
        fi
        export "$name=$value"
      done < "$profile_env"
      # Product children load the one reviewed per-service env set. Do not let
      # credentials inherited from an unrelated legacy shell/supervisor win.
      unset LIVEKIT_API_KEY LIVEKIT_API_SECRET
      EIDOLON_ADMIN_ROOT="$EIDOLON_SOURCE_ADMIN"
      WEB_DIR="${EIDOLON_ADMIN_ROOT}/web"
      API_HOST="${EIDOLON_ADMIN_API_HOST:-127.0.0.1}"
      API_PORT="${EIDOLON_ADMIN_API_PORT:-9000}"
      WEB_PORT="${EIDOLON_ADMIN_WEB_PORT:-9001}"
      SV_PROFILE="$profile"
      SV_CONF="$SV_PROFILE_CONF"
      SV_PID="${VAR_DIR}/supervisord-${profile}.pid"
      SV_SOCK="${VAR_DIR}/supervisor-${profile}.sock"
      export EIDOLON_SUPERVISOR_PROFILE="$profile"
      export EIDOLON_SUPERVISOR_PID="$SV_PID"
      export EIDOLON_SUPERVISOR_SOCKET="$SV_SOCK"
      export EIDOLON_SUPERVISOR_INCLUDE_GLOB="${OPS_ROOT}/deploy/supervisor/product-source.conf"
      export EIDOLON_ADMIN_SUPERVISOR_SOCKET="$SV_SOCK"
      export EIDOLON_SUPERVISOR_LOG_FILE="${LOG_DIR}/admin/supervisord-${profile}.log"
      export EIDOLON_SUPERVISOR_CHILDLOG_DIR="${LOG_DIR}/admin/childlogs"
      ;;
    *)
      error "unknown supervisor profile: $profile"
      exit 1
      ;;
  esac
}

# --- Deps -------------------------------------------------------------------

ensure_api_deps() {
  if [[ "$SV_PROFILE" == "product-source" ]]; then
    ensure_product_source_deps
    return 0
  fi
  if [[ -z "$EIDOLON_NATS_SERVER" ]]; then
    error "nats-server is not on PATH"
    exit 1
  fi
  if [[ ! -x "${VENV}/bin/uvicorn" || ! -x "${VENV}/bin/supervisord" ]]; then
    info "first run — creating the Ops control venv"
    python3 -m venv "$VENV"
    "${VENV}/bin/pip" install -q --upgrade pip
    "${VENV}/bin/pip" install -q -e "${OPS_ROOT}[dev]" -e "${EIDOLON_ADMIN_ROOT}[dev]"
  fi
}

ensure_product_source_deps() {
  if [[ ! -x "${VENV}/bin/supervisord" || ! -x "${VENV}/bin/supervisorctl" ]]; then
    info "syncing the locked Ops product-source control runtime"
    uv sync --frozen --extra dev
  fi
}

ensure_web_deps() {
  if [[ ! -x "${WEB_DIR}/${VITE_BIN_REL}" ]]; then
    if command -v pnpm >/dev/null 2>&1; then
      info "first run — pnpm install"
      (cd "$WEB_DIR" && pnpm install)
    elif command -v npm >/dev/null 2>&1; then
      info "first run — npm install"
      (cd "$WEB_DIR" && npm install)
    else
      error "neither pnpm nor npm on PATH"
      exit 1
    fi
  fi
}

# --- process tree helpers --------------------------------------------------
#
# Why we need these: supervisord starts each child program with its own
# session (setsid), which means every child becomes a process-group leader
# independent of supervisord's group. So ``kill -<supervisord_pgid>`` only
# reaches supervisord itself — its children survive as orphans (PPID=1).
#
# The fix is to walk the PPID tree explicitly: enumerate descendants via
# pgrep -P, then signal each. This is the only way to guarantee that when
# we SIGKILL supervisord (because graceful shutdown timed out), we don't
# leave subprocesses hanging on to ports.

# Print all descendants of $1 (recursive) as ``pid|comm`` lines, where
# ``comm`` is the command name at snapshot time. Empty if no children.
#
# We snapshot the command name now so ``kill_tree`` can re-check it
# before signaling: a PID we collected at t=0 may have died and been
# reused by the kernel for a wholly unrelated process by t=signal.
# Without this guard we'd SIGKILL random innocents whenever shutdown
# stretched long enough for PID reuse (more common on busy macOS).
collect_descendants() {
  local parent=$1
  local children child comm
  children=$(pgrep -P "$parent" 2>/dev/null || true)
  for child in $children; do
    comm=$(ps -o comm= -p "$child" 2>/dev/null | tr -d '[:space:]')
    [[ -z "$comm" ]] && continue  # PID died between pgrep and ps; skip
    echo "${child}|${comm}"
    collect_descendants "$child"
  done
}

# Signal the entire tree rooted at $1 with signal $2 (e.g. "-TERM" or "-KILL").
#
# Order: parent first (stops supervisord's autorestart from racing us by
# respawning a child mid-shutdown), then descendants. Each kill is
# best-effort — already-dead PIDs return non-zero, which is fine.
#
# Each descendant is verified against its snapshotted comm: if the PID
# has been reused (different comm) we skip it. This is best-effort
# protection — comm match doesn't prove identity (two processes with
# the same name still alias) but it eliminates the common case where
# the PID has been recycled into something unrelated (a shell, sshd).
kill_tree() {
  local root=$1 signal=$2 entry pid snap_comm cur_comm
  kill "$signal" "$root" 2>/dev/null || true
  while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    pid="${entry%%|*}"
    snap_comm="${entry#*|}"
    cur_comm=$(ps -o comm= -p "$pid" 2>/dev/null | tr -d '[:space:]')
    if [[ -z "$cur_comm" ]]; then
      continue  # already gone, fine
    fi
    if [[ "$cur_comm" != "$snap_comm" ]]; then
      warn "kill_tree: skip PID $pid — comm changed ($snap_comm → $cur_comm), likely PID reuse"
      continue
    fi
    kill "$signal" "$pid" 2>/dev/null || true
  done < <(collect_descendants "$root")
}

# --- vite dev server --------------------------------------------------------

# --- supervisord ------------------------------------------------------------

# Print the PID when var/supervisord.pid points at a live supervisord process.
#
# The PID-existence check alone isn't enough: PIDs get recycled by the
# kernel. If our last supervisord crashed without removing its pid file,
# and the kernel later reassigned that PID to (say) the shell ``sleep``
# in run_all.sh, ``kill -0`` succeeds and we'd incorrectly skip startup.
# Verifying the process is really a supervisord invocation closes that
# hole.
#
# We grep ``ps -o args=`` rather than ``-o comm=`` because on macOS
# ``comm`` returns the executable basename (``python3``) and supervisord
# is launched as a script. The full args contain
# ``.../bin/supervisord -c ...`` which we can match unambiguously.
sv_pid_from_file() {
  [[ -f "$SV_PID" ]] || return 1
  local pid; pid=$(cat "$SV_PID" 2>/dev/null)
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -o args= -p "$pid" 2>/dev/null | grep -q "bin/supervisord" || return 1
  echo "$pid"
}

sv_ctl_ready() {
  [[ -S "$SV_SOCK" ]] && "${VENV}/bin/supervisorctl" -c "$SV_CONF" version >/dev/null 2>&1
}

# Print the PID reported by the live supervisord XML-RPC socket.
sv_pid_from_ctl() {
  [[ -S "$SV_SOCK" ]] || return 1
  local pid
  pid="$("${VENV}/bin/supervisorctl" -c "$SV_CONF" pid 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  echo "$pid"
}

# Repair var/supervisord.pid from the socket when the daemon is alive but the
# pidfile is missing or stale. This is the important split-brain guard: a live
# supervisord keeps auto-restarting children, so treating "no pidfile" as "not
# running" makes restart collide with its own managed processes.
sv_repair_pidfile_from_ctl() {
  local pid file_pid
  pid="$(sv_pid_from_ctl)" || return 1
  file_pid="$(cat "$SV_PID" 2>/dev/null || true)"
  if [[ "$file_pid" != "$pid" ]]; then
    warn "supervisord socket is live (PID $pid) but pidfile is stale/missing; repairing $SV_PID" >&2
    echo "$pid" >"$SV_PID"
  fi
  echo "$pid"
}

sv_pid() {
  sv_pid_from_file || sv_repair_pidfile_from_ctl
}

sv_alive() {
  sv_pid >/dev/null
}

# Stop channel before LiveKit during stack shutdown so the worker does not log
# ConnectionRefused while livekit-server is tearing down (supervisorctl
# shutdown stops programs in parallel by default).
do_sv_stop_channel_first() {
  if ! sv_ctl_ready; then
    return 0
  fi
  local raw state
  raw="$("${VENV}/bin/supervisorctl" -c "$SV_CONF" status channel:channel-worker 2>/dev/null || true)"
  state="$(echo "$raw" | awk '{print $2}')"
  if [[ -z "$state" || "$state" == "STOPPED" || "$state" == "ERROR" ]]; then
    return 0
  fi
  info "stopping channel-worker before stack shutdown (clean LiveKit disconnect)"
  "${VENV}/bin/supervisorctl" -c "$SV_CONF" stop channel:channel-worker 2>/dev/null \
    || warn "supervisorctl stop channel:channel-worker failed (continuing)"
  local i
  for i in $(seq 1 80); do
    raw="$("${VENV}/bin/supervisorctl" -c "$SV_CONF" status channel:channel-worker 2>/dev/null || true)"
    state="$(echo "$raw" | awk '{print $2}')"
    if [[ -z "$state" || "$state" == "STOPPED" ]]; then
      return 0
    fi
    sleep 0.5
  done
  warn "channel-worker still ${state:-running} after 40s; proceeding with shutdown"
}

# Reload the product-source configuration into a running supervisord.
do_sv_reread_update() {
  ensure_api_deps
  if ! sv_alive; then
    return 0
  fi
  local i
  for i in $(seq 1 40); do
    sv_ctl_ready && break
    sleep 0.25
  done
  if ! sv_ctl_ready; then
    warn "supervisorctl not ready on $SV_SOCK — skip reread/update"
    return 1
  fi
  info "supervisord reread + update (reload enabled/*.conf)"
  if ! "${VENV}/bin/supervisorctl" -c "$SV_CONF" reread; then
    error "supervisorctl reread failed; restart is required before this configuration is active"
    return 1
  fi
  if ! "${VENV}/bin/supervisorctl" -c "$SV_CONF" update; then
    error "supervisorctl update failed; restart is required before this configuration is active"
    return 1
  fi
}

do_sv_start() {
  ensure_api_deps
  if [[ "$SV_PROFILE" != "product-source" ]]; then
    eidolon_ensure_livekit_credentials
  fi
  if sv_alive; then
    info "supervisord already running (PID $(sv_pid), socket $SV_SOCK)"
    info "  config reload only — use '$0 status' to inspect; '$0 restart' for full stop+start"
    if ! do_sv_reread_update; then
      return 1
    fi
    return 0
  fi
  # If a stale socket lingers from a crashed daemon, supervisord will refuse
  # to start.
  if [[ -S "$SV_SOCK" ]] && ! sv_ctl_ready; then
    info "cleaning stale socket $SV_SOCK"
    rm -f "$SV_SOCK"
  fi
  info "starting supervisord (conf $SV_CONF)"
  "${VENV}/bin/supervisord" -c "$SV_CONF"
  # supervisord daemonises immediately and writes the pid file.
  sleep 0.5
  if ! sv_alive; then
    error "supervisord did not start; tail of supervisord.log:"
    tail -30 "$LOG_DIR/admin/supervisord.log" >&2 || true
    return 1
  fi
  info "supervisord PID $(sv_pid), socket $SV_SOCK"
  info "  (admin-api auto-starts under supervisord)"
  do_sv_reread_update
}

do_sv_stop() {
  if ! sv_alive; then
    info "supervisord not running"
    rm -f "$SV_PID" 2>/dev/null || true
    return 0
  fi
  local sv_pid; sv_pid="$(sv_pid)"

  # Step 1: stop channel while LiveKit is still up (avoids reconnect errors in logs).
  do_sv_stop_channel_first

  # Step 2: ask supervisord to gracefully shut down the rest of the program tree.
  #
  # It will send SIGTERM to each program and wait up to that program's
  # ``stopwaitsecs`` before SIGKILL-ing. Remaining programs shut down in parallel;
  # wall time is bounded by the max ``stopwaitsecs`` plus supervisord overhead.
  info "supervisord shutdown (stops admin-api and all enabled supervised programs)"
  "${VENV}/bin/supervisorctl" -c "$SV_CONF" shutdown 2>/dev/null \
    || warn "supervisorctl shutdown rpc failed (continuing with patient wait)"

  # Step 3: patient wait. 60s = max(stopwaitsecs)=40 + 20s buffer for
  # supervisord's own teardown. Previously this was 20s, which routinely
  # truncated channel-worker's graceful exit and forced step 3.
  local wait_seconds=60
  local checks=$((wait_seconds * 2))
  for _ in $(seq 1 $checks); do sv_alive || break; sleep 0.5; done

  if ! sv_alive; then
    rm -f "$SV_PID" 2>/dev/null || true
    return 0
  fi

  # Step 4: escalation. Graceful shutdown didn't complete in time. This
  # is where the old code SIGKILL'd just supervisord — and every child
  # immediately became a PPID=1 orphan because supervisord uses setsid
  # to isolate each child into its own session, so signaling supervisord
  # alone doesn't reach its descendants.
  #
  # ``kill_tree`` walks the PPID hierarchy explicitly and signals every
  # descendant individually, eliminating the orphan window. We do this
  # in two stages: TERM first (some children may still react), then KILL
  # if anyone survives.
  warn "supervisord didn't exit within ${wait_seconds}s — escalating to TERM-tree"
  kill_tree "$sv_pid" "-TERM"
  for _ in $(seq 1 30); do sv_alive || break; sleep 0.5; done

  if sv_alive; then
    warn "still alive after TERM-tree (15s); SIGKILL whole tree"
    kill_tree "$sv_pid" "-KILL"
    sleep 1
  fi

  rm -f "$SV_PID" 2>/dev/null || true
}

do_sv_status() {
  header "supervisord"
  if sv_alive; then
    info "running PID $(sv_pid), socket $SV_SOCK"
    echo
    "${VENV}/bin/supervisorctl" -c "$SV_CONF" status || true
  else
    info "not running"
  fi
}

# --- combined ---------------------------------------------------------------

# Audit the ports this topology is about to bind, before supervisord binds
# them. The dev path has done this since two projects in this workspace first
# claimed one port; this path did not, and the symptom of a squatted port is a
# program in an endless BACKOFF loop with the bind error buried in its own log.
# It happened: eidolon_vision defaulted to 8085, which the registry assigns to
# Data's workspace API, and Data spent the whole run in BACKOFF. Vision moved to
# 18085 and is registered now; the audit is what makes the next one loud.
#
# Ops audits its own declared ports rather than delegating to Admin's dev
# service catalogue: the set being started here is the product-source topology,
# and a port belonging to some other profile is not this run's business.
do_product_source_port_audit() {
  local busy=0 name port holder
  for name in \
    EIDOLON_PRODUCT_DATA_PORT EIDOLON_PRODUCT_DATA_WORKSPACE_PORT \
    EIDOLON_PRODUCT_HUB_PORT EIDOLON_PRODUCT_KERNEL_PORT \
    EIDOLON_PRODUCT_EIDOLOND_PORT EIDOLON_PRODUCT_LOCAL_API_PORT \
    EIDOLON_PRODUCT_MEMORY_DISCOVERY_PORT EIDOLON_PRODUCT_MEMORY_ADMIN_PORT \
    EIDOLON_PRODUCT_AGENT_HTTP_PORT EIDOLON_PRODUCT_AGENT_ADMIN_PORT \
    EIDOLON_PRODUCT_CHANNEL_PROVIDER_PORT EIDOLON_PRODUCT_CHANNEL_WORKER_PORT \
    EIDOLON_PRODUCT_LIVEKIT_PORT EIDOLON_ADMIN_API_PORT \
    EIDOLON_PRODUCT_NATS_PORT EIDOLON_PRODUCT_NATS_HTTP_PORT
  do
    port="${!name:-}"
    [[ -z "$port" ]] && continue
    # A program this supervisord already owns is not a conflict; it is the
    # thing being restarted. Only a listener from outside this profile is.
    holder="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -F pc 2>/dev/null \
      | awk '/^p/{pid=substr($0,2)} /^c/{print pid" "substr($0,2)}' | head -1)"
    [[ -z "$holder" ]] && continue
    if supervised_descendant "${holder%% *}"; then
      continue
    fi
    error "port $port ($name) is held by: $holder"
    busy=1
  done
  if (( busy )); then
    error "refusing to start: a declared port is held by a process this profile does not own"
    error "  Stop it, or move it off the port registry (src/eidolon_ops/assets/ports.yaml)."
    return 1
  fi
  info "all declared ports are free or already ours"
  return 0
}

# Is this pid part of the supervisord tree we are about to reuse? SV_PID is
# the pidfile path, so the running supervisord's pid is read out of it.
supervised_descendant() {
  local pid="$1" guard=0 supervisor
  supervisor="$(cat "${SV_PID:-/nonexistent}" 2>/dev/null | tr -d ' ')"
  [[ -z "$supervisor" ]] && return 1
  while [[ -n "$pid" && "$pid" != "1" && $guard -lt 20 ]]; do
    [[ "$pid" == "$supervisor" ]] && return 0
    pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
    guard=$((guard + 1))
  done
  return 1
}

do_product_source_start() {
  configure_supervisor_profile product-source
  ensure_product_source_deps
  header "declared ports are free"
  do_product_source_port_audit || return 1
  # NATS is this profile's own program (nats:nats-server), started by eidolond
  # like every service it reconciles; a broker someone else started on 4222
  # is caught by the port audit above instead of being adopted.
  if [[ -z "$EIDOLON_NATS_SERVER" || ! -x "$EIDOLON_NATS_SERVER" ]]; then
    error "nats-server is not on PATH"
    return 1
  fi
  if [[ -z "$EIDOLON_LIVEKIT_BIN" || ! -x "$EIDOLON_LIVEKIT_BIN" ]]; then
    error "livekit-server is not on PATH"
    return 1
  fi
  # The template, not the rendered config: the wrapper writes the latter at
  # every start, so requiring it here would only prove that some earlier run
  # left a file behind -- which is exactly how a config for port 17880
  # survived months of starts.
  if [[ ! -f "$EIDOLON_LIVEKIT_TEMPLATE_CONFIG" || -L "$EIDOLON_LIVEKIT_TEMPLATE_CONFIG" ]]; then
    error "generated LiveKit template is missing or unsafe: $EIDOLON_LIVEKIT_TEMPLATE_CONFIG"
    return 1
  fi
  header "supervisord product-source (Mac source topology)"
  do_sv_start
  echo
  do_sv_status
}

do_product_source_stop() {
  configure_supervisor_profile product-source
  header "supervisord product-source"
  do_sv_stop
}

do_product_source_restart() {
  do_product_source_stop
  sleep 1
  do_product_source_start
}

do_product_source_status() {
  configure_supervisor_profile product-source
  do_sv_status
}

do_product_source_commissioning_code() {
  configure_supervisor_profile product-source
  ensure_product_source_deps
  local ttl=600
  local code=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ttl)  ttl="${2:-}"; shift 2 ;;
      # Passed straight to bootstrapctl: the Host owns the rule about what a
      # usable code is, and a second opinion here could only disagree with it.
      --code) code="${2:-}"; shift 2 ;;
      *)
        error "usage: $0 product-source commissioning-code [--ttl SECONDS] [--code DIGITS]"
        return 2
        ;;
    esac
  done
  if [[ ! "$ttl" =~ ^[0-9]+$ || "$ttl" -lt 60 || "$ttl" -gt 86400 ]]; then
    error "commissioning code TTL must be between 60 and 86400 seconds"
    return 2
  fi
  local -a code_argument=()
  if [[ -n "$code" ]]; then
    code_argument=(--code "$code")
  fi
  "${OPS_ROOT}/deploy/supervisor/wrappers/with-env.sh" \
    "$EIDOLON_SOURCE_ADMIN" \
    "${EIDOLON_PRODUCT_ENV_ROOT}/bootstrap.env" \
    -- "$EIDOLON_SOURCE_ADMIN/.venv/bin/eidolon-bootstrapctl" commissioning-code \
       --ttl "$ttl" "${code_argument[@]}"
}

do_product_source_web_start() {
  configure_supervisor_profile product-source
  ensure_product_source_deps
  ensure_web_deps
  do_sv_start
  local state
  state="$("${VENV}/bin/supervisorctl" -c "$SV_CONF" status admin-web 2>/dev/null || true)"
  if [[ "$state" == *" RUNNING "* ]]; then
    info "Admin Web already running / http://127.0.0.1:${WEB_PORT}/"
    return 0
  fi
  "${VENV}/bin/supervisorctl" -c "$SV_CONF" start admin-web
  info "Admin Web / http://127.0.0.1:${WEB_PORT}/"
}

do_product_source_web_stop() {
  configure_supervisor_profile product-source
  ensure_product_source_deps
  local state
  state="$("${VENV}/bin/supervisorctl" -c "$SV_CONF" status admin-web 2>/dev/null || true)"
  if [[ "$state" == *" STOPPED "* || -z "$state" ]]; then
    info "Admin Web not running"
    return 0
  fi
  "${VENV}/bin/supervisorctl" -c "$SV_CONF" stop admin-web
}

do_product_source_web_restart() {
  configure_supervisor_profile product-source
  ensure_product_source_deps
  ensure_web_deps
  do_sv_start
  "${VENV}/bin/supervisorctl" -c "$SV_CONF" restart admin-web
  info "Admin Web / http://127.0.0.1:${WEB_PORT}/"
}

do_product_source_web_status() {
  configure_supervisor_profile product-source
  ensure_product_source_deps
  "${VENV}/bin/supervisorctl" -c "$SV_CONF" status admin-web || true
}

# --- dispatch ---------------------------------------------------------------
#
# One profile, the operations Ops calls, and help. Anything else is refused:
# a command no caller uses is a second way in (Ops 总纲 §1.5).

case "${1:-}" in
  product-source)
    shift
    case "${1:-status}" in
      start)   do_product_source_start ;;
      stop)    do_product_source_stop ;;
      restart) do_product_source_restart ;;
      status)  do_product_source_status ;;
      commissioning-code)
        shift
        do_product_source_commissioning_code "$@"
        ;;
      web-start) do_product_source_web_start ;;
      web-stop) do_product_source_web_stop ;;
      web-restart) do_product_source_web_restart ;;
      web-status) do_product_source_web_status ;;
      *)
        error "unknown product-source command: ${1:-}"
        error "usage: $0 product-source start|stop|restart|status|commissioning-code|web-start|web-stop|web-restart|web-status"
        exit 1
        ;;
    esac
    ;;

  -h|--help|help)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    error "unknown command: ${1:-}"
    error "this script is the internal adapter behind ./eidolon mac; run ./eidolon mac ... instead"
    exit 1
    ;;
esac
