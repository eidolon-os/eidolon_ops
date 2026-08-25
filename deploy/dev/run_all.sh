#!/usr/bin/env bash
# Eidolon macOS host adapter — supervisord edition.
#
# This Ops-owned wrapper does only what supervisord can't do for itself:
#   1. first-run bootstrap (venv + uv install, pnpm install, log dirs)
#   2. start the supervisord daemon (which then starts admin-api + every
#      enabled sub-project under deploy/supervisor/enabled/*.conf)
#   3. launch the vite dev server for the admin web (port 9001)
#   4. clean stop of everything in reverse order
#
# admin-api itself is now a supervised program (see
# deploy/supervisor/available/admin.conf), so it inherits auto-restart,
# unified logs, and remote-controlled restart from the Configs page.
#
# Per-program control (start/stop/restart memory-supervisor etc.) happens
# via the admin UI or `supervisorctl`. This script is just the top-level
# bootstrap + lifecycle.
#
# Usage:
#   ./deploy/dev/run_all.sh                # foreground admin-api + web only (no supervisord)
#   ./deploy/dev/run_all.sh start          # cold start: ports must be free, then supervisord + vite
#   ./deploy/dev/run_all.sh start --force-cleanup
#                                        # SIGTERM Eidolon-looking port holders, then cold start
#   ./deploy/dev/run_all.sh start --strict
#                                        # fail if optional enabled services are degraded
#   ./deploy/dev/run_all.sh start --no-wait-ready
#                                        # skip post-start readiness wait
#   ./deploy/dev/run_all.sh stop           # stop vite + supervisord (all supervised programs)
#   ./deploy/dev/run_all.sh restart        # stop then start (use when stack is already running)
#   ./deploy/dev/run_all.sh status         # show vite + supervisorctl status (no port check)
#   ./deploy/dev/run_all.sh status --readiness
#                                        # include one-shot service readiness diagnostics
#   ./deploy/dev/run_all.sh foreground     # admin-api + web in foreground (no sub-projects)
#                                        # supervised admin + agent + memory + nats only
#
#   ./deploy/dev/run_all.sh sv [...]       # passthrough to supervisorctl
#                                        # e.g. sv status, sv restart channel:channel-worker
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
export EIDOLON_ADMIN_SUPERVISOR_AVAILABLE_DIR="${EIDOLON_ADMIN_SUPERVISOR_AVAILABLE_DIR:-${OPS_ROOT}/deploy/supervisor/available}"
export EIDOLON_ADMIN_SUPERVISOR_ENABLED_DIR="${EIDOLON_ADMIN_SUPERVISOR_ENABLED_DIR:-${EIDOLON_RUNTIME_ROOT}/supervisor/enabled}"

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
LOG_PROJECTS=(admin audit nats livekit memory data hub kernel agent channel client-web mementos admin/esp32-tools/jobs)
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
  "${EIDOLON_STATE_ROOT}/memory/mempalaces" \
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

load_ports_env() {
  ensure_api_deps
  # shellcheck disable=SC2046
  eval "$("${VENV}/bin/python" -m eidolon_admin_server.app.ports export)"
  API_HOST="${EIDOLON_ADMIN_API_HOST:-127.0.0.1}"
  API_PORT="${EIDOLON_ADMIN_API_PORT:-9000}"
  WEB_PORT="${EIDOLON_ADMIN_WEB_PORT:-9001}"
}

collect_ports_registry() {
  ensure_api_deps
  info "collecting config/ports.yaml from sub-project settings (read-only)"
  "${VENV}/bin/python" -m eidolon_admin_server.app.ports collect || true
}

# First-start materialization of ``deploy/supervisor/enabled/*.conf``.
# The directory is gitignored — runtime state, not source — so a fresh
# clone has only ``.gitkeep`` and the Admin UI's Enable/Disable
# choices need somewhere to live without git stomping on them. We seed
# from ``config/default-enabled.txt`` exactly ONCE, marked by a
# sentinel file. After that, the directory belongs to the operator
# (UI + manual ln).
#
# Idempotency contract:
#   - sentinel exists                 → do nothing, respect whatever
#                                       symlinks the operator chose.
#   - sentinel missing                → create symlinks for every name
#                                       in default-enabled.txt that
#                                       doesn't already have one;
#                                       touch sentinel; never re-run.
# This means: "deleting a symlink to disable" survives ``git pull`` /
# ``run_all.sh restart`` / anything else, because the sentinel keeps
# us from re-seeding.
seed_enabled_symlinks() {
  local enabled_dir="${EIDOLON_ADMIN_SUPERVISOR_ENABLED_DIR}"
  local available_dir="${OPS_ROOT}/deploy/supervisor/available"
  local defaults_file="${EIDOLON_CONFIG_ROOT}/default-enabled.txt"
  local sentinel="${enabled_dir}/.seeded"

  mkdir -p "$enabled_dir"

  if [[ -f "$sentinel" ]]; then
    return 0
  fi
  if [[ ! -f "$defaults_file" ]]; then
    warn "seed: ${defaults_file} missing — skipping (Admin UI will need manual enable)"
    return 0
  fi

  # Migration safety: if the operator already has *any* symlink in
  # enabled/ but no sentinel yet, they're an existing checkout pulling
  # the "untrack enabled/" change. Their current state is the truth —
  # we must not re-seed and resurrect things they had disabled. Just
  # claim the sentinel and exit, preserving their choices.
  local existing
  existing="$(find "$enabled_dir" -maxdepth 1 -type l -name '*.conf' 2>/dev/null | head -n 1)"
  if [[ -n "$existing" ]]; then
    info "enabled/ already populated; treating as existing checkout (no re-seed)"
    touch "$sentinel"
    return 0
  fi

  info "first-start seed: materializing enabled/ from $(basename "$defaults_file")"
  local count=0 skipped=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    # strip inline comments + trim whitespace
    local name="${line%%#*}"
    name="${name#"${name%%[![:space:]]*}"}"
    name="${name%"${name##*[![:space:]]}"}"
    [[ -z "$name" ]] && continue

    local available="${available_dir}/${name}.conf"
    local link="${enabled_dir}/${name}.conf"
    if [[ ! -f "$available" ]]; then
      warn "  - ${name}: available/${name}.conf missing, skip"
      skipped=$((skipped + 1))
      continue
    fi
    if [[ -e "$link" || -L "$link" ]]; then
      # Operator already has a symlink (or a file). Don't overwrite.
      continue
    fi
    ln -s "$available" "$link"
    count=$((count + 1))
  done < "$defaults_file"

  touch "$sentinel"
  info "  seeded ${count} symlink(s) (${skipped} skipped); future enable/disable goes through Admin UI"
}

VENV="${OPS_ROOT}/.venv"
export EIDOLON_OPS_VENV="$VENV"
export EIDOLON_NATS_SERVER="${EIDOLON_NATS_SERVER:-$(command -v nats-server || true)}"
export EIDOLON_LIVEKIT_BIN="${EIDOLON_LIVEKIT_BIN:-$(command -v livekit-server || true)}"
WEB_DIR="${EIDOLON_ADMIN_ROOT}/web"
VITE_BIN_REL="node_modules/.bin/vite"

SV_DEFAULT_CONF="${OPS_ROOT}/deploy/dev/supervisord.conf"
SV_PROFILE_CONF="${OPS_ROOT}/deploy/dev/supervisord.profile.conf"
SV_CONF="$SV_DEFAULT_CONF"
SV_PID="${VAR_DIR}/supervisord.pid"
SV_SOCK="${VAR_DIR}/supervisor.sock"
SV_PROFILE=""
SUPERVISOR_PROFILE_ENABLED_DIR=""
PREFLIGHT_SERVICE_IDS=""

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
      SUPERVISOR_PROFILE_ENABLED_DIR="${OPS_ROOT}/deploy/supervisor"
      PREFLIGHT_SERVICE_IDS="admin,eidolond,data,data-workspace,hub,kernel,nats,livekit,memory,agent,channel"
      export EIDOLON_SUPERVISOR_PROFILE="$profile"
      export EIDOLON_SUPERVISOR_PID="$SV_PID"
      export EIDOLON_SUPERVISOR_SOCKET="$SV_SOCK"
      export EIDOLON_SUPERVISOR_ENABLED_DIR="$SUPERVISOR_PROFILE_ENABLED_DIR"
      export EIDOLON_SUPERVISOR_INCLUDE_GLOB="${OPS_ROOT}/deploy/supervisor/product-source.conf"
      export EIDOLON_ADMIN_SUPERVISOR_SOCKET="$SV_SOCK"
      export EIDOLON_ADMIN_SUPERVISOR_ENABLED_DIR="$SUPERVISOR_PROFILE_ENABLED_DIR"
      export EIDOLON_SUPERVISOR_LOG_FILE="${LOG_DIR}/admin/supervisord-${profile}.log"
      export EIDOLON_SUPERVISOR_CHILDLOG_DIR="${LOG_DIR}/admin/childlogs"
      ;;
    *)
      error "unknown supervisor profile: $profile"
      exit 1
      ;;
  esac
}

materialize_supervisor_profile() {
  [[ -n "$SV_PROFILE" ]] || return 0
  local configs=()
  case "$SV_PROFILE" in
    product-source)
      return 0
      ;;
    *)
      error "unknown supervisor profile: $SV_PROFILE"
      exit 1
      ;;
  esac

  mkdir -p "$SUPERVISOR_PROFILE_ENABLED_DIR"
  rm -f "${SUPERVISOR_PROFILE_ENABLED_DIR}"/*.conf
  local name available link
  for name in "${configs[@]}"; do
    available="${OPS_ROOT}/deploy/supervisor/available/${name}.conf"
    link="${SUPERVISOR_PROFILE_ENABLED_DIR}/${name}.conf"
    if [[ ! -f "$available" ]]; then
      error "profile ${SV_PROFILE}: missing supervisor config $available"
      exit 1
    fi
    ln -s "$available" "$link"
  done
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

migrate_system_data() {
  ensure_api_deps
  local data_root="${EIDOLON_ROOT}/eidolon_data"
  if [[ ! -f "${data_root}/alembic.ini" ]]; then
    error "eidolon_data migration project not found: ${data_root}"
    exit 1
  fi
  info "migrating eidolon-system.sqlite3 to the current Alembic head"
  (cd "$data_root" && "${VENV}/bin/alembic" -c alembic.ini upgrade head)
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

proc_cwd() {
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1 || true
}

web_pid_is_ours() {
  local pid=$1 args cwd
  kill -0 "$pid" 2>/dev/null || return 1
  args="$(ps -o args= -p "$pid" 2>/dev/null || true)"
  cwd="$(proc_cwd "$pid" || true)"
  [[ "$args" == *"vite"* || "$args" == *"vite.js"* ]] || return 1
  [[ "$cwd" == "$WEB_DIR" || "$args" == *"$WEB_DIR"* ]]
}

web_pid_from_file() {
  local file pid
  for file in "$WEB_PID_FILE" "$LEGACY_WEB_PID_FILE"; do
    [[ -f "$file" ]] || continue
    pid="$(cat "$file" 2>/dev/null || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    if web_pid_is_ours "$pid"; then
      if [[ "$file" == "$LEGACY_WEB_PID_FILE" ]]; then
        info "adopting legacy web PID file $LEGACY_WEB_PID_FILE (PID $pid)" >&2
        echo "$pid" >"$WEB_PID_FILE"
      fi
      echo "$pid"
      return 0
    fi
    warn "ignoring stale web pidfile $file (PID ${pid:-?} is not this Vite server)" >&2
  done
  return 1
}

web_listener_pids() {
  lsof -nP -tiTCP:"$WEB_PORT" -sTCP:LISTEN 2>/dev/null || true
}

web_pid_from_port() {
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    if web_pid_is_ours "$pid"; then
      warn "web PID file missing/stale; adopting Vite listener on :$WEB_PORT (PID $pid)" >&2
      echo "$pid" >"$WEB_PID_FILE"
      echo "$pid"
      return 0
    fi
  done < <(web_listener_pids)
  return 1
}

web_pid() {
  web_pid_from_file || web_pid_from_port
}

web_alive() { web_pid >/dev/null; }

print_web_port_conflict() {
  error "port $WEB_PORT is already in use by a non-admin-web process:"
  lsof -nP -iTCP:"$WEB_PORT" -sTCP:LISTEN >&2 || true
  error "stop that process or change EIDOLON_ADMIN_WEB_PORT, then retry"
}

do_web_start() {
  ensure_web_deps
  local pid
  if pid="$(web_pid)"; then
    info "web already running (PID $pid)"
    return 0
  fi
  if [[ -n "$(web_listener_pids)" ]]; then
    print_web_port_conflict
    return 1
  fi
  info "starting web (log $WEB_LOG_FILE)"
  (
    cd "$WEB_DIR"
    nohup "./${VITE_BIN_REL}" --port "$WEB_PORT" --strictPort >>"$WEB_LOG_FILE" 2>&1 &
    echo $! >"$WEB_PID_FILE"
  )
  sleep 1
  if ! web_alive; then
    error "web died immediately; tail $WEB_LOG_FILE :"
    tail -30 "$WEB_LOG_FILE" >&2 || true
    rm -f "$WEB_PID_FILE"
    return 1
  fi
  info "web PID $(cat "$WEB_PID_FILE")  /  http://127.0.0.1:${WEB_PORT}/"
}

do_web_stop() {
  local pid
  if ! pid="$(web_pid)"; then
    info "web not running"
    rm -f "$WEB_PID_FILE" "$LEGACY_WEB_PID_FILE" 2>/dev/null || true
    return 0
  fi
  # Vite spawns esbuild + occasional node worker children for HMR. Use
  # kill_tree so they all go down together — otherwise we leave esbuild
  # daemons holding scratch ports / file watches as orphans.
  info "SIGTERM web $pid (+ descendants)"
  kill_tree "$pid" "-TERM"
  for _ in $(seq 1 10); do web_alive || break; sleep 0.2; done
  if web_alive; then
    warn "vite tree still alive after 2s; SIGKILL whole tree"
    kill_tree "$pid" "-KILL"
  fi
  rm -f "$WEB_PID_FILE" "$LEGACY_WEB_PID_FILE"
  info "web stopped"
}

do_web_status() {
  header "admin web (vite)"
  local pid
  if pid="$(web_pid)"; then
    info "running PID $pid"
    echo "  URL: http://127.0.0.1:${WEB_PORT}/"
    echo "  Log: $WEB_LOG_FILE"
  else
    info "not running"
    rm -f "$WEB_PID_FILE" 2>/dev/null || true
  fi
}

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

# Reload deploy/supervisor/enabled/*.conf into a running supervisord.
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

do_sv_passthrough() {
  ensure_api_deps
  exec "${VENV}/bin/supervisorctl" -c "$SV_CONF" "$@"
}

# --- combined ---------------------------------------------------------------

# Pre-flight: refuse to start if any declared port is held by a process
# not under our control. Catches the "previous run's orphan" case before
# it cascades into supervisord's "Exited too quickly" failures.
#
# Pass --force-cleanup to auto-SIGTERM any orphans found (the audit CLI
# handles SIGTERM → re-scan → escalate to SIGKILL → re-scan). If even
# SIGKILL doesn't free the port, we still refuse to start because
# something is deeply wrong and silent failure would be worse.
do_preflight() {
  ensure_api_deps
  # Phase 33.A10: pass --emit-skip-list so the CLI writes any
  # busy-but-optional service IDs (mementos when operator runs it
  # standalone) to a temp file. do_start consumes that list after
  # supervisord boots and stops the corresponding programs, closing the
  # "autostart fires duplicate that crashes on bind" race.
  SKIP_LIST_FILE="$(mktemp -t eidolon-skip-list.XXXXXX)"
  export SKIP_LIST_FILE
  local cli="${VENV}/bin/python -m eidolon_admin_server.app.system_health.cli check --emit-skip-list ${SKIP_LIST_FILE}"
  if [[ -n "${PREFLIGHT_SERVICE_IDS:-}" ]]; then
    cli="$cli --services ${PREFLIGHT_SERVICE_IDS}"
  fi
  if [[ "${PREFLIGHT_CLEANUP:-0}" == "1" ]]; then
    cli="$cli --cleanup"
    info "pre-flight: will SIGTERM Eidolon-looking listeners on declared ports, then continue"
  else
    info "pre-flight: checking declared ports are free (required for cold start)"
  fi
  if ! $cli; then
    rm -f "$SKIP_LIST_FILE"
    exit 1
  fi
}

# Phase 33.A10: stop supervisord programs that correspond to optional
# services whose ports were already bound at pre-flight. Without this,
# supervisord's autostart fires a second copy that crashes on bind and
# loops until autorestart gives up. The skip-list file is whatever
# do_preflight wrote.
do_stop_busy_optionals() {
  local skip_file="${SKIP_LIST_FILE:-}"
  if [[ -z "$skip_file" || ! -s "$skip_file" ]]; then
    return 0
  fi
  info "stopping supervisord programs for already-running optional services"
  while IFS= read -r sid; do
    [[ -z "$sid" ]] && continue
    info "  - $sid (port held by pre-existing process)"
    "${VENV}/bin/supervisorctl" -c "$SV_CONF" stop "$sid" >/dev/null 2>&1 || true
  done < "$skip_file"
  rm -f "$skip_file"
  unset SKIP_LIST_FILE
}

enabled_service_ids_csv() {
  local enabled_dir="${SUPERVISOR_PROFILE_ENABLED_DIR:-${EIDOLON_ADMIN_SUPERVISOR_ENABLED_DIR}}"
  local names=()
  local conf base
  for conf in "${enabled_dir}"/*.conf; do
    [[ -e "$conf" || -L "$conf" ]] || continue
    base="$(basename "$conf" .conf)"
    [[ -n "$base" ]] && names+=("$base")
  done
  local IFS=,
  echo "${names[*]}"
}

do_readiness_wait() {
  local include_admin_web="${1:-0}"
  local timeout="${2:-${EIDOLON_READY_TIMEOUT:-60}}"
  local services="${PREFLIGHT_SERVICE_IDS:-}"
  if [[ "${EIDOLON_SKIP_READY_WAIT:-0}" == "1" ]]; then
    warn "readiness wait skipped by --no-wait-ready / EIDOLON_SKIP_READY_WAIT=1"
    return 0
  fi
  ensure_api_deps
  if [[ -z "$services" ]]; then
    services="$(enabled_service_ids_csv)"
  fi
  if [[ -z "$services" ]]; then
    warn "readiness: no enabled services found; skipping"
    return 0
  fi

  local cmd=(
    "${VENV}/bin/python"
    -m eidolon_admin_server.app.system_health.cli
    wait
    --timeout "$timeout"
    --interval "${EIDOLON_READY_INTERVAL:-0.5}"
    --services "$services"
    --include-supervisor-groups
  )
  if [[ "$include_admin_web" == "1" ]]; then
    cmd+=(--include-admin-web)
  fi
  if [[ "${EIDOLON_READINESS_STRICT:-0}" == "1" ]]; then
    cmd+=(--strict)
  fi

  info "readiness: waiting for ${services} (timeout ${timeout}s)"
  "${cmd[@]}"
}

do_start() {
  collect_ports_registry
  load_ports_env
  migrate_system_data
  # Fresh-clone bootstrap of deploy/supervisor/enabled/. No-op on the
  # second start onward (sentinel-gated), so the operator's Admin UI
  # Enable/Disable decisions are never overridden by this script.
  seed_enabled_symlinks
  header "pre-flight port audit"
  do_preflight
  echo
  header "supervisord (incl. admin-api)"
  do_sv_start
  do_stop_busy_optionals
  echo
  header "admin web"
  do_web_start
  echo
  header "service readiness"
  do_readiness_wait 1
}

do_stop() {
  header "admin web"
  do_web_stop
  echo
  header "supervisord"
  do_sv_stop
}

do_restart() {
  do_stop
  sleep 1
  do_start
}

# Audit the ports this topology is about to bind, before supervisord binds
# them. The dev path has done this since two projects in this workspace first
# claimed one port; this path did not, and the symptom of a squatted port is a
# program in an endless BACKOFF loop with the bind error buried in its own log.
# It happened: eidolon_vision defaults to 8085, which the registry assigns to
# Data's workspace API, and Data spent the whole run in BACKOFF.
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
    EIDOLON_PRODUCT_LIVEKIT_PORT EIDOLON_ADMIN_API_PORT
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
  header "external NATS gate"
  if ! "${OPS_ROOT}/deploy/supervisor/wrappers/wait-tcp.sh" \
    --host 127.0.0.1 --port 4222 --timeout 3 -- /usr/bin/true; then
    error "no NATS is listening on 127.0.0.1:4222"
    error "  This Host's foundation is external (see system-services.yaml:"
    error "  nats has 'supervisord: external'), so Ops does not own the"
    error "  process. Start one and retry:"
    error "    nats-server -js -sd ${EIDOLON_STATE_ROOT}/nats/jetstream \\"
    error "      --port 4222 --http_port 8222"
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
  if [[ "${1:-}" == "--ttl" ]]; then
    ttl="${2:-}"
  elif [[ $# -ne 0 ]]; then
    error "usage: $0 product-source commissioning-code [--ttl SECONDS]"
    return 2
  fi
  if [[ ! "$ttl" =~ ^[0-9]+$ || "$ttl" -lt 60 || "$ttl" -gt 86400 ]]; then
    error "commissioning code TTL must be between 60 and 86400 seconds"
    return 2
  fi
  "${OPS_ROOT}/deploy/supervisor/wrappers/with-env.sh" \
    "$EIDOLON_SOURCE_ADMIN" \
    "${EIDOLON_PRODUCT_ENV_ROOT}/bootstrap.env" \
    -- "$EIDOLON_SOURCE_ADMIN/.venv/bin/eidolon-bootstrapctl" commissioning-code --ttl "$ttl"
}

do_product_source_sv() {
  configure_supervisor_profile product-source
  ensure_product_source_deps
  do_sv_passthrough "$@"
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

do_status() {
  do_web_status
  echo
  do_sv_status
  if [[ "${EIDOLON_STATUS_READINESS:-0}" == "1" ]]; then
    echo
    load_ports_env
    header "service readiness"
    do_readiness_wait 1 "${EIDOLON_STATUS_READY_TIMEOUT:-2}" || true
  fi
}

do_foreground() {
  collect_ports_registry
  load_ports_env
  migrate_system_data
  ensure_web_deps
  cleanup() {
    [[ -n "${API_PID:-}" ]] && kill "$API_PID" 2>/dev/null || true
    [[ -n "${WEB_PID:-}" ]] && kill "$WEB_PID" 2>/dev/null || true
  }
  trap cleanup EXIT INT TERM

  "${VENV}/bin/uvicorn" eidolon_admin_server.app.main:app \
    --host "$API_HOST" --port "$API_PORT" \
    > >(tee -a "$API_FOREGROUND_LOG_FILE") 2>&1 &
  API_PID=$!
  (
    cd "$WEB_DIR"
    "./${VITE_BIN_REL}" --port "$WEB_PORT" --strictPort \
      > >(tee -a "$WEB_FOREGROUND_LOG_FILE") 2>&1
  ) &
  WEB_PID=$!

  echo
  info "eidolon-admin (foreground) — supervisord NOT touched (NATS / sub-projects not started)"
  echo "  API: http://${API_HOST}:${API_PORT}/docs"
  echo "  Web: http://127.0.0.1:${WEB_PORT}/"
  echo "  API log: $API_FOREGROUND_LOG_FILE"
  echo "  Web log: $WEB_FOREGROUND_LOG_FILE"
  echo "  Use '$0 start' for the full stack (NATS, memory, hub, agent, channel, … + vite)."
  echo "  Control-plane calls require separately running eidolond and authority services."
  echo "  Ctrl+C to stop."
  echo
  wait "$API_PID" "$WEB_PID" || true
}

# --- dispatch ---------------------------------------------------------------

# Start/status flags are parsed here and removed from $@ before the case match.
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --force-cleanup)
      export PREFLIGHT_CLEANUP=1
      ;;
    --strict|--strict-readiness)
      export EIDOLON_READINESS_STRICT=1
      ;;
    --no-wait-ready|--no-readiness)
      export EIDOLON_SKIP_READY_WAIT=1
      ;;
    --readiness)
      export EIDOLON_STATUS_READINESS=1
      ;;
    *)
      ARGS+=("$arg")
      ;;
  esac
done
set -- "${ARGS[@]}"

case "${1:-}" in
  start)      do_start ;;
  stop)       do_stop ;;
  restart)    do_restart ;;
  status)     do_status ;;
  foreground) do_foreground ;;
  "")         do_foreground ;;

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
      sv)
        shift
        do_product_source_sv "$@"
        ;;
      *)
        error "unknown product-source command: ${1:-}"
        error "usage: $0 product-source start|stop|restart|status|web-start|web-stop|web-restart|web-status|commissioning-code|sv [...]"
        exit 1
        ;;
    esac
    ;;

  # Targeted admin-web control (api now goes through supervisorctl).
  start-web|web-start)   do_web_start ;;
  stop-web|web-stop)     do_web_stop ;;
  restart-web|web-restart) do_web_stop; sleep 1; do_web_start ;;
  status-web|web-status) do_web_status ;;

  # Compat: old "start-admin" used to mean "start api+web". api is supervised
  # now, so map these to the web-only flow + a friendly hint.
  start-admin)
    warn "admin-api is now supervised; '$0 sv start admin:admin-api' restarts the api."
    do_web_start
    ;;
  stop-admin)
    warn "admin-api is now supervised; '$0 sv stop admin:admin-api' stops the api."
    do_web_stop
    ;;
  restart-admin)
    warn "admin-api is now supervised; restarting admin web (vite) only."
    warn "  to restart the api: '$0 sv restart admin:admin-api'"
    do_web_stop; sleep 1; do_web_start
    ;;
  status-admin)
    do_web_status
    echo
    "${VENV}/bin/supervisorctl" -c "$SV_CONF" status admin:admin-api 2>/dev/null || warn "supervisord not running"
    ;;

  start-sv|sv-start)   do_sv_start ;;
  stop-sv|sv-stop)     do_sv_stop ;;
  status-sv|sv-status) do_sv_status ;;
  sv)
    shift
    do_sv_passthrough "$@"
    ;;

  -h|--help|help)
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    error "unknown command: ${1:-}"
    error ""
    error "Lifecycle (full stack via supervisord):"
    error "  $0 start [--force-cleanup]   cold start — ports must be free first"
    error "  $0 stop                    stop vite + supervisord"
    error "  $0 restart [--force-cleanup] stop then start (use if stack already running)"
    error "  $0 status                    show vite + supervisorctl (no port check)"
    error "  $0 foreground                admin-api + vite only (no NATS / sub-projects)"
    error ""
    error "Partial / passthrough:"
    error "  $0 {start,stop,restart,status}-web"
    error "  $0 {start,stop,status}-sv"
    error "  $0 sv <args>                 supervisorctl passthrough"
    exit 1
    ;;
esac
