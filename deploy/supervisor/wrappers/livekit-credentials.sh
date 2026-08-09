#!/usr/bin/env bash
# Generate or load the Host-local LiveKit credential pair without sourcing
# data as shell code. This file is sourced by run_all.sh so exports reach the
# supervisord parent environment; tests may source it independently.

eidolon_ensure_livekit_credentials() {
  local credentials_file="${EIDOLON_LIVEKIT_CREDENTIALS_FILE:-${EIDOLON_STATE_ROOT}/livekit/credentials.env}"
  local credentials_dir
  credentials_dir="$(dirname "$credentials_file")"

  if [[ -L "$credentials_dir" ]]; then
    echo "LiveKit credential directory is a symlink: $credentials_dir" >&2
    return 1
  fi
  mkdir -p "$credentials_dir"
  chmod 700 "$credentials_dir"

  if [[ -n "${LIVEKIT_API_KEY:-}" || -n "${LIVEKIT_API_SECRET:-}" ]]; then
    if [[ -z "${LIVEKIT_API_KEY:-}" || -z "${LIVEKIT_API_SECRET:-}" ]]; then
      echo "set both LIVEKIT_API_KEY and LIVEKIT_API_SECRET, or neither" >&2
      return 1
    fi
  else
    if [[ -L "$credentials_file" ]]; then
      echo "LiveKit credential file is a symlink: $credentials_file" >&2
      return 1
    fi
    if [[ -e "$credentials_file" && ! -f "$credentials_file" ]]; then
      echo "LiveKit credential path is not a regular file: $credentials_file" >&2
      return 1
    fi
    if [[ ! -f "$credentials_file" ]]; then
      local generated_key generated_secret temporary
      generated_key="eidolon_$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
      generated_secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
      temporary="$(mktemp "${credentials_dir}/.credentials.XXXXXX")"
      chmod 600 "$temporary"
      printf 'LIVEKIT_API_KEY=%s\nLIVEKIT_API_SECRET=%s\n' \
        "$generated_key" "$generated_secret" >"$temporary"
      mv "$temporary" "$credentials_file"
    fi
    chmod 600 "$credentials_file"

    local name value key_seen=0 secret_seen=0
    LIVEKIT_API_KEY=""
    LIVEKIT_API_SECRET=""
    while IFS='=' read -r name value || [[ -n "$name" ]]; do
      case "$name" in
        LIVEKIT_API_KEY)
          ((key_seen += 1))
          LIVEKIT_API_KEY="$value"
          ;;
        LIVEKIT_API_SECRET)
          ((secret_seen += 1))
          LIVEKIT_API_SECRET="$value"
          ;;
        "") ;;
        *)
          echo "unexpected key in LiveKit credential file: $name" >&2
          return 1
          ;;
      esac
    done <"$credentials_file"
    if [[ "$key_seen" -ne 1 || "$secret_seen" -ne 1 ]]; then
      echo "LiveKit credential file must contain each required key exactly once" >&2
      return 1
    fi
  fi

  if [[ ! "${LIVEKIT_API_KEY:-}" =~ ^[A-Za-z0-9_-]{16,64}$ ]] \
    || [[ ! "${LIVEKIT_API_SECRET:-}" =~ ^[A-Za-z0-9_-]{32,128}$ ]]; then
    echo "LiveKit credential shape is invalid" >&2
    return 1
  fi
  export LIVEKIT_API_KEY LIVEKIT_API_SECRET
}
