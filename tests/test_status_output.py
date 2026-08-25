from __future__ import annotations

from eidolon_ops.status_output import render_status, render_status_error


def test_mac_status_prioritizes_supervisor_and_unhealthy_services() -> None:
    output = render_status(
        {
            "host_id": "mac-dev",
            "status": "degraded",
            "output": "\x1b[32m[INFO]\x1b[0m not running",
            "health": {
                "status": "degraded",
                "foundation_mode": "external",
                "checks": {
                    "nats": {"healthy": True, "http_status": 200},
                    "admin": {"healthy": False, "http_status": None},
                },
            },
            "network": {"lan_ipv4": "192.168.1.10", "addresses": ["192.168.1.10"]},
            "ports": {
                "nats": [
                    {"label": "Client", "bind": "0.0.0.0", "port": 4222, "protocol": "tcp"}
                ],
                "admin": [
                    {"label": "API", "bind": "127.0.0.1", "port": 9000, "protocol": "tcp"}
                ],
            },
        }
    )

    assert "总体状态    异常" in output
    assert "Supervisor  未运行" in output
    assert "1/2 正常" in output
    assert "nats" in output and "HTTP 200" in output
    assert "admin" in output and "无响应" in output
    assert "LAN IP      192.168.1.10" in output
    assert "192.168.1.10:4222/tcp Client" in output
    assert "127.0.0.1:9000/tcp API" in output
    assert "异常服务    admin" in output
    assert "\x1b" not in output


def test_pi_status_shows_units_release_and_receipt() -> None:
    output = render_status(
        {
            "plan": {"host_id": "eidolon-pi5"},
            "host": "eidolon-box",
            "system": "linux",
            "machine": "aarch64",
            "endpoint": "eidolon.local (192.168.1.20)",
            "network": {
                "lan_ipv4": "192.168.1.20",
                "addresses": ["169.254.10.2", "192.168.1.20"],
            },
            "ports": {
                "admin": [
                    {"label": "API", "bind": "127.0.0.1", "port": 9000, "protocol": "tcp"}
                ],
                "agent": [
                    {"label": "HTTP", "bind": "127.0.0.1", "port": 8180, "protocol": "tcp"}
                ],
            },
            "units": {
                "eidolon-admin.service": {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "running",
                    "NRestarts": 0,
                },
                "eidolon-agent.service": {
                    "LoadState": "loaded",
                    "ActiveState": "failed",
                    "SubState": "failed",
                    "NRestarts": 3,
                },
            },
            "current_links": {
                "admin": "/opt/eidolon/releases/r42/eidolon_admin",
                "agent": "/opt/eidolon/releases/r42/eidolon_agent",
            },
            "recent_receipts": [{"release_id": "r42", "status": "activated"}],
        }
    )

    assert "当前版本    r42" in output
    assert "最近发布    r42 · activated" in output
    assert "1/2 正常" in output
    assert "eidolon-agent.service" in output
    assert "failed/failed · 重启 3" in output
    assert "全部 IP     169.254.10.2, 192.168.1.20" in output
    assert "127.0.0.1:8180/tcp HTTP" in output


def test_status_error_is_actionable() -> None:
    output = render_status_error("connection refused", profile="mac.toml")
    assert "查询失败" in output
    assert "mac.toml" in output
    assert "connection refused" in output
    assert "网络连接" in output
