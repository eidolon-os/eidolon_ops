"""The files a macOS source run needs, rendered from the pinned sources.

Only the rendering lives here. What is rendered — which env files, which
settings, which template placeholders — is the same set the product Host gets;
where the two genuinely differ is the path layout and the port assignment, and
both of those are data in this module rather than branches in the caller.
"""

from __future__ import annotations

import re
from pathlib import Path

from eidolon_ops import environment
from eidolon_ops.config import OperationsConfig
from eidolon_ops.errors import OperationsError
from eidolon_ops.paths import PRODUCT_PATHS, HostProfile
from eidolon_ops.readiness import CHANNEL_WORKER_PORT, LIVEKIT_SIGNALLING_PORT

ENV_NAMES = (
    "data.env",
    "hub.env",
    "kernel.env",
    "admin.env",
    "local-api.env",
    "bootstrap.env",
    "agent.env",
    "channel.env",
    "memory.env",
    "livekit.env",
)
SETTING_INPUT_NAMES = ("agent.yaml", "channel.yaml", "memory.yaml")
#: Kernel's own product profiles: what eidolond runs and what the manifest of
#: system services says. Hub's settings used to be a third entry here and is
#: not one any more — it is rendered from Hub's own repository, which is what
#: :data:`eidolon_ops.hub_assets.HUB_SETTINGS_TEMPLATE` names.
KERNEL_SETTINGS = {
    "kernel.yaml": "config/kernel.systemd.example.yaml",
    "system-services.yaml": "config/system-services.yaml",
}
#: The rendered Hub settings a source run starts Hub with, under the profile's
#: settings root like every other name in this module.
HUB_SETTINGS_NAME = "hub.yaml"

#: Which port each component binds on a source run. ``config/ports.yaml`` is
#: the registry a product Host is sent; this is the same assignment for the
#: profile that runs here, and the two Channel/LiveKit entries it shares with
#: the readiness payload come from one place so they cannot disagree.
PORTS = {
    "nats": 4222,
    "nats_http": 8222,
    "livekit": LIVEKIT_SIGNALLING_PORT,
    "memory_admin": 8019,
    "memory_discovery": 8020,
    "memory_embedder": 8760,
    "agent_admin": 8081,
    "agent_http": 8180,
    "hub": 8082,
    "kernel": 8083,
    "data": 8084,
    "data_workspace": 8085,
    "eidolond": 8090,
    "channel_worker": CHANNEL_WORKER_PORT,
    "channel_provider": 8767,
    # Local recognition, on a Host that does it itself. 8767 is the Channel
    # Provider's and was this service's default until the two were found to
    # collide by reading, which is not a mechanism -- see the drift tests.
    "asr_stream": 8768,
    # Local generation, likewise. An OpenAI-compatible server, so the Agent
    # reaches it as it reaches any other -- the port is the only thing about it
    # that is ours to assign.
    "llm_api": 8769,
    # This Host's own voice. Also loopback, and also not in the dev stack: the
    # engine loads RKNN graphs onto an NPU a workstation does not have.
    "tts_stream": 8770,
    "admin": 9000,
    "admin_web": 9001,
    "local_api": 9002,
}


def status_ports(*, hub_https_port: int | None = None) -> dict[str, list[dict[str, object]]]:
    """Every operator-relevant listener, derived from the owned port table.

    Health endpoints name only one port per service. Status has a different
    job: show every listener an operator may need to diagnose or reach, such as
    Agent's HTTP/Admin/gRPC trio and LiveKit's signalling/TURN/RTC surfaces.
    """

    def endpoint(
        label: str,
        bind: str,
        port: int,
        *,
        protocol: str = "tcp",
        end_port: int | None = None,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "label": label,
            "bind": bind,
            "port": port,
            "protocol": protocol,
        }
        if end_port is not None:
            value["end_port"] = end_port
        return value

    hub_lan_port = hub_https_port or 8443
    data = {
        "admin": [endpoint("API", "127.0.0.1", PORTS["admin"])],
        "admin-web": [endpoint("Web", "127.0.0.1", PORTS["admin_web"])],
        "data": [endpoint("API", "127.0.0.1", PORTS["data"])],
        "data-workspace": [endpoint("Workspace API", "127.0.0.1", PORTS["data_workspace"])],
        "hub-api": [endpoint("API", "127.0.0.1", PORTS["hub"])],
        "hub-ingress": [endpoint("HTTPS", "0.0.0.0", hub_lan_port)],
        "kernel": [endpoint("API", "127.0.0.1", PORTS["kernel"])],
        "eidolond": [endpoint("System API", "127.0.0.1", PORTS["eidolond"])],
        "local-api": [endpoint("HTTPS", "0.0.0.0", PORTS["local_api"])],
        "memory-supervisor": [
            endpoint("Supervisor", "127.0.0.1", PORTS["memory_admin"]),
            endpoint("MCP base", "127.0.0.1", 10030),
        ],
        "memory-embedder": [endpoint("Embedding", "127.0.0.1", PORTS["memory_embedder"])],
        "memory-discovery": [endpoint("Discovery", "127.0.0.1", PORTS["memory_discovery"])],
        "agent": [
            endpoint("HTTP", "127.0.0.1", PORTS["agent_http"]),
            endpoint("Admin", "127.0.0.1", PORTS["agent_admin"]),
            endpoint("gRPC", "127.0.0.1", 45051),
        ],
        "channel-provider": [endpoint("Provider", "127.0.0.1", PORTS["channel_provider"])],
        "channel": [endpoint("Worker", "127.0.0.1", PORTS["channel_worker"])],
        "nats": [
            endpoint("Client", "0.0.0.0", PORTS["nats"]),
            endpoint("Monitoring", "0.0.0.0", PORTS["nats_http"]),
        ],
        "livekit": [
            endpoint("Signalling", "0.0.0.0", PORTS["livekit"]),
            endpoint("TURN", "0.0.0.0", 3478, protocol="udp"),
            endpoint("RTC", "0.0.0.0", 50000, protocol="udp", end_port=60000),
        ],
    }
    data["hub"] = [*data["hub-api"], *data["hub-ingress"]]
    data["memory"] = [
        *data["memory-embedder"],
        *data["memory-supervisor"],
        *data["memory-discovery"],
    ]
    return data


#: Deliberately outside :data:`PORTS`, which is checked for equality against the
#: ports the components declare: the browser client is not one of them. It is
#: still an origin Admin has to admit and a port nothing else may be handed, and
#: three places used to write ``3001`` for it — the registry, the external
#: service list, and (not at all) Admin's CORS allow-list, which is how the web
#: client ended up refused by the browser on every source run.
CLIENT_WEB_PORT = 3001

_MEMORY_SUPERVISOR_ANCHOR = "supervisor:\n  eager_init: true"


#: Templates Ops ships. Beside the code rather than at a repository root, so
#: they are found the same way from a checkout and from an installed wheel.
ASSETS = Path(__file__).with_name("assets")


def translate_fhs(profile: HostProfile, value: str) -> str:
    """Point a product-shaped template at this workstation's own roots.

    A component may say where its state goes either as the product path or as
    ``$EIDOLON_STATE_ROOT``, which resolves to the same place on either kind of
    Host because both export it. Both are translated: a settings file an
    operator opens should say which directory it means, rather than leave the
    answer to whichever environment the service happened to inherit.

    Every role in :data:`eidolon_ops.paths.PRODUCT_PATHS` is translated, and the
    substitutions are derived from that table rather than restated here. The
    restated version omitted ``config_root``, so Hub's settings template — whose
    three Owner Domain paths are under ``/etc/eidolon`` — reached a Mac source
    run still pointing at a directory that does not exist on it. Nothing failed
    at generation time; Hub failed at startup, on every run, for weeks.
    """

    paths = profile.paths
    replacements: list[tuple[str, str]] = []
    for role, product_path in PRODUCT_PATHS.items():
        host_path = str(getattr(paths, role))
        replacements.append((str(product_path), host_path))
        replacements.append((f"${_ENVIRONMENT_NAMES[role]}", host_path))
    for old, new in replacements:
        value = value.replace(old, new)
    return translate_ports(value)


#: The variable a component writes when it defers a root to the Host, per role.
#: Taken from the same export table the services are started with, so a template
#: may say either the product path or the variable and mean one thing.
_ENVIRONMENT_NAMES = {
    "install_root": "EIDOLON_INSTALL_ROOT",
    "current_root": "EIDOLON_WORKSPACE_ROOT",
    "config_root": "EIDOLON_CONFIG_ROOT",
    "state_root": "EIDOLON_STATE_ROOT",
    "runtime_root": "EIDOLON_RUNTIME_ROOT",
    "log_root": "EIDOLON_LOG_ROOT",
    "cache_root": "EIDOLON_CACHE_ROOT",
    "bootstrap_state_root": "EIDOLON_BOOTSTRAP_STATE_ROOT",
    "bootstrap_runtime_root": "EIDOLON_BOOTSTRAP_RUNTIME_ROOT",
}


def translate_ports(value: str) -> str:
    if _MEMORY_SUPERVISOR_ANCHOR in value and "admin_http_port:" not in value:
        value = value.replace(
            _MEMORY_SUPERVISOR_ANCHOR,
            f"{_MEMORY_SUPERVISOR_ANCHOR}\n  admin_http_port: {PORTS['memory_admin']}",
        )
    return value


def memory_supervisor_block() -> str:
    return f"\n\n{_MEMORY_SUPERVISOR_ANCHOR}\n  admin_http_port: {PORTS['memory_admin']}\n"


def eidolond_settings(profile: HostProfile) -> str:
    paths = profile.paths
    # The workspace this profile drives, which is a fact the profile carries.
    # It was read off this module's own __file__ before, which happened to
    # agree only because both were in one checkout.
    root = profile.workspace_root
    return f"""\
manifest:
  path: {paths.config_root / "settings/system-services.yaml"}
persistence:
  path: {paths.state_root / "eidolond.sqlite3"}
host:
  driver: supervisord
  supervisorctl: {root / ".venv/bin/supervisorctl"}
  supervisor_config: {root / "deploy/dev/supervisord.profile.conf"}
  command_timeout_seconds: 20
reconciliation:
  interval_seconds: 5
  readiness_timeout_seconds: 3
interface:
  host: 127.0.0.1
  port: {PORTS["eidolond"]}
  uds: {paths.runtime_root / "system.sock"}
  uds_mode: "0600"
"""


def profile_environment(
    profile: HostProfile,
    config: OperationsConfig,
    *,
    hub_certificate: Path,
    hub_private_key: Path,
    lan_ipv4: str,
    hub_https_port: int,
) -> str:
    values: dict[str, str] = {
        **profile.environment(),
        "EIDOLON_PRODUCT_ENV_ROOT": str(profile.paths.config_root / "env"),
        "EIDOLON_PRODUCT_SETTINGS_ROOT": str(profile.paths.config_root / "settings"),
        "EIDOLON_LIVEKIT_TEMPLATE_CONFIG": str(profile.paths.config_root / "settings/livekit.yaml"),
        "EIDOLON_ADMIN_API_HOST": "127.0.0.1",
        "EIDOLON_ADMIN_API_PORT": str(PORTS["admin"]),
        "EIDOLON_ADMIN_WEB_PORT": str(PORTS["admin_web"]),
        "EIDOLON_PRODUCT_NATS_PORT": str(PORTS["nats"]),
        "EIDOLON_PRODUCT_NATS_HTTP_PORT": str(PORTS["nats_http"]),
        "EIDOLON_PRODUCT_LIVEKIT_PORT": str(PORTS["livekit"]),
        "EIDOLON_PRODUCT_MEMORY_ADMIN_PORT": str(PORTS["memory_admin"]),
        "EIDOLON_PRODUCT_MEMORY_DISCOVERY_PORT": str(PORTS["memory_discovery"]),
        "EIDOLON_PRODUCT_AGENT_ADMIN_PORT": str(PORTS["agent_admin"]),
        "EIDOLON_PRODUCT_AGENT_HTTP_PORT": str(PORTS["agent_http"]),
        "EIDOLON_PRODUCT_HUB_PORT": str(PORTS["hub"]),
        "EIDOLON_PRODUCT_KERNEL_PORT": str(PORTS["kernel"]),
        "EIDOLON_PRODUCT_DATA_PORT": str(PORTS["data"]),
        "EIDOLON_PRODUCT_DATA_WORKSPACE_PORT": str(PORTS["data_workspace"]),
        "EIDOLON_PRODUCT_EIDOLOND_PORT": str(PORTS["eidolond"]),
        "EIDOLON_PRODUCT_CHANNEL_WORKER_PORT": str(PORTS["channel_worker"]),
        "EIDOLON_PRODUCT_CHANNEL_PROVIDER_PORT": str(PORTS["channel_provider"]),
        "EIDOLON_PRODUCT_LOCAL_API_PORT": str(PORTS["local_api"]),
        "EIDOLON_APP_LAN_IPV4": lan_ipv4,
        "EIDOLON_APP_HUB_HTTPS_PORT": str(hub_https_port),
        "EIDOLON_APP_HUB_TLS_CERTIFICATE": str(hub_certificate),
        "EIDOLON_APP_HUB_TLS_PRIVATE_KEY": str(hub_private_key),
        "EIDOLON_SOURCE_KERNEL": str(config.sources["eidolon_kernel"].path),
        "EIDOLON_SOURCE_DATA": str(config.sources["eidolon_data"].path),
        "EIDOLON_SOURCE_HUB": str(config.sources["eidolon_hub"].path),
        "EIDOLON_SOURCE_ADMIN": str(config.sources["eidolon_admin"].path),
        "EIDOLON_SOURCE_AGENT": str(config.sources["eidolon_agent"].path),
        "EIDOLON_SOURCE_CHANNEL": str(config.sources["eidolon_channel"].path),
        "EIDOLON_SOURCE_MEMORY": str(config.sources["eidolon_memory"].path),
    }
    if profile.external_livekit_config is not None:
        values["EIDOLON_LIVEKIT_GENERATED_CONFIG"] = str(profile.external_livekit_config)
    return environment.serialize_ops(values)


def admin_ports_yaml() -> str:
    return f"""\
admin:
  api: {{host: 127.0.0.1, port: {PORTS["admin"]}}}
  web: {{port: {PORTS["admin_web"]}}}
hub:
  api: {{host: 127.0.0.1, port: {PORTS["hub"]}}}
data:
  api: {{host: 127.0.0.1, port: {PORTS["data"]}}}
  workspace_api: {{host: 127.0.0.1, port: {PORTS["data_workspace"]}}}
kernel:
  api: {{host: 127.0.0.1, port: {PORTS["kernel"]}}}
eidolond:
  api: {{host: 127.0.0.1, port: {PORTS["eidolond"]}}}
agent:
  http: {{port: {PORTS["agent_http"]}}}
  admin: {{port: {PORTS["agent_admin"]}}}
  grpc: {{port: 45051}}
memory:
  embedder: {{host: 127.0.0.1, port: {PORTS["memory_embedder"]}}}
  discovery: {{host: 127.0.0.1, port: {PORTS["memory_discovery"]}}}
  mcp: {{port: 10030}}
  supervisor_http: {{host: 127.0.0.1, port: {PORTS["memory_admin"]}}}
channel:
  worker: {{port: {PORTS["channel_worker"]}}}
  provider: {{port: {PORTS["channel_provider"]}}}
client_web: {{port: {CLIENT_WEB_PORT}}}
nats: {{port: {PORTS["nats"]}, http_port: {PORTS["nats_http"]}}}
livekit:
  port: {PORTS["livekit"]}
  turn_udp_port: 3478
  rtc_port_start: 50000
  rtc_port_end: 60000
"""


#: Each managed service as Admin's catalog names it: id, label, supervisord
#: group, programs, and the health surface it publishes.
_MANAGED_SERVICES = (
    ("admin", "Eidolon Admin", "admin", ("admin-api",), "http://127.0.0.1:{admin}/docs"),
    (
        "admin-web",
        "Eidolon Admin Web",
        "admin-web",
        ("admin-web",),
        "http://127.0.0.1:{admin_web}/",
    ),
    ("bootstrap", "Eidolon Bootstrap", "bootstrap", ("bootstrapd",), None),
    ("local-api", "Eidolon Local API", "local-api", ("local-api",), None),
    ("eidolond", "Eidolon System Manager", "eidolond", ("eidolond",), None),
    (
        "data",
        "Eidolon Data V2 Authority",
        "data",
        ("data-api",),
        "http://127.0.0.1:{data}/health",
    ),
    (
        "data-workspace",
        "Eidolon Data Workspace Authority",
        "data",
        ("data-workspace-api",),
        "http://127.0.0.1:{data_workspace}/health",
    ),
    ("hub", "Eidolon Hub", "hub", ("hub-api",), "http://127.0.0.1:{hub}/health"),
    ("kernel", "Eidolon Kernel", "kernel", ("kernel-api",), "http://127.0.0.1:{kernel}/health"),
    (
        "memory",
        "Eidolon Memory",
        "memory",
        ("memory-embedder", "memory-supervisor", "memory-discovery"),
        "http://127.0.0.1:{memory_admin}/api/admin/health",
    ),
    ("agent", "Eidolon Agent", "agent", ("agent",), "http://127.0.0.1:{agent_http}/readyz"),
    (
        "channel-provider",
        "Eidolon Channel Provider",
        "channel-provider",
        ("channel-provider",),
        "http://127.0.0.1:{channel_provider}/health",
    ),
    ("channel", "Eidolon Channel Worker", "channel", ("channel-worker",), None),
)
_EXTERNAL_SERVICES = (
    ("nats", "NATS (external)", "http://127.0.0.1:{nats_http}/varz"),
    ("livekit", "LiveKit (external)", "http://127.0.0.1:{livekit}/"),
    ("client-web", "Eidolon Client Web (external)", f"http://127.0.0.1:{CLIENT_WEB_PORT}/"),
)


def admin_services_yaml() -> str:
    # The browser origins Admin admits. Admin's own registry allow-lists the
    # web client too; this generated copy listed only the operator console, so
    # on a source run every request the web client made was refused by the
    # browser before Admin saw it. A component's own file is the reference for
    # what that component accepts, even where Ops states the topology.
    lines = [
        "admin:",
        "  host: 127.0.0.1",
        f"  port: {PORTS['admin']}",
        "  cors_origins:",
        f"    - http://127.0.0.1:{PORTS['admin_web']}",
        f"    - http://localhost:{PORTS['admin_web']}",
        f"    - http://127.0.0.1:{CLIENT_WEB_PORT}",
        f"    - http://localhost:{CLIENT_WEB_PORT}",
        "services:",
    ]
    for service_id, name, group, programs, health in _MANAGED_SERVICES:
        lines.extend(_service_header(service_id, name))
        if health is not None:
            lines.append(f"    health: {health.format(**PORTS)}")
        lines.extend(
            [
                "    supervisor:",
                f"      group: {group}",
                "      programs:",
                *(f"        - {program}" for program in programs),
                "    features: []",
            ]
        )
    for service_id, name, health in _EXTERNAL_SERVICES:
        lines.extend(_service_header(service_id, name))
        lines.append(f"    health: {health.format(**PORTS)}")
        lines.append("    features: []")
    return "\n".join(lines) + "\n"


def _service_header(service_id: str, name: str) -> list[str]:
    return [
        f"  - id: {service_id}",
        f"    name: {name}",
        "    integration: infra",
        "    base_url: ''",
        "    upstream_prefix: ''",
        "    auth: {type: none}",
    ]


_LIVEKIT_KEYS_BLOCK = re.compile(r"(?m)^keys:\n((?:[ \t].*(?:\n|$))*)")
_LIVEKIT_KEY_PAIR = re.compile(r"(?m)^\s{2}([A-Za-z0-9_-]+):\s*([A-Za-z0-9_-]+)\s*$")


def external_livekit_credentials(path: Path | None) -> dict[str, str]:
    """Adopt the credentials the running LiveKit already issued itself.

    A source run shares one LiveKit with whatever started it, so the key pair
    comes from that server's own generated config rather than being minted a
    second time — two pairs would mean Channel and Hub could not meet.
    """

    if path is None or not path.is_file() or path.is_symlink():
        raise OperationsError(f"external LiveKit config is missing or unsafe: {path}")
    block = _LIVEKIT_KEYS_BLOCK.search(path.read_text(encoding="utf-8"))
    matches = _LIVEKIT_KEY_PAIR.findall(block.group(1) if block else "")
    if len(matches) != 1:
        raise OperationsError("external LiveKit config must contain exactly one key pair")
    key, secret = matches[0]
    if not 3 <= len(key) <= 64 or not 8 <= len(secret) <= 128:
        raise OperationsError("external LiveKit credential shape is invalid")
    return {"LIVEKIT_API_KEY": key, "LIVEKIT_API_SECRET": secret}
