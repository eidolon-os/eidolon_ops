#!/usr/bin/env bash
# Ops-owned wrapper used by program blocks that need a component's .env
# loaded before exec'ing the real binary.
#
# Usage:
#   with-env.sh <project_dir> <env_file_basename_or_path> -- <cmd...>
#
# Examples:
#   with-env.sh /path/to/eidolon_hub .env -- /path/to/.venv/bin/uvicorn hub.main:app
#   with-env.sh /path/to/eidolon_channel deploy/.livekit-channel.env -- python -m eidolon.livekit.agent.server
#
# - cd into <project_dir>
# - Parse <env_file> if present (KEY=VALUE only; safe against quoting like
#   space-bearing values that would break `source`)
# - exec the remaining argv
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: $0 <project_dir> <env_file> -- <cmd...>" >&2
  exit 2
fi

project_dir="$1"; shift
env_file="$1"; shift

if [[ "$1" != "--" ]]; then
  echo "expected '--' after env file path, got '$1'" >&2
  exit 2
fi
shift

cd "$project_dir"

if [[ ! -f "$env_file" ]]; then
  echo "error: env file not found: ${project_dir}/${env_file}" >&2
  echo "hint: run ./scripts/init-config.sh in ${project_dir}" >&2
  exit 1
fi

while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "${line#"${line%%[![:space:]]*}"}" == \#* ]] && continue
    [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    val="${BASH_REMATCH[2]}"
    val="${val%"${val##*[![:space:]]}"}"
    if [[ "$val" =~ ^\"(.*)\"$ ]] || [[ "$val" =~ ^\'(.*)\'$ ]]; then
      val="${BASH_REMATCH[1]}"
    fi
    # Do not override variables already set in the parent environment —
    # this is the contract supervisord users rely on (admin's ports.yaml /
    # services.yaml inject canonical values via ``environment=`` lines).
    #
    # But silent skipping was confusing: edit project ``.env``, restart,
    # nothing changes because supervisord's environment block still wins.
    # Emit a one-line stderr breadcrumb whenever the values disagree so the
    # operator sees "we kept the parent env value, ignored the .env value"
    # in the supervisord stderr log.
    if [[ -z "${!key+x}" ]]; then
      export "$key=$val"
    elif [[ "${!key}" != "$val" ]]; then
      case "$key" in
        *SECRET*|*TOKEN*|*PASSWORD*|*PRIVATE_KEY*|*API_KEY*)
          echo "with-env: keeping parent ${key}=<redacted> (ignored differing ${env_file} value)" >&2
          ;;
        *)
          echo "with-env: keeping parent ${key}=${!key} (ignored ${env_file} value '${val}')" >&2
          ;;
      esac
    fi
  done < "$env_file"

exec "$@"
