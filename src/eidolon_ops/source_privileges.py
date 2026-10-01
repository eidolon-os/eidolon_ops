"""Darwin privilege boundary: fixed Host initialization and supervisor launch.

The operator prepares sources, dependencies and migrations. This helper never
installs packages or renders inputs, and accepts no arbitrary command.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shlex
import shutil
import sys
from pathlib import Path

from eidolon_ops.component_contract import load_component_contract
from eidolon_ops.config import load_config
from eidolon_ops.errors import OperationsError
from eidolon_ops.paths import HostDriver, HostProfile, load_host_profile, merged_environment
from eidolon_ops.process import ProcessRunner, SubprocessRunner, checked
from eidolon_ops.source_runtime import (
    provision_source_files,
    provision_source_identities,
    require_service_identities,
    require_service_roots,
    shutdown_timeout,
    source_services,
)


def privileged_action(profile: HostProfile, action: str, runner: ProcessRunner):
    if action not in {"provision", "start"}:
        raise OperationsError("unsupported source privilege action")
    values = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "EIDOLON_NATS_SERVER": os.environ.get("EIDOLON_NATS_SERVER") or shutil.which("nats-server") or "",
        "EIDOLON_LIVEKIT_BIN": os.environ.get("EIDOLON_LIVEKIT_BIN") or shutil.which("livekit-server") or "",
    }
    command = (
        "/usr/bin/env", *(f"{key}={value}" for key, value in values.items()),
        sys.executable, "-m", "eidolon_ops.source_privileges", str(profile.path), action,
    )
    script = f"do shell script {json.dumps(shlex.join(command), ensure_ascii=False)} with administrator privileges"
    return checked(
        f"source Host {action}",
        runner.run(("/usr/bin/osascript", "-e", script),
                   timeout=300 + (shutdown_timeout(profile) if action == "provision" else 0)),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("action", choices=("provision", "start"))
    arguments = parser.parse_args()
    if os.geteuid() != 0 or sys.platform != "darwin":
        raise OperationsError("source privilege helper requires the Darwin administrator")
    profile = load_host_profile(arguments.profile)
    if profile.driver is not HostDriver.LOCAL_SUPERVISORD or profile.operations_config is None:
        raise OperationsError("source privilege helper requires a local source Host")
    operator = pwd.getpwuid(profile.path.stat().st_uid)
    if operator.pw_uid == 0:
        raise OperationsError("source workspace operator cannot be root")
    config = load_config(profile.operations_config).with_source_overrides(profile.source_overrides)
    services = []
    for component, source in config.sources.items():
        declaration = load_component_contract(source.path, component)
        if declaration is None:
            raise OperationsError(f"source component has no operational contract: {component}")
        services.extend(source_services(declaration, profile, source.path))
    services = tuple(services)
    environment = merged_environment(profile)
    stop_seconds = shutdown_timeout(profile)
    environment["EIDOLON_SUPERVISOR_SHUTDOWN_SECONDS"] = str(stop_seconds)
    environment.update(HOME=operator.pw_dir, USER=operator.pw_name, LOGNAME=operator.pw_name)
    runner = SubprocessRunner()
    if arguments.action == "provision":
        # Stop through the existing supervisor before any ownership transfer.
        checked("quiesce source Host", runner.run(
            (str(profile.lifecycle_script), "product-source", "stop"),
            env=environment, timeout=stop_seconds + 120,
        ))
        provision_source_identities(services, operator.pw_uid)
    else:
        require_service_identities(services, operator.pw_uid)
    if arguments.action == "provision":
        provision_source_files(services, profile, operator.pw_name, initialize=True)
    else:
        require_service_roots(services)
    if arguments.action == "start":
        result = checked("launch source supervisor", runner.run(
            (str(profile.lifecycle_script), "product-source", "start"),
            env=environment, timeout=120,
        ))
        print(result.stdout)
    else:
        print("source service identities and declared private roots initialized")


if __name__ == "__main__":
    main()
