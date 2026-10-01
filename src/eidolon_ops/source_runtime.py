"""Render source-host bindings from components' existing operational contracts.

Service assets remain component-owned. The source adapter translates their
process, environment and identity declarations; it does not define a second
control plane. Components that opt into source bindings must bind every unit.
"""

from __future__ import annotations

import configparser
import os
import pwd
import shlex
from dataclasses import dataclass
from pathlib import Path

from eidolon_ops import source_assets
from eidolon_ops.component_contract import ComponentContract
from eidolon_ops.errors import OperationsError
from eidolon_ops.paths import HostProfile


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
        for entry in values.get("Environment", []):
            for assignment in shlex.split(entry):
                name, value = assignment.split("=", 1)
                environment[name] = translate(value)
        environment.update({k: translate(v) for k, v in binding.get("environment", {}).items()})
        files = []
        for entry in values.get("EnvironmentFile", []):
            path = entry.removeprefix("-")
            if path == "/etc/eidolon/host.env":
                continue  # HostPaths.environment is already exported by Ops.
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
                user=user,
                requires=tuple(unit.get("requires", ())),
                command=command,
                directory=root,
                environment=environment,
                auxiliary_programs=tuple(binding.get("auxiliary_programs", [])),
                state_paths=paths("StateDirectory", "/var/lib"),
                runtime_paths=paths("RuntimeDirectory", "/run"),
                primary_group=values.get("Group", [user])[-1],
            )
        )
    return tuple(result)


def render_supervisor(template: str, services: tuple[SourceService, ...], operator: str) -> str:
    """Combine driver-specific workers with contract-derived control services."""
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(template)
    for section in parser.sections():
        if section.startswith("program:"):
            parser[section]["user"] = operator
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
    blocks = [template]
    # Preserve comments in the platform adapter; add user explicitly for root supervision.
    for section in parser.sections():
        if section.startswith("program:"):
            template = template.replace(f"[{section}]", f"[{section}]\nuser={operator}", 1)
    blocks = [template]
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
        env = {"PYTHONUNBUFFERED": "1", **service.environment}
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
stopwaitsecs=20
stdout_logfile=%(ENV_EIDOLON_LOG_ROOT)s/admin/{service.program}.log
stderr_logfile=%(ENV_EIDOLON_LOG_ROOT)s/admin/{service.program}.err.log
environment={assignments}

[group:{service.group}]
priority={priority(service.unit_id)}
programs={",".join((service.program, *service.auxiliary_programs))}
""")
    return "\n".join(blocks)


def require_service_identities(
    services: tuple[SourceService, ...], operator_uid: int | None = None
) -> None:
    """Do not silently collapse isolated workload identities to the operator."""
    uids = {}
    for service in services:
        try:
            uid = pwd.getpwnam(service.user).pw_uid
        except KeyError as exc:
            raise OperationsError(
                f"source Host service account is missing: {service.user}; provision the component's isolated identities before starting"
            ) from exc
        if (
            uid == (os.getuid() if operator_uid is None else operator_uid)
            or uid == 0
            or uid in uids
        ):
            raise OperationsError(f"source Host service identity is not isolated: {service.user}")
        uids[uid] = service.user
    if services and os.geteuid() != 0:
        raise OperationsError(
            "the source Host supervisor must be started by the privileged host adapter to launch isolated service accounts"
        )


def provision_source_identities(
    services: tuple[SourceService, ...], operator_uid: int | None = None
) -> None:
    """Darwin's account adapter for the same component-owned identity contract.

    The normal lifecycle entrypoint calls this before changing a running Host.
    Linux already provisions these identities through hostagent.identities.
    """
    import grp
    import subprocess
    import sys

    if not services:
        return
    if os.geteuid() != 0:
        raise OperationsError(
            "isolated source-host services require administrator privileges; run the normal Host lifecycle through sudo"
        )
    if sys.platform != "darwin":
        raise OperationsError("source identity provisioning requires the Darwin host adapter")

    def run(*args):
        subprocess.run(args, check=True, capture_output=True, text=True)

    for service in services:
        groups = {service.user, service.primary_group}
        values = service_values(
            (service.directory / "deploy/systemd" / f"{service.unit_id}.service").read_text()
        )
        groups.update(
            name for entry in values.get("SupplementaryGroups", ()) for name in entry.split()
        )
        for name in sorted(groups):
            try:
                grp.getgrnam(name)
            except KeyError:
                used = {g.gr_gid for g in grp.getgrall()}
                gid = next(n for n in range(1000, 60000) if n not in used)
                run("/usr/bin/dscl", ".", "-create", f"/Groups/{name}")
                run("/usr/bin/dscl", ".", "-create", f"/Groups/{name}", "PrimaryGroupID", str(gid))
        try:
            pwd.getpwnam(service.user)
        except KeyError:
            used = {u.pw_uid for u in pwd.getpwall()}
            uid = next(n for n in range(1000, 60000) if n not in used)
            record = f"/Users/{service.user}"
            run("/usr/bin/dscl", ".", "-create", record)
            for key, value in {
                "UniqueID": str(uid),
                "PrimaryGroupID": str(grp.getgrnam(service.primary_group).gr_gid),
                "UserShell": "/usr/bin/false",
                "NFSHomeDirectory": "/var/empty",
                "IsHidden": "1",
                "Password": "*",
            }.items():
                run("/usr/bin/dscl", ".", "-create", record, key, value)
        account = pwd.getpwnam(service.user)
        if account.pw_gid != grp.getgrnam(service.primary_group).gr_gid:
            raise OperationsError(
                f"existing source service account has unexpected primary group: {service.user}"
            )
        for name in sorted(groups):
            run("/usr/sbin/dseditgroup", "-o", "edit", "-a", service.user, "-t", "user", name)
    require_service_identities(services, operator_uid)


def provision_source_files(
    services: tuple[SourceService, ...], profile: HostProfile, operator: str
) -> None:
    """Materialize the service asset's filesystem ownership and narrow read ACLs.

    The trusted Host operator retains maintenance access. Network workloads
    receive only their own state and declared input files; a traversal ACL
    does not let a workload list/read another workload's private directories.
    """
    import grp
    import subprocess

    if os.geteuid() != 0:
        raise OperationsError(
            "source service filesystem provisioning requires administrator privileges"
        )
    operator_account = pwd.getpwnam(operator)
    # Elevating the adapter changes no ownership role of ordinary source
    # workers. Generated inputs remain maintainable by the trusted operator;
    # isolated workloads below receive their own state and narrow read ACLs.
    for path in (profile.paths.config_root, *profile.paths.config_root.rglob("*")):
        if path.is_symlink():
            raise OperationsError(f"unsafe source configuration path: {path}")
        os.chown(path, operator_account.pw_uid, operator_account.pw_gid)
    for path in (
        profile.paths.state_root,
        profile.paths.runtime_root,
        profile.paths.log_root,
        profile.paths.cache_root,
    ):
        os.chown(path, operator_account.pw_uid, operator_account.pw_gid)
    authority_input = profile.paths.state_root / "hub/authority-bootstrap.json"
    if authority_input.exists():
        os.chown(authority_input, operator_account.pw_uid, operator_account.pw_gid)

    def grant(path: Path, user: str, rights: str):
        subprocess.run(
            ("/bin/chmod", "+a", f"user:{user} allow {rights}", str(path)),
            check=True,
            capture_output=True,
        )

    def traverse(path: Path, user: str):
        for parent in reversed(path.parents):
            if parent != Path("/"):
                grant(parent, user, "search")

    for service in services:
        account = pwd.getpwnam(service.user)
        gid = grp.getgrnam(service.primary_group).gr_gid
        asset_values = service_values(
            (service.directory / "deploy/systemd" / f"{service.unit_id}.service").read_text()
        )
        directories = (
            *(
                (path, int(asset_values.get("StateDirectoryMode", ["0755"])[-1], 8))
                for path in service.state_paths
            ),
            *(
                (path, int(asset_values.get("RuntimeDirectoryMode", ["0755"])[-1], 8))
                for path in service.runtime_paths
            ),
        )
        for path, mode in directories:
            if path.is_symlink():
                raise OperationsError(f"unsafe source service directory: {path}")
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(mode)
            traverse(path, service.user)
            for child in (path, *path.rglob("*")):
                if child.is_symlink():
                    raise OperationsError(f"unsafe source service state: {child}")
                os.chown(child, account.pw_uid, gid)
                grant(
                    child,
                    operator,
                    "read,write,append,readattr,writeattr,readextattr,writeextattr,readsecurity,delete"
                    if not child.is_dir()
                    else "list,search,add_file,add_subdirectory,delete_child,readattr,writeattr,readsecurity,file_inherit,directory_inherit",
                )
        traverse(service.directory, service.user)
        # The source code is public to the service; no blanket access to its private inputs.
        files = {
            profile.paths.config_root / "env" / Path(entry.removeprefix("-")).name
            for entry in service_values(
                (service.directory / "deploy/systemd" / f"{service.unit_id}.service").read_text()
            ).get("EnvironmentFile", [])
            if Path(entry.removeprefix("-")).name != "host.env"
        }
        from eidolon_ops import environment

        values = dict(service.environment)
        for path in files:
            values.update(
                environment.parse(
                    path.read_text(), label="source service input", key=environment.SERVICE_KEY
                )
            )
        files.update(
            Path(value)
            for value in values.values()
            if value.startswith("/") and Path(value).is_file()
        )
        for path in files:
            traverse(path, service.user)
            grant(path, service.user, "read,readattr,readextattr,readsecurity")
        # Directory clients connect to the source system manager's native socket.
        for name, value in values.items():
            if name.endswith("SYSTEM_DIRECTORY_UDS") and value:
                path = Path(value)
                traverse(path, service.user)
                grant(path.parent, service.user, "read,write,file_inherit,only_inherit")
                if path.exists():
                    grant(path, service.user, "read,write")
    # Bootstrap's group-scoped TLS file is also the Local API's TLS identity.
    # SupplementaryGroups in the shared asset supplies exactly this read access.
