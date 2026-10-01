"""Render source-host bindings from components' existing operational contracts.

Service assets remain component-owned. The source adapter translates their
process, environment and identity declarations; it does not define a second
control plane. Components that opt into source bindings must bind every unit.
"""

from __future__ import annotations

import configparser
import io
import os
import pwd
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

from eidolon_ops import source_assets
from eidolon_ops.component_contract import ComponentContract
from eidolon_ops.errors import OperationsError
from eidolon_ops.hostagent.contract import HOST_ENV_PATH, HOST_ENV_VALUE
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
            "PYTHONUNBUFFERED": "1", "HOME": "/var/empty",
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


def provision_source_identities(
    services: tuple[SourceService, ...], operator_uid: int | None = None
) -> None:
    """Darwin's account adapter for the same component-owned identity contract.

    Explicit initialization calls this after stopping the Host.
    Linux already provisions these identities through hostagent.identities.
    """
    import grp
    import subprocess
    import sys

    if not services:
        return
    if os.geteuid() != 0:
        raise OperationsError(
            "source identity initialization requires administrator privileges through the helper; run provision --apply as the workspace operator"
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
            if grp.getgrnam(name).gr_gid not in os.getgrouplist(service.user, account.pw_gid):
                run("/usr/sbin/dseditgroup", "-o", "edit", "-a", service.user, "-t", "user", name)
    if operator_uid is not None:
        operator = pwd.getpwuid(operator_uid)
        for group in {s.primary_group for s in services if s.program == "lifecycle-workflow"}:
            if grp.getgrnam(group).gr_gid not in os.getgrouplist(operator.pw_name, operator.pw_gid):
                run("/usr/sbin/dseditgroup", "-o", "edit", "-a", operator.pw_name, "-t", "user", group)
    require_service_identities(services, operator_uid)


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


def require_service_roots(services: tuple[SourceService, ...]) -> None:
    import grp
    for service in services:
        uid = pwd.getpwnam(service.user).pw_uid
        gid = grp.getgrnam(service.primary_group).gr_gid
        for path, mode in service_directories(service):
            if (not path.is_dir() or path.is_symlink() or
                (path.stat().st_uid, path.stat().st_gid, stat.S_IMODE(path.stat().st_mode))
                != (uid, gid, mode)):
                raise OperationsError(f"source service root needs explicit provision --apply: {path}")


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


def provision_source_files(
    services: tuple[SourceService, ...], profile: HostProfile, operator: str,
    *, initialize: bool = False,
) -> None:
    """Initialize declared private roots, or authorize freshly rendered inputs.

    Recursive ownership transfer belongs only to explicit provisioning while
    the Host is stopped. Normal starts validate those roots and grant access
    only to this generation of component inputs. No parent-wide socket ACLs.
    """
    import grp
    import re
    import subprocess

    if initialize and os.geteuid() != 0:
        raise OperationsError("source filesystem provisioning requires administrator privileges")
    operator_account = pwd.getpwnam(operator)
    managed = (
        profile.paths.config_root, profile.paths.state_root, profile.paths.runtime_root,
        profile.paths.bootstrap_state_root, profile.paths.bootstrap_runtime_root,
    )

    def checked_path(path: Path, roots=managed):
        if not any(path == root or path.is_relative_to(root) for root in roots):
            raise OperationsError(f"source permission target is outside declared Host roots: {path}")
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise OperationsError(f"source permission target contains a symlink: {path}")

    principal_ids: dict[str, set[str]] = {}

    def grant(path: Path, user: str, rights: str):
        if user not in principal_ids:
            result = subprocess.run(
                ("/usr/bin/dsmemberutil", "getuuid", "-U", user),
                check=True, capture_output=True, text=True,
            )
            principal_ids[user] = {user, getattr(result, "stdout", "").strip()}
        existing = subprocess.run(
            ("/bin/ls", "-lde", str(path)), check=True, capture_output=True, text=True,
        )
        required = set(rights.split(","))
        for line in getattr(existing, "stdout", "").splitlines():
            match = re.search(r"\d+: (?:user:)?(\S+) allow (.+)$", line.strip())
            if match and match[1] in principal_ids[user] and required <= set(match[2].split(",")):
                return
        subprocess.run(
            ("/bin/chmod", "+a", f"user:{user} allow {rights}", str(path)),
            check=True, capture_output=True, text=True,
        )

    def traverse(path: Path, user: str):
        account = pwd.getpwnam(user)
        groups = set(os.getgrouplist(user, account.pw_gid))
        for parent in reversed(path.parents):
            details = parent.stat()
            if (details.st_mode & stat.S_IXOTH
                or (details.st_uid == account.pw_uid and details.st_mode & stat.S_IXUSR)
                or (details.st_gid in groups and details.st_mode & stat.S_IXGRP)):
                continue
            # Private system directories are never changed to accommodate a source run.
            if details.st_uid != operator_account.pw_uid:
                raise OperationsError(f"source service cannot traverse a non-operator directory: {parent}")
            grant(parent, user, "search")

    plans = []
    for service in services:
        account = pwd.getpwnam(service.user)
        gid = grp.getgrnam(service.primary_group).gr_gid
        asset = service.directory / "deploy/systemd" / f"{service.unit_id}.service"
        values = service_values(asset.read_text())
        directories = service_directories(service)
        files = {
            profile.paths.config_root / "env" / Path(entry.removeprefix("-")).name
            for entry in values.get("EnvironmentFile", [])
            if Path(entry.removeprefix("-")).name != "host.env"
        }
        from eidolon_ops import environment
        inputs = dict(service.environment)
        for path in files:
            checked_path(path)
            inputs.update(environment.parse(path.read_text(), label="source input", key=environment.SERVICE_KEY))
        files.update(Path(value) for value in inputs.values()
                     if value.startswith("/") and Path(value).is_file())
        for path, mode in directories:
            checked_path(path)
            if path in (profile.paths.state_root, profile.paths.runtime_root, profile.paths.config_root):
                raise OperationsError(f"service cannot own a shared Host root: {path}")
            if initialize:
                for child in path.rglob("*"):
                    checked_path(child)
            elif not path.is_dir() or (path.stat().st_uid, path.stat().st_gid,
                stat.S_IMODE(path.stat().st_mode)) != (account.pw_uid, gid, mode):
                raise OperationsError(f"source service root needs explicit provision --apply: {path}")
        for path in files:
            checked_path(path)
        plans.append((service, account, gid, directories, files))

    for service, account, gid, directories, files in plans:
        if initialize:
            for path, mode in directories:
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(mode)
                # Existing data is transferred once; future files inherit maintenance access.
                for child in (path, *path.rglob("*")):
                    checked_path(child)
                    details = child.stat()
                    if (details.st_uid, details.st_gid) != (account.pw_uid, gid):
                        os.chown(child, account.pw_uid, gid)
                    grant(child, operator,
                          "read,write,append,readattr,writeattr,readextattr,writeextattr,readsecurity,delete"
                          if not child.is_dir() else
                          "list,search,add_file,add_subdirectory,delete_child,readattr,writeattr,readsecurity,file_inherit,directory_inherit")
                traverse(path, service.user)
        traverse(service.directory, service.user)
        for path in files:
            traverse(path, service.user)
            details = path.stat()
            groups = set(os.getgrouplist(service.user, account.pw_gid))
            if not (details.st_mode & stat.S_IROTH
                    or (details.st_uid == account.pw_uid and details.st_mode & stat.S_IRUSR)
                    or (details.st_gid in groups and details.st_mode & stat.S_IRGRP)):
                grant(path, service.user, "read,readattr,readextattr,readsecurity")
