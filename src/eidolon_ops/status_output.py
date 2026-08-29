"""Human-readable rendering for the two Host status contracts.

The JSON document remains the machine contract.  This module is deliberately
only a view over it: Mac source runs report HTTP health checks, while product
Hosts report systemd units, active release links and receipts.  Flattening both
into generic key/value output would hide the facts an operator asks for first.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_GOOD_STATUSES = frozenset({"healthy", "ok", "observed", "app_ready"})
_UNIT_SERVICES = {
    "eidolon-bootstrapd.service": "bootstrap",
    "eidolond.service": "eidolond",
    "eidolon-data.service": "data",
    "eidolon-data-workspace.service": "data-workspace",
    "eidolon-hub.service": "hub-api",
    "eidolon-hub-ingress.service": "hub-ingress",
    "eidolon-kernel.service": "kernel",
    "eidolon-local-api.service": "local-api",
    "eidolon-lifecycle-workflow.service": "lifecycle-workflow",
    "eidolon-admin.service": "admin",
    "eidolon-nats.service": "nats",
    "eidolon-livekit.service": "livekit",
    "eidolon-memory-embedder.service": "memory-embedder",
    "eidolon-memory-supervisor.service": "memory-supervisor",
    "eidolon-memory-discovery.service": "memory-discovery",
    "eidolon-agent.service": "agent",
    "eidolon-channel-provider.service": "channel-provider",
    "eidolon-channel.service": "channel",
}


def render_status(document: Mapping[str, object]) -> str:
    """Render status evidence as a compact operator report."""

    health = _mapping(document.get("health"))
    checks = _mapping(health.get("checks"))
    units = _mapping(document.get("units"))
    if checks:
        return _render_mac(document, health, checks)
    if units:
        return _render_pi(document, units)
    return _render_generic(document)


def render_status_error(error: str, *, profile: object | None = None) -> str:
    lines = ["Eidolon Host 状态", "=================", "总体状态   查询失败"]
    if profile is not None:
        lines.append(f"主机配置   {profile}")
    lines.extend((f"原因       {error}", "", "请检查主机配置、网络连接和凭据后重试。"))
    return "\n".join(lines)


def _render_mac(
    document: Mapping[str, object],
    health: Mapping[str, object],
    checks: Mapping[str, object],
) -> str:
    rows: list[tuple[str, str, str, str]] = []
    failures: list[str] = []
    for name, raw in checks.items():
        check = _mapping(raw)
        healthy = check.get("healthy") is True
        state = "正常" if healthy else "异常"
        detail = _health_detail(check)
        rows.append((str(name), state, _port_summary(document, str(name)), detail))
        if not healthy:
            failures.append(str(name))

    supervisor = _supervisor_state(document.get("output"))
    healthy_count = len(rows) - len(failures)
    overall = not failures and _status_is_good(health.get("status"))
    lines = _header(
        document,
        overall=overall,
        host_detail="Mac · product-source",
    )
    lines.extend(
        (
            *_network_lines(document),
            f"Supervisor  {supervisor}",
            f"基础设施    {health.get('foundation_mode', '-')}",
            f"服务        {healthy_count}/{len(rows)} 正常",
            "",
            _table(("服务", "状态", "监听地址 / 端口", "详情"), rows),
        )
    )
    if failures:
        lines.extend(("", f"异常服务    {', '.join(failures)}"))
        if supervisor == "未运行":
            lines.append("建议        运行该主机的 start 命令，然后再次执行 status。")
        else:
            lines.append("建议        查看异常服务日志；需要原始诊断数据时使用 status --json。")
    return "\n".join(lines)


def _render_pi(document: Mapping[str, object], units: Mapping[str, object]) -> str:
    rows: list[tuple[str, str, str, str]] = []
    failures: list[str] = []
    for name, raw in units.items():
        unit = _mapping(raw)
        load = str(unit.get("LoadState", "-"))
        active = str(unit.get("ActiveState", "-"))
        sub = str(unit.get("SubState", "-"))
        restarts = unit.get("NRestarts", "-")
        error = unit.get("error")
        healthy = load == "loaded" and active == "active" and error is None
        state = "正常" if healthy else "异常"
        detail = str(error) if error else f"{active}/{sub} · 重启 {restarts}"
        service = _UNIT_SERVICES.get(str(name), str(name))
        rows.append((str(name), state, _port_summary(document, service), detail))
        if not healthy:
            failures.append(str(name))

    releases = _release_ids(_mapping(document.get("current_links")))
    latest_receipt = _latest_receipt(document.get("recent_receipts"))
    healthy_count = len(rows) - len(failures)
    lines = _header(
        document,
        overall=not failures,
        host_detail=" · ".join(
            value
            for value in (
                str(document.get("system", "")),
                str(document.get("machine", "")),
                str(document.get("host", "")),
            )
            if value
        )
        or "Pi product Host",
    )
    lines.extend(
        (
            f"连接        {document.get('endpoint', '-')}",
            *_network_lines(document),
            f"当前版本    {', '.join(releases) if releases else '未发现 active release'}",
            f"最近发布    {latest_receipt}",
            f"服务        {healthy_count}/{len(rows)} 正常",
            f"代码差距    {_behind_summary(document)}",
            "",
            _table(("Systemd Unit", "状态", "监听地址 / 端口", "详情"), rows),
        )
    )
    if failures:
        lines.extend(
            (
                "",
                f"异常服务    {', '.join(failures)}",
                "建议        查看异常 unit 日志；需要原始诊断数据时使用 status --json。",
            )
        )
    return "\n".join(lines)


def _behind_summary(document: Mapping[str, object]) -> str:
    """One line for the question status cannot otherwise answer.

    A Host can be entirely healthy and still not be running what was committed
    an hour ago, and every service reading 正常 is exactly when nobody thinks to
    check. Counts only; `pending` names the commits.
    """

    behind = _mapping(document.get("behind"))
    if not behind:
        return "-"
    commits = behind.get("pending_commits")
    sources = behind.get("sources")
    uncommitted = _mapping(behind.get("uncommitted"))
    parts = []
    if isinstance(commits, int) and commits and isinstance(sources, list):
        parts.append(f"本机领先 {commits} 个提交（{', '.join(str(item) for item in sources)}）")
    if uncommitted:
        parts.append(f"{len(uncommitted)} 个源有未提交改动")
    if not parts:
        return "与 Host 一致"
    return " · ".join(parts) + " —— 详情见 pending"


def _render_generic(document: Mapping[str, object]) -> str:
    status = str(document.get("status", "unknown"))
    lines = _header(document, overall=_status_is_good(status), host_detail="Host")
    lines.append(f"报告状态   {status}")
    if error := document.get("error"):
        lines.append(f"原因       {error}")
    lines.extend(("", "没有可展示的服务明细；使用 status --json 查看原始报告。"))
    return "\n".join(lines)


def _header(
    document: Mapping[str, object], *, overall: bool, host_detail: str
) -> list[str]:
    plan = _mapping(document.get("plan"))
    host_id = document.get("host_id") or plan.get("host_id") or "-"
    return [
        "Eidolon Host 状态",
        "=================",
        f"主机        {host_id}",
        f"类型        {host_detail}",
        f"总体状态    {'正常' if overall else '异常'}",
    ]


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    widths = [_display_width(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], _display_width(value))
    lines = ["  ".join(_pad(value, widths[index]) for index, value in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend("  ".join(_pad(value, widths[index]) for index, value in enumerate(row)) for row in rows)
    return "\n".join(lines)


def _display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1 for character in value)


def _pad(value: str, width: int) -> str:
    return value + " " * max(0, width - _display_width(value))


def _network_lines(document: Mapping[str, object]) -> tuple[str, ...]:
    network = _mapping(document.get("network"))
    lan_ipv4 = network.get("lan_ipv4") or _endpoint_ipv4(document.get("endpoint")) or "未发现"
    raw_addresses = network.get("addresses")
    addresses = [str(value) for value in raw_addresses] if isinstance(raw_addresses, list) else []
    return (
        f"LAN IP      {lan_ipv4}",
        f"全部 IP     {', '.join(addresses) if addresses else lan_ipv4}",
    )


def _endpoint_ipv4(raw: object) -> str | None:
    found = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", str(raw or ""))
    return found.group(1) if found else None


def _port_summary(document: Mapping[str, object], service: str) -> str:
    ports = _mapping(document.get("ports"))
    raw_endpoints = ports.get(service)
    if not isinstance(raw_endpoints, list) or not raw_endpoints:
        return "—"
    network = _mapping(document.get("network"))
    lan_ipv4 = str(network.get("lan_ipv4") or _endpoint_ipv4(document.get("endpoint")) or "*")
    rendered: list[str] = []
    for raw in raw_endpoints:
        endpoint = _mapping(raw)
        port = endpoint.get("port")
        if not isinstance(port, int):
            continue
        end_port = endpoint.get("end_port")
        port_text = f"{port}-{end_port}" if isinstance(end_port, int) else str(port)
        bind = str(endpoint.get("bind", "*"))
        display_host = lan_ipv4 if bind == "0.0.0.0" else bind
        protocol = str(endpoint.get("protocol", "tcp"))
        label = str(endpoint.get("label", ""))
        rendered.append(f"{display_host}:{port_text}/{protocol} {label}".rstrip())
    return "; ".join(rendered) if rendered else "—"


def _health_detail(check: Mapping[str, object]) -> str:
    status = check.get("http_status")
    if isinstance(status, int):
        return f"HTTP {status}"
    if status is None:
        return "无响应"
    return str(status)


def _supervisor_state(raw: object) -> str:
    output = _ANSI.sub("", str(raw or ""))
    if "not running" in output.lower():
        return "未运行"
    if "running" in output.lower():
        return "运行中"
    return "状态未知"


def _release_ids(links: Mapping[str, object]) -> list[str]:
    found: list[str] = []
    for raw in links.values():
        if not isinstance(raw, str):
            continue
        match = re.search(r"/releases/([^/]+)", raw)
        release = match.group(1) if match else raw.rstrip("/").split("/")[-1]
        if release and release not in found:
            found.append(release)
    return found


def _latest_receipt(raw: object) -> str:
    if not isinstance(raw, list) or not raw:
        return "无记录"
    receipt = _mapping(raw[-1])
    release = receipt.get("release_id") or "未知版本"
    status = receipt.get("status") or "未知状态"
    return f"{release} · {status}"


def _status_is_good(raw: object) -> bool:
    return str(raw).lower() in _GOOD_STATUSES


def _mapping(raw: object) -> Mapping[str, object]:
    return raw if isinstance(raw, Mapping) else {}


def render_pending(document: Mapping[str, object]) -> str:
    """Render the gap between these checkouts and the Host, for a person.

    The same view-over-the-contract rule as the status renderers above: the JSON
    stays the machine answer, and this exists because the question — "is my
    commit on the board" — is asked by someone who wants to read the answer, not
    parse it.
    """

    active = document.get("active_release")
    pending = _mapping(document.get("pending"))
    uncommitted = _mapping(document.get("uncommitted"))
    lines = [
        "本机与 Host 的差距",
        "==================",
        f"主机        {document.get('host_id') or _mapping(document.get('plan')).get('host_id') or '-'}",
        f"链路        {document.get('endpoint') or '-'}",
        f"运行中      {active or '未知'}",
    ]
    if not pending and not uncommitted:
        lines.extend(("", f"每个源都与 {active} 一致，没有未部署的提交。"))
        return "\n".join(lines)

    if pending:
        rows = []
        for source_id, entry in sorted(pending.items()):
            values = _mapping(entry)
            commits = values.get("commits")
            rows.append(
                (
                    source_id,
                    str(commits) if isinstance(commits, int) else "?",
                    str(values.get("shipped") or "-")[:12],
                    str(values.get("head") or "-")[:12],
                    str(values.get("reason") or ""),
                )
            )
        lines.extend(
            ("", "未部署的提交", _table(("源", "提交数", "Host 上", "本机 HEAD", "说明"), rows))
        )
        for source_id, entry in sorted(pending.items()):
            subjects = _mapping(entry).get("subjects")
            if isinstance(subjects, list) and subjects:
                lines.append(f"  {source_id}:")
                lines.extend(f"    {subject!s}" for subject in subjects)

    if uncommitted:
        rows = [
            (source_id, str(_mapping(entry).get("paths") or "?"))
            for source_id, entry in sorted(uncommitted.items())
        ]
        lines.extend(
            (
                "",
                "未提交的改动（任何 release 都带不走，deploy 会直接拒绝）",
                _table(("源", "文件数"), rows),
            )
        )
        for source_id, entry in sorted(uncommitted.items()):
            sample = _mapping(entry).get("sample")
            if isinstance(sample, list) and sample:
                lines.append(f"  {source_id}:")
                lines.extend(f"    {item!s}" for item in sample)

    lines.extend(("", str(document.get("detail") or "")))
    return "\n".join(lines)
