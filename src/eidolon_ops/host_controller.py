"""One lifecycle interface backed by host-specific process adapters."""

from __future__ import annotations

import os
from pathlib import Path

from eidolon_ops.config import load_config
from eidolon_ops.controller import EidolonPiController, OperationsError
from eidolon_ops.paths import HostProfile, merged_environment
from eidolon_ops.process import ProcessRunner, checked


class HostController:
    def __init__(
        self,
        profile: HostProfile,
        runner: ProcessRunner,
        *,
        revision_overrides: tuple[str, ...] = (),
    ) -> None:
        self.profile = profile
        self.runner = runner
        self.revision_overrides = revision_overrides

    def status(self) -> dict[str, object]:
        if self.profile.driver == "local-supervisord":
            return self._local_lifecycle("status")
        return self._pi().status()

    def app_ready(self) -> dict[str, object]:
        return self._require_pi("app-ready").app_ready()

    def provision(self, *, apply: bool) -> dict[str, object]:
        return self._require_pi("provision").provision(apply=apply)

    def install(
        self,
        *,
        release_id: str,
        resume: bool,
        apply: bool,
        reset_existing: bool = False,
        wipe_authority_data: bool = False,
    ) -> dict[str, object]:
        return self._require_pi("install").install(
            release_id=release_id,
            resume=resume,
            apply=apply,
            reset_existing=reset_existing,
            wipe_authority_data=wipe_authority_data,
        )

    def reset(self, *, wipe_authority_data: bool, apply: bool) -> dict[str, object]:
        return self._require_pi("reset").reset(
            wipe_authority_data=wipe_authority_data,
            apply=apply,
        )

    def deploy(self, *, release_id: str, resume: bool, activate: bool) -> dict[str, object]:
        return self._require_pi("deploy").deploy(
            release_id=release_id, resume=resume, activate=activate
        )

    def rollback(self, *, release_id: str, snapshot: Path, apply: bool) -> dict[str, object]:
        return self._require_pi("rollback").rollback(
            release_id=release_id, snapshot=snapshot, apply=apply
        )

    def diagnose(self, *, output: Path) -> dict[str, object]:
        return self._require_pi("diagnose").diagnose(output=output)

    def migrate_paths(self, *, apply: bool) -> dict[str, object]:
        from eidolon_ops.local_path_migration import LocalPathMigrator

        migrator = LocalPathMigrator(self.profile)
        return migrator.apply() if apply else migrator.plan()

    def doctor(self, *, release_id: str | None = None) -> dict[str, object]:
        paths = self.profile.paths
        path_items = (
            ("install", paths.install_root),
            ("current", paths.current_root),
            ("config", paths.config_root),
            ("state", paths.state_root),
            ("runtime", paths.runtime_root),
            ("logs", paths.log_root),
            ("cache", paths.cache_root),
            ("bootstrap_state", paths.bootstrap_state_root),
            ("bootstrap_runtime", paths.bootstrap_runtime_root),
        )
        if self.profile.driver == "local-supervisord":
            from eidolon_ops.local_path_migration import LocalPathMigrator

            path_report = {
                name: {"path": str(value), "exists": value.exists()} for name, value in path_items
            }
            script = self._local_script()
            migration = LocalPathMigrator(self.profile).plan()
            healthy = (
                paths.current_root.is_dir()
                and os.access(script, os.X_OK)
                and migration["status"] == "clean"
            )
            return {
                "status": "healthy" if healthy else "degraded",
                "host_id": self.profile.host_id,
                "platform": self.profile.platform,
                "driver": self.profile.driver,
                "paths": path_report,
                "lifecycle_script": str(script),
                "path_migration": migration,
            }
        remote = self._pi().doctor(release_id=release_id)
        return {
            "status": remote.get("status", "degraded"),
            "host_id": self.profile.host_id,
            "platform": self.profile.platform,
            "driver": self.profile.driver,
            "paths": {name: str(value) for name, value in path_items},
            "remote": remote,
        }

    def lifecycle(
        self,
        operation: str,
        *,
        dry_run: bool = False,
        force_cleanup: bool = False,
        strict: bool = False,
        wait_ready: bool = True,
    ) -> dict[str, object]:
        if operation not in {"start", "stop", "restart"}:
            raise OperationsError(f"unsupported lifecycle operation: {operation}")
        if self.profile.driver == "local-supervisord":
            from eidolon_ops.local_path_migration import LocalPathMigrator

            arguments = [operation]
            if force_cleanup:
                arguments.append("--force-cleanup")
            if strict:
                arguments.append("--strict")
            if not wait_ready:
                arguments.append("--no-wait-ready")
            if dry_run:
                return {
                    "status": "dry_run",
                    "driver": self.profile.driver,
                    "command": [str(self._local_script()), *arguments],
                    "environment": self.profile.environment(),
                }
            if operation in {"start", "restart"}:
                LocalPathMigrator(self.profile).require_clean()
            return self._local_lifecycle(tuple(arguments))
        if force_cleanup or strict or not wait_ready:
            raise OperationsError("Mac lifecycle flags are not valid for the systemd adapter")
        return self._pi().lifecycle(operation, dry_run=dry_run)

    def local_profile(
        self, profile_name: str, operation: str, *, arguments: tuple[str, ...] = ()
    ) -> dict[str, object]:
        if self.profile.driver != "local-supervisord":
            raise OperationsError("Supervisor profiles are available on the macOS adapter only")
        if profile_name not in {"core-contract", "os-control-plane"}:
            raise OperationsError(f"unsupported local profile: {profile_name}")
        script = self._local_script()
        result = checked(
            f"local {profile_name} {operation}",
            self.runner.run(
                (str(script), profile_name, operation, *arguments),
                cwd=script.parents[2],
                env=merged_environment(self.profile),
                timeout=300,
            ),
        )
        return {
            "status": "ok",
            "host_id": self.profile.host_id,
            "profile": profile_name,
            "operation": operation,
            "output": result.stdout.strip(),
        }

    def logs(self, *, service: str | None, lines: int, since: str | None) -> dict[str, object]:
        if self.profile.driver == "ssh-systemd":
            unit = service
            if unit is not None and not unit.endswith(".service"):
                if not unit.startswith("eidolon-") and unit != "eidolond":
                    unit = f"eidolon-{unit}"
                unit = f"{unit}.service"
            return self._pi().logs(unit=unit, lines=lines, since=since)
        if since is not None:
            raise OperationsError("--since is supported by the systemd adapter only")
        if lines < 1 or lines > 5000:
            raise OperationsError("lines must be between 1 and 5000")
        root = self.profile.paths.log_root
        target = root / service if service else root
        if root not in target.resolve(strict=False).parents and target != root:
            raise OperationsError("service log selector escapes the log root")
        files = [target] if target.is_file() else sorted(target.rglob("*.log"))
        output: dict[str, list[str]] = {}
        for path in files:
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
            output[str(path.relative_to(root))] = content[-lines:]
        return {"status": "ok", "host_id": self.profile.host_id, "logs": output}

    def _local_lifecycle(self, arguments: tuple[str, ...] | str) -> dict[str, object]:
        if isinstance(arguments, str):
            arguments = (arguments,)
        script = self._local_script()
        result = checked(
            f"local host {arguments[0]}",
            self.runner.run(
                (str(script), *arguments),
                cwd=script.parents[2],
                env=merged_environment(self.profile),
                timeout=300,
            ),
        )
        return {
            "status": "ok",
            "host_id": self.profile.host_id,
            "platform": self.profile.platform,
            "driver": self.profile.driver,
            "output": result.stdout.strip(),
        }

    def _local_script(self) -> Path:
        script = self.profile.lifecycle_script
        if script is None or not script.is_file() or not os.access(script, os.X_OK):
            raise OperationsError(f"local lifecycle script is missing or not executable: {script}")
        return script

    def _pi(self) -> EidolonPiController:
        config_path = self.profile.operations_config
        if config_path is None:
            raise OperationsError("Pi host profile does not reference an operations config")
        config = load_config(config_path).with_revision_overrides(self.revision_overrides)
        return EidolonPiController(config, self.runner)

    def _require_pi(self, operation: str) -> EidolonPiController:
        if self.profile.driver != "ssh-systemd":
            raise OperationsError(f"{operation} is available on the Pi adapter only")
        return self._pi()
