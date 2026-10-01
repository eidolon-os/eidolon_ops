"""Render source-host bindings from components' existing operational contracts.

Service assets remain component-owned. The source adapter translates their
process, environment and identity declarations; it does not define a second
control plane. Components that opt into source bindings must bind every unit.
"""

from __future__ import annotations

import configparser
import grp
import io
import os
import pwd
import shlex
from dataclasses import dataclass
from pathlib import Path

from eidolon_ops import source_assets
from eidolon_ops.component_contract import ComponentContract
from eidolon_ops.errors import OperationsError
from eidolon_ops.hostagent.contract import HOST_ENV_PATH, HOST_ENV_VALUE
from eidolon_ops.paths import HostPlatform, HostProfile


@dataclass(frozen=True)
class SourceService:
    unit_id: str
    program: str
    group: str
    user: str
    requires: tuple[str, ...]
    command: str
    directory: Path
    environment: dict[str, str]
    auxiliary_programs: tuple[str, ...]
    state_paths: tuple[Path, ...]
    runtime_paths: tuple[Path, ...]
    primary_group: str
    declared_user: str


def source_operator(profile: HostProfile):
    """A source checkout has the operator's UID as its local trust boundary."""
    uid = profile.path.stat().st_uid if profile.path.exists() else os.getuid()
    if uid == 0 or os.geteuid() != uid:
        raise OperationsError("source Host must run as its non-root workspace owner")
    return pwd.getpwuid(uid)


def service_values(text: str) -> dict[str, list[str]]:
    """Read repeated systemd directives without discarding Environment entries."""
    section = ""
    result: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line
        elif section == "[Service]" and "=" in line:
            name, value = line.split("=", 1)
            result.setdefault(name, []).append(value)
    return result


def source_services(
    contract: ComponentContract, profile: HostProfile, root: Path
) -> tuple[SourceService, ...]:
    if not any("supervisord" in unit for unit in contract.units):
        return ()
    operator = source_operator(profile)
    declared_accounts = {unit["user"] for unit in contract.units}
    result = []
    for unit in contract.units:
        binding = unit.get("supervisord")
        if binding is None:
            raise OperationsError(
                f"{contract.component_id}:{unit['id']} has no source-host execution binding"
            )
        asset = root / "deploy/systemd" / f"{unit['id']}.service"
        values = service_values(asset.read_text())

        def one(name: str, values=values, asset=asset) -> str:
            entries = values.get(name, [])
            if len(entries) != 1:
                raise OperationsError(f"{asset}: exactly one {name} is required")
            return entries[0]

        user = one("User")
        if user != unit["user"]:
            raise OperationsError(f"{asset}: service identity disagrees with component contract")

        def translate(value: str) -> str:
            if value in declared_accounts:
                return operator.pw_name
            value = source_assets.translate_fhs(profile, value)
            # Components run out of their selected worktrees, including overrides.
            value = value.replace(
                str(profile.paths.current_root / contract.component_id), str(root)
            )
            # Source settings and input environments have separate directories.
            for name in ("ports.yaml", "services.yaml"):
                if Path(value).name == name:
                    value = str(profile.paths.config_root / "settings" / name)
            return value

        environment: dict[str, str] = {}
        if str(HOST_ENV_PATH) in values.get("EnvironmentFile", []):
            # Translate the same shared Host inputs the systemd unit consumes;
            # inherited launcher defaults cannot replace a declared input.
            for assignment in HOST_ENV_VALUE.splitlines():
                name, value = assignment.split("=", 1)
                environment[name] = translate(value)
            environment.update(profile.environment())
        for entry in values.get("Environment", []):
            for assignment in shlex.split(entry):
                name, value = assignment.split("=", 1)
                environment[name] = translate(value)
        environment.update({k: translate(v) for k, v in binding.get("environment", {}).items()})
        files = []
        for entry in values.get("EnvironmentFile", []):
            path = entry.removeprefix("-")
            if path == str(HOST_ENV_PATH):
                continue  # Translated into this service's explicit environment above.
            mapped = translate(path)
            if path.startswith("/etc/eidolon/") and path.endswith(".env"):
                mapped = str(profile.paths.config_root / "env" / Path(path).name)
            files.append(mapped)
        if len(files) > 1:
            raise OperationsError(
                f"{asset}: source launcher does not support multiple environment files"
            )
        command = translate(one("ExecStart"))
        if files:
            wrapper = "%(ENV_EIDOLON_OPS_ROOT)s/deploy/supervisor/wrappers/with-env.sh"
            command = f"{wrapper} {shlex.quote(str(root))} {shlex.quote(files[0])} -- {command}"

        def paths(key, base, values=values):
            return tuple(
                Path(translate(str(Path(base) / name)))
                for value in values.get(key, [])
                for name in value.split()
            )

        result.append(
            SourceService(
                unit_id=unit["id"],
                program=binding["program"],
                group=binding["group"],
                user=operator.pw_name,
                requires=tuple(unit.get("requires", ())),
                command=command,
                directory=root,
                environment=environment,
                auxiliary_programs=tuple(binding.get("auxiliary_programs", [])),
                state_paths=paths("StateDirectory", "/var/lib"),
                runtime_paths=paths("RuntimeDirectory", "/run"),
                primary_group=grp.getgrgid(operator.pw_gid).gr_name,
                declared_user=user,
            )
        )
    return tuple(result)


def render_supervisor(
    template: str, services: tuple[SourceService, ...], operator: str,
    *, platform: HostPlatform = HostPlatform.MACOS,
    contracts: tuple[ComponentContract, ...] | None = None,
) -> str:
    """Combine driver-specific workers with contract-derived control services."""
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(template)
    selected = {
        unit["id"]: unit for contract in contracts or () for unit in contract.units
    }
    bound_units = set()
    for section in list(parser.sections()):
        if not section.startswith("source-program:"):
            continue
        program = section.removeprefix("source-program:")
        unit = parser[section]["unit"]
        group = parser[section]["group"]
        if f"program:{program}" not in parser or f"group:{group}" not in parser:
            raise OperationsError(f"source template has an incomplete unit binding: {unit}")
        bound_units.add(unit)
        if contracts is not None and unit not in selected:
            parser.remove_section(f"program:{program}")
            parser.remove_section(f"group:{group}")
        parser.remove_section(section)
    for unit in selected.values():
        if unit.get("requires_capability") and not unit.get("supervisord") and unit["id"] not in bound_units:
            raise OperationsError(f"source Host lacks an execution binding for selected unit: {unit['id']}")
    if "program:local-api-mdns" in parser:
        parser["program:local-api-mdns"]["command"] = (
            '/usr/bin/dns-sd -R "Eidolon Local API" _eidolon-local-api._tcp local 9002 contract=1 scheme=https'
            if platform is HostPlatform.MACOS else
            'avahi-publish-service "Eidolon Local API" _eidolon-local-api._tcp 9002 contract=1 scheme=https'
        )
    try:
        operator_home = pwd.getpwnam(operator).pw_dir
    except KeyError:
        operator_home = "/var/empty"
    for section in parser.sections():
        if section.startswith("program:"):
            parser[section]["user"] = operator
            parser[section]["stopasgroup"] = "true"
            parser[section]["killasgroup"] = "true"
            existing = parser[section].get("environment", "")
            parser[section]["environment"] = (
                f'HOME="{operator_home}",USER="{operator}",LOGNAME="{operator}"'
                + ("," + existing if existing else "")
            )
    programs = {
        section.removeprefix("program:")
        for section in parser.sections()
        if section.startswith("program:")
    }
    groups = {
        section.removeprefix("group:")
        for section in parser.sections()
        if section.startswith("group:")
    }
    rendered = io.StringIO()
    parser.write(rendered)
    blocks = [rendered.getvalue()]
    nodes = {service.unit_id: service for service in services}
    priority_cache = {}
    active = set()

    def priority(unit_id):
        if unit_id in priority_cache:
            return priority_cache[unit_id]
        if unit_id in active:
            raise OperationsError(f"source service dependency cycle: {unit_id}")
        if unit_id not in nodes:
            group = unit_id.removeprefix("eidolon-")
            section = f"group:{group}"
            if section not in parser:
                raise OperationsError(f"no source-host binding for required dependency: {unit_id}")
            return parser[section].getint("priority", fallback=999)
        active.add(unit_id)
        result = 1 + max((priority(dep) for dep in nodes[unit_id].requires), default=0)
        active.remove(unit_id)
        priority_cache[unit_id] = result
        return result

    for service in services:
        if service.program in programs or service.group in groups:
            raise OperationsError(f"duplicate source runtime binding: {service.unit_id}")
        if not set(service.auxiliary_programs) <= programs:
            raise OperationsError(f"missing source companion process for {service.unit_id}")
        programs.add(service.program)
        groups.add(service.group)
        env = {
            "PYTHONUNBUFFERED": "1", "HOME": operator_home,
            "USER": service.user, "LOGNAME": service.user, **service.environment,
        }
        assignments = ",".join(
            f'{k}="{v.replace(chr(37), chr(37) * 2)}"' for k, v in sorted(env.items())
        )
        blocks.append(f"""[program:{service.program}]
command={service.command}
directory={service.directory}
user={service.user}
autostart=true
autorestart=true
startsecs=2
stopsignal=TERM
stopasgroup=true
killasgroup=true
stopwaitsecs=20
stdout_logfile=%(ENV_EIDOLON_LOG_ROOT)s/admin/{service.program}.log
stderr_logfile=%(ENV_EIDOLON_LOG_ROOT)s/admin/{service.program}.err.log
environment={assignments}

[group:{service.group}]
priority={priority(service.unit_id)}
programs={",".join((service.program, *service.auxiliary_programs))}
""")
    return "\n".join(blocks)



def service_directories(service: SourceService):
    values = service_values(
        (service.directory / "deploy/systemd" / f"{service.unit_id}.service").read_text()
    )
    return (
        *((path, int(values.get("StateDirectoryMode", ["0755"])[-1], 8))
          for path in service.state_paths),
        *((path, int(values.get("RuntimeDirectoryMode", ["0755"])[-1], 8))
          for path in service.runtime_paths),
    )

def shutdown_timeout(profile: HostProfile) -> int:
    """Allow Supervisor's ordered group shutdown to honor every stop timeout."""
    path = profile.paths.config_root / "supervisor.conf"
    if not path.is_file():
        return 300
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    return 30 + sum(
        parser[section].getint("stopwaitsecs", fallback=10) + 5
        for section in parser.sections() if section.startswith("program:")
    )
