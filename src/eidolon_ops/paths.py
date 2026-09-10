"""Host-neutral filesystem contract for Eidolon operations.

The names in this module describe lifecycle and durability, not one operating
system's directory convention.  A host profile maps the same roles to macOS or
Linux paths.  Components consume the exported environment variables and never
derive product state from ``HOME``.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from eidolon_ops.config import SOURCE_IDS, SourceConfig


class HostProfileError(ValueError):
    """A host profile is incomplete, ambiguous, or unsafe."""


class HostPlatform(StrEnum):
    """The machine a Host profile describes."""

    MACOS = "macos"
    RASPBERRY_PI = "raspberry-pi"
    RK3588 = "rk3588"


class HostDriver(StrEnum):
    """How Ops reaches a Host and drives its services.

    A closed enumeration rather than a string, because this value used to be
    compared literally in nine places to decide what an operation was allowed
    to do. What a Host can do is now derived from the adapter this selects.
    """

    LOCAL_SUPERVISORD = "local-supervisord"
    SSH_SYSTEMD = "ssh-systemd"


_HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: Which driver each platform is reachable through. One entry per real Host;
#: a pair that is not in this table is not a Host this tool has an adapter for.
_PLATFORM_DRIVERS = {
    HostPlatform.MACOS: HostDriver.LOCAL_SUPERVISORD,
    HostPlatform.RASPBERRY_PI: HostDriver.SSH_SYSTEMD,
    HostPlatform.RK3588: HostDriver.SSH_SYSTEMD,
}


def is_product_board(platform: HostPlatform) -> bool:
    """Whether this is a box Ops installs onto, rather than an operator's own.

    Read off the driver table rather than listed again: a platform Ops reaches
    over SSH and drives with systemd is a product board, and that is the same
    fact the table already states. The two path rules below used to name the
    Raspberry Pi, which made every one of them a place a second board would
    have to be remembered.
    """

    return _PLATFORM_DRIVERS[platform] is HostDriver.SSH_SYSTEMD


_FOUNDATION_MODES = {"external"}
_IPV4_LITERAL = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
#: The product Host's layout, stated once. A Pi profile is required to be
#: exactly this (see :func:`_require_paths`), and a workstation profile is the
#: same set of roles at different locations — which is why a template written
#: for the product can be pointed at a source run by substituting these
#: literals. Both of those uses read this table, because two hand-maintained
#: copies of one layout are only ever accidentally equal: the translation table
#: used to be written out separately and silently omitted ``config_root``, so
#: every Hub settings file a Mac generated pointed at ``/etc/eidolon``.
#: Ordered longest-literal-first so a nested root is replaced before its parent.
PRODUCT_PATHS: Mapping[str, Path] = MappingProxyType(
    {
        "current_root": Path("/opt/eidolon/current"),
        "bootstrap_state_root": Path("/var/lib/eidolon-bootstrap"),
        "bootstrap_runtime_root": Path("/run/eidolon-bootstrap"),
        "install_root": Path("/opt/eidolon"),
        "config_root": Path("/etc/eidolon"),
        "state_root": Path("/var/lib/eidolon"),
        "runtime_root": Path("/run/eidolon"),
        "log_root": Path("/var/log/eidolon"),
        "cache_root": Path("/var/cache/eidolon"),
    }
)
_PATH_FIELDS = (
    "install_root",
    "current_root",
    "config_root",
    "state_root",
    "runtime_root",
    "log_root",
    "cache_root",
    "bootstrap_state_root",
    "bootstrap_runtime_root",
)


@dataclass(frozen=True, slots=True)
class HostPaths:
    """Absolute locations classified by ownership and lifetime."""

    install_root: Path
    current_root: Path
    config_root: Path
    state_root: Path
    runtime_root: Path
    log_root: Path
    cache_root: Path
    bootstrap_state_root: Path
    bootstrap_runtime_root: Path

    def environment(self) -> dict[str, str]:
        return {
            "EIDOLON_INSTALL_ROOT": str(self.install_root),
            "EIDOLON_WORKSPACE_ROOT": str(self.current_root),
            # Compatibility for components that have not yet renamed this
            # long-standing variable.  It has exactly the same value and is
            # not a second source of truth.
            "EIDOLON_ROOT": str(self.current_root),
            "EIDOLON_CONFIG_ROOT": str(self.config_root),
            "EIDOLON_STATE_ROOT": str(self.state_root),
            "EIDOLON_RUNTIME_ROOT": str(self.runtime_root),
            "EIDOLON_LOG_ROOT": str(self.log_root),
            "EIDOLON_CACHE_ROOT": str(self.cache_root),
            "EIDOLON_BOOTSTRAP_STATE_ROOT": str(self.bootstrap_state_root),
            "EIDOLON_BOOTSTRAP_RUNTIME_ROOT": str(self.bootstrap_runtime_root),
            # Bootstrap predates the host profile contract and consumes DIR
            # names.  They are aliases, not independently configurable paths.
            "EIDOLON_BOOTSTRAP_STATE_DIR": str(self.bootstrap_state_root),
            "EIDOLON_BOOTSTRAP_RUNTIME_DIR": str(self.bootstrap_runtime_root),
        }


@dataclass(frozen=True, slots=True)
class AppAccess:
    """Device-reachable application endpoints for one Host.

    ``lan_ipv4`` is optional because an address is observed state, not a
    decision. A Host that gets its address from DHCP has no stable value to
    declare, and a declaration that silently goes stale makes every reachability
    check ambiguous: you cannot tell a real network fault from an old config.
    Leave it out and the Host reports the address it currently has.
    """

    lan_ipv4: IPv4Address | None
    hub_https_port: int
    allow_insecure_livekit: bool

    #: The Setup code ``commissioning-code`` names instead of letting the Host
    #: draw one. A development loop pins it so the operator never has to look a
    #: code up: the command becomes one you fire without reading its output,
    #: and the digits you type on the phone are the same every time.
    #:
    #: The Host stays the authority — it re-checks the value and refuses one it
    #: would not have drawn itself. Nothing about the mechanism changes: the
    #: code still opens one ordinary session that expires, is spent once, dies
    #: after five wrong tries, and supersedes any window before it.
    #: The literal, when the profile pins one inline. That spelling belongs to
    #: an example rather than a tracked profile — a code in a tracked file is a
    #: code everybody has — so it is validated while this file is being read,
    #: which is where a reviewer would look for the mistake.
    setup_code: str | None = None
    #: Or the path the value lives at, which is where a tracked profile points.
    #: Read by `factory_setup_code` when something needs the code, not here.
    #:
    #: Lazily on purpose, and for the same reason the 15 install inputs are:
    #: they are machine-local secrets, and their absence is a failure of the
    #: operation that needs them rather than of every operation. Reading this
    #: one eagerly meant a fresh checkout could not run `status` — it failed on
    #: a pairing code `status` has no use for.
    setup_code_file: Path | None = None

    def factory_setup_code(self) -> str | None:
        """The code `commissioning-code` names, read at the moment it is wanted."""

        if self.setup_code is not None:
            return self.setup_code
        if self.setup_code_file is None:
            return None
        try:
            code = self.setup_code_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise HostProfileError(
                f"app.setup_code_file cannot be read: {self.setup_code_file}. This Host's "
                "factory pairing code lives with its other secrets rather than in this "
                "profile; create it there, or drop the field to ship without one."
            ) from exc
        _require_usable_setup_code(code)
        return code


@dataclass(frozen=True, slots=True)
class HostProfile:
    path: Path
    host_id: str
    platform: HostPlatform
    driver: HostDriver
    paths: HostPaths
    lifecycle_script: Path | None
    operations_config: Path | None

    @property
    def workspace_root(self) -> Path:
        """The checkout this profile drives, taken from the profile itself.

        A source-run profile points at a working tree: the lifecycle script it
        declares lives at ``<workspace>/deploy/dev/run_all.sh``, so the script
        the operator configured is what defines the workspace. Ops used to
        answer this by walking up from its own ``__file__``, which is only the
        same directory by coincidence — and stops being so the moment Ops is
        installed rather than run from a checkout.
        """

        if self.lifecycle_script is None:
            raise HostProfileError(
                f"{self.host_id} declares no lifecycle script, so it has no workspace"
            )
        return self.lifecycle_script.parents[2]

    foundation_mode: Literal["external"] | None = None
    external_livekit_config: Path | None = None
    app: AppAccess | None = None
    source_overrides: Mapping[str, SourceConfig] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def environment(self) -> dict[str, str]:
        values = self.paths.environment()
        values.update(
            {
                "EIDOLON_HOST_ID": self.host_id,
                "EIDOLON_HOST_PLATFORM": str(self.platform),
                "EIDOLON_HOST_DRIVER": str(self.driver),
            }
        )
        if self.foundation_mode is not None:
            values["EIDOLON_FOUNDATION_MODE"] = self.foundation_mode
        return values


def load_host_profile(path: Path) -> HostProfile:
    resolved = path.expanduser().resolve()
    try:
        document = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise HostProfileError(f"host profile is unreadable: {resolved}") from exc
    allowed_root = {"schema_version", "host", "paths", "adapter", "app", "source_overrides"}
    if not {"schema_version", "host", "paths", "adapter"}.issubset(document) or not set(
        document
    ).issubset(allowed_root):
        raise HostProfileError(
            "host profile root must contain schema_version, host, paths and adapter, with only app "
            "and source_overrides optional"
        )
    if document["schema_version"] != 1:
        raise HostProfileError("host profile schema_version must be 1")

    host = _table(document["host"], "host")
    if set(host) != {"id", "platform", "driver"}:
        raise HostProfileError("host must contain exactly id, platform and driver")
    host_id = _text(host["id"], "host.id")
    if _HOST_ID.fullmatch(host_id) is None:
        raise HostProfileError("host.id is invalid")
    platform = _member(HostPlatform, host["platform"], "host.platform")
    driver = _member(HostDriver, host["driver"], "host.driver")
    if _PLATFORM_DRIVERS[platform] is not driver:
        raise HostProfileError("host platform and driver are incompatible")

    paths_wire = _table(document["paths"], "paths")
    if set(paths_wire) != set(_PATH_FIELDS):
        raise HostProfileError(f"paths must contain exactly {', '.join(_PATH_FIELDS)}")
    paths = HostPaths(
        **{name: _absolute_path(paths_wire[name], f"paths.{name}") for name in _PATH_FIELDS}
    )
    _validate_paths(paths, platform=platform)

    adapter = _table(document["adapter"], "adapter")
    base = resolved.parent
    lifecycle_script: Path | None = None
    operations_config: Path | None = None
    foundation_mode: Literal["external"] | None = None
    external_livekit_config: Path | None = None
    if driver is HostDriver.LOCAL_SUPERVISORD:
        if set(adapter) != {
            "lifecycle_script",
            "operations_config",
            "foundation_mode",
            "external_livekit_config",
        }:
            raise HostProfileError(
                "local adapter must contain lifecycle, operations and foundation settings"
            )
        lifecycle_script = _local_path(
            adapter["lifecycle_script"], base, "adapter.lifecycle_script"
        )
        operations_config = _local_path(
            adapter["operations_config"], base, "adapter.operations_config"
        )
        mode = _text(adapter["foundation_mode"], "adapter.foundation_mode")
        if mode not in _FOUNDATION_MODES:
            raise HostProfileError(
                f"adapter.foundation_mode must be one of {sorted(_FOUNDATION_MODES)}"
            )
        foundation_mode = mode  # type: ignore[assignment]
        external_livekit_config = _local_path(
            adapter["external_livekit_config"], base, "adapter.external_livekit_config"
        )
    else:
        if set(adapter) != {"operations_config"}:
            raise HostProfileError("ssh adapter must contain only operations_config")
        operations_config = _local_path(
            adapter["operations_config"], base, "adapter.operations_config"
        )

    source_overrides = _source_overrides(document.get("source_overrides"), base=base)
    if source_overrides and driver is not HostDriver.LOCAL_SUPERVISORD:
        raise HostProfileError("source_overrides are available only for local-supervisord hosts")
    app = _app_access(document.get("app"), platform=platform, base=base)
    return HostProfile(
        path=resolved,
        host_id=host_id,
        platform=platform,
        driver=driver,
        paths=paths,
        lifecycle_script=lifecycle_script,
        operations_config=operations_config,
        foundation_mode=foundation_mode,
        external_livekit_config=external_livekit_config,
        app=app,
        source_overrides=source_overrides,
    )


def merged_environment(profile: HostProfile) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(profile.environment())
    return environment


def _validate_paths(paths: HostPaths, *, platform: HostPlatform) -> None:
    if paths.current_root == paths.install_root:
        if is_product_board(platform):
            raise HostProfileError(
                "a product board's current_root must be a link below install_root"
            )
    elif paths.install_root not in paths.current_root.parents:
        raise HostProfileError("current_root must equal or be below install_root")

    lifecycle_roots = (
        paths.config_root,
        paths.state_root,
        paths.runtime_root,
        paths.log_root,
        paths.cache_root,
    )
    if len(set(lifecycle_roots)) != len(lifecycle_roots):
        raise HostProfileError("config/state/runtime/log/cache roots must be distinct")
    if paths.bootstrap_state_root == paths.state_root:
        raise HostProfileError("Bootstrap state must remain a distinct ownership boundary")
    if paths.bootstrap_runtime_root == paths.runtime_root:
        raise HostProfileError("Bootstrap runtime must remain a distinct ownership boundary")
    if is_product_board(platform):
        for name, value in PRODUCT_PATHS.items():
            if getattr(paths, name) != value:
                raise HostProfileError(f"a product board's paths.{name} must be {value}")


def _table(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise HostProfileError(f"{label} must be a table")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HostProfileError(f"{label} must be a non-empty string")
    return value.strip()


def _member(enumeration: type[Any], value: object, label: str) -> Any:
    try:
        return enumeration(_text(value, label))
    except ValueError as exc:
        allowed = ", ".join(sorted(str(member) for member in enumeration))
        raise HostProfileError(f"{label} must be one of {allowed}") from exc


#: Kept beside the rule that uses it. The Host owns the real definition
#: (``eidolon_admin_server.bootstrap.domain``); ops cannot import across the
#: repo boundary, so it restates the rule rather than guessing at it.
SETUP_CODE_DIGITS = 8


def _require_usable_setup_code(code: str) -> None:
    """One rule, wherever the value came from."""

    if not _is_usable_setup_code(code):
        raise HostProfileError(
            "app.setup_code must be a code the Host would have drawn: "
            f"{SETUP_CODE_DIGITS} digits, not all the same, and not the "
            "plain run up or down"
        )


def _is_usable_setup_code(value: str) -> bool:
    if len(value) != SETUP_CODE_DIGITS or not value.isdigit() or not value.isascii():
        return False
    if len(set(value)) == 1:
        return False
    ascending = "".join(str(digit % 10) for digit in range(SETUP_CODE_DIGITS))
    return value not in {ascending, ascending[::-1]}


def _app_access(
    value: object | None, *, platform: HostPlatform, base: Path
) -> AppAccess | None:
    if value is None:
        return None
    document = _table(value, "app")
    required = {"hub_https_port", "allow_insecure_livekit"}
    optional = {"lan_ipv4", "setup_code", "setup_code_file"}
    if not required <= set(document) or not set(document) <= (required | optional):
        raise HostProfileError(
            f"app must contain exactly {', '.join(sorted(required))}, with only "
            + ", ".join(sorted(optional))
            + " optional"
        )
    address: IPv4Address | None = None
    if "lan_ipv4" in document:
        try:
            address = ip_address(_text(document["lan_ipv4"], "app.lan_ipv4"))
        except ValueError as exc:
            raise HostProfileError("app.lan_ipv4 must be a private IPv4 address") from exc
        if not isinstance(address, IPv4Address) or not address.is_private or address.is_loopback:
            raise HostProfileError("app.lan_ipv4 must be a private IPv4 address")
    port = document["hub_https_port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise HostProfileError("app.hub_https_port must be a valid TCP port")
    # No `livekit_client_url` here any more. The checks that used to guard it
    # had squeezed the field to exactly two legal values — the declared
    # `lan_ipv4`, or this Host's derived hostname — and Ops holds both, so the
    # field carried nothing and cost a transcription. It is derived now, in
    # `host_identity.livekit_client_url`, which is also where the one rule that
    # survived lives: plain `ws://` only when this profile opted in.
    allow_insecure = document["allow_insecure_livekit"]
    if not isinstance(allow_insecure, bool):
        raise HostProfileError("app.allow_insecure_livekit must be boolean")
    # Two spellings, one value. `setup_code` is the literal, which is right for
    # an example and wrong for a profile this repository tracks: a code in a
    # tracked file is a code everybody has. `setup_code_file` names a path
    # instead — under the operator's own ignored input directory, beside every
    # other secret this Host is installed with — so the profile can be reviewed
    # and shared while the code stays one operator's.
    if "setup_code" in document and "setup_code_file" in document:
        raise HostProfileError("app may name setup_code or setup_code_file, not both")
    setup_code: str | None = None
    setup_code_file: Path | None = None
    if "setup_code" in document:
        # Inline and therefore tracked: mirrored from the Host's own rule so a
        # bad value is caught while reading this file rather than three hops
        # away on the machine. The Host re-checks it and stays the authority.
        setup_code = _text(document["setup_code"], "app.setup_code")
        _require_usable_setup_code(setup_code)
    elif "setup_code_file" in document:
        # Declared here, read when wanted. The declaration is what this file
        # can be reviewed for; the value is one operator's machine-local
        # secret, like the 15 install inputs beside it.
        setup_code_file = _local_path(document["setup_code_file"], base, "app.setup_code_file")
    return AppAccess(
        lan_ipv4=address,
        hub_https_port=port,
        allow_insecure_livekit=allow_insecure,
        setup_code=setup_code,
        setup_code_file=setup_code_file,
    )


def _source_overrides(value: object | None, *, base: Path) -> Mapping[str, SourceConfig]:
    if value is None:
        return MappingProxyType({})
    document = _table(value, "source_overrides")
    unknown = set(document).difference(SOURCE_IDS)
    if unknown:
        raise HostProfileError(
            "source_overrides contains unknown source: " + ", ".join(sorted(unknown))
        )
    overrides: dict[str, SourceConfig] = {}
    for source_id, raw in document.items():
        source = _table(raw, f"source_overrides.{source_id}")
        # ``path`` is the capability that has to survive: a Host profile points
        # Memory and Channel at frozen copies whose commits the shared release
        # matrix cannot use. ``revision`` is optional for the same reason it is
        # optional in the operations config — the checkout named here knows
        # which commit it is on, and asking a person to restate it is asking
        # them to keep two copies of one fact equal by hand.
        if "path" not in source or not set(source) <= {"path", "revision"}:
            raise HostProfileError(
                f"source_overrides.{source_id} must contain a path and may contain a revision"
            )
        revision: str | None = None
        if "revision" in source:
            revision = _text(source["revision"], f"source_overrides.{source_id}.revision")
            if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                raise HostProfileError(
                    f"source_overrides.{source_id}.revision must be exactly 40 lowercase hex"
                )
        overrides[source_id] = SourceConfig(
            path=_local_path(source["path"], base, f"source_overrides.{source_id}.path"),
            revision=revision,
        )
    return MappingProxyType(overrides)


def _absolute_path(value: object, label: str) -> Path:
    path = Path(_text(value, label)).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise HostProfileError(f"{label} must be a safe absolute path")
    # Host paths can describe a remote machine.  Normalize lexically without
    # resolving symlinks against the workstation filesystem.
    return Path(os.path.abspath(path))


def _local_path(value: object, base: Path, label: str) -> Path:
    path = Path(_text(value, label)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)
