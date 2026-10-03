"""Onboard a fresh Home Assistant bench and write a long-lived access token.

Runs with the bench venv's Python (it needs only the standard library). On a
fresh instance it creates the owner user through the onboarding API, finishes
the onboarding steps, logs in, and asks the WebSocket API for a long-lived
token. On an already onboarded instance it needs --username/--password.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CLIENT_ID = "http://eidolon.local/ha-bench"


def post(url: str, body: dict | None = None, *, form: bool = False, headers: dict | None = None) -> dict:
    data = urllib.parse.urlencode(body).encode() if form else json.dumps(body or {}).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded" if form else "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def onboard(base: str, username: str, password: str) -> str | None:
    """Create the owner through onboarding; returns an auth code, or None if already onboarded."""
    try:
        status = json.loads(urllib.request.urlopen(f"{base}/api/onboarding", timeout=30).read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if all(step["done"] for step in status):
        return None
    created = post(
        f"{base}/api/onboarding/users",
        {"client_id": CLIENT_ID, "name": "Eidolon Bench", "username": username, "password": password, "language": "zh-Hans"},
    )
    code = created["auth_code"]
    token = post(f"{base}/auth/token", {"client_id": CLIENT_ID, "grant_type": "authorization_code", "code": code}, form=True)
    bearer = {"Authorization": f"Bearer {token['access_token']}"}
    post(f"{base}/api/onboarding/core_config", {}, headers=bearer)
    post(f"{base}/api/onboarding/analytics", {}, headers=bearer)
    integration = post(f"{base}/api/onboarding/integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID}, headers=bearer)
    return integration["auth_code"]


def login(base: str, username: str, password: str) -> str:
    flow = post(f"{base}/auth/login_flow", {"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID})
    result = post(f"{base}/auth/login_flow/{flow['flow_id']}", {"client_id": CLIENT_ID, "username": username, "password": password})
    if result.get("type") != "create_entry":
        raise SystemExit(f"login did not complete: {result}")
    return result["result"]


def access_token(base: str, code: str) -> str:
    token = post(f"{base}/auth/token", {"client_id": CLIENT_ID, "grant_type": "authorization_code", "code": code}, form=True)
    return token["access_token"]


async def long_lived_token(ws_url: str, access: str) -> str:
    # aiohttp is a dependency of Home Assistant itself, so the bench venv has it.
    import aiohttp

    async with aiohttp.ClientSession() as session, session.ws_connect(ws_url, max_msg_size=2**22) as ws:
        assert (await ws.receive_json())["type"] == "auth_required"
        await ws.send_json({"type": "auth", "access_token": access})
        assert (await ws.receive_json())["type"] == "auth_ok"
        await ws.send_json(
            {"id": 1, "type": "auth/long_lived_access_token", "client_name": "Eidolon Hub", "lifespan": 3650}
        )
        reply = await ws.receive_json()
        if not reply.get("success"):
            raise SystemExit(f"could not create a long-lived token: {reply}")
        return reply["result"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8123")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--username", default="eidolon")
    parser.add_argument("--password", default=None, help="defaults to a generated one, stored beside the token")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    secret_path = args.out.with_suffix(".password")
    password = args.password or (secret_path.read_text().strip() if secret_path.exists() else secrets.token_urlsafe(16))
    # Persisted before onboarding: a failure after the user exists must not lose it.
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(password + "\n")
    secret_path.chmod(0o600)
    code = onboard(base, args.username, password)
    if code is None:
        code = login(base, args.username, password)
    access = access_token(base, code)
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
    token = asyncio.run(long_lived_token(ws_url, access))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(token + "\n")
    args.out.chmod(0o600)
    secret_path.write_text(password + "\n")
    secret_path.chmod(0o600)
    print(f"long-lived token written to {args.out} (user {args.username})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
