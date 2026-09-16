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
            "running_releases": {
                "status": "observed",
                "releases": {"r42": [{"pid": 11, "unit": "eidolon-admin.service"}]},
            },
        }
    )

    assert "当前版本    r42" in output
    assert "实际运行    与 current 链接一致" in output
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


def _serving_report(running: dict[str, object]) -> dict[str, object]:
    """A Host with nothing else wrong with it: every unit loaded and active."""

    return {
        "plan": {"host_id": "eidolon-pi5"},
        "host": "eidolon-box",
        "system": "linux",
        "machine": "aarch64",
        "units": {
            "eidolon-channel-provider.service": {
                "LoadState": "loaded",
                "ActiveState": "active",
                "SubState": "running",
                "NRestarts": 0,
            }
        },
        "current_links": {
            "channel": "/opt/eidolon/releases/pi5-standing-window-20260916b/eidolon_channel"
        },
        "running_releases": running,
    }


def test_pi_status_refuses_to_call_a_host_healthy_that_is_serving_an_old_release() -> None:
    """The 2026-09-16 report, which had no unhealthy unit to show — and said 正常.

    Channel Provider was active, its port answered, and the link named the new
    release; the process was serving the previous one out of a directory that
    had already been deleted. Nothing on this report could say so, so the
    verdict at the top was the wrong one with entirely correct inputs.
    """

    output = render_status(
        _serving_report(
            {
                "status": "observed",
                "releases": {
                    "20260911-pi5-authority-simplification-1": [
                        {"pid": 4711, "unit": "eidolon-channel-provider.service"}
                    ]
                },
            }
        )
    )

    assert "eidolon-channel-provider.service → 20260911-pi5-authority-simplification-1" in output
    assert "重启这些 unit" in output
    assert "1/1 正常" in output, "the unit really is active; that was never the problem"
    assert "总体状态    异常" in output, "the verdict must not read healthy"


def test_pi_status_says_so_when_it_could_not_read_the_processes() -> None:
    """A reading that did not happen is not a reading that came back clean."""

    output = render_status(_serving_report({"status": "unreadable", "releases": {}}))

    assert "实际运行    未能读取进程实际版本" in output
    assert "总体状态    异常" in output
