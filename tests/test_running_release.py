"""What each live process is executing, and putting a Host back on one release."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from eidolon_ops.hostagent import contract, primitives, runtime_release
from eidolon_ops.hostagent.primitives import TargetError

pytestmark = pytest.mark.component


def _release(root: Path, release_id: str, component: str = "eidolon_channel") -> Path:
    path = root / contract.RELEASES.relative_to("/") / release_id / component
    path.mkdir(parents=True, exist_ok=True)
    return path


def _link(root: Path, release_id: str) -> None:
    """Point every component link at one release, the way an activation does."""

    for component, absolute in contract.CURRENT_LINKS.items():
        link = root / absolute.relative_to("/")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(_release(root, release_id, component))


def _process(
    root: Path,
    pid: int,
    *,
    unit: str | None = None,
    exe: str | None = None,
    argv: tuple[str, ...] = (),
    cgroup: str | None = None,
) -> None:
    entry = root / "proc" / str(pid)
    entry.mkdir(parents=True)
    if exe is not None:
        (entry / "exe").symlink_to(exe)
    (entry / "cmdline").write_bytes(b"".join(value.encode() + b"\0" for value in argv))
    if cgroup is not None:
        (entry / "cgroup").write_text(cgroup, encoding="utf-8")
    elif unit is not None:
        (entry / "cgroup").write_text(f"0::/system.slice/{unit}\n", encoding="utf-8")


def _venv_python(root: Path, release_id: str, component: str = "eidolon_channel") -> str:
    return str(_release(root, release_id, component) / ".venv/bin/python")


@pytest.fixture(autouse=True)
def procfs(tmp_path: Path) -> None:
    (tmp_path / "proc").mkdir(exist_ok=True)


def test_argv_names_the_release_when_the_executable_cannot(tmp_path: Path) -> None:
    """A venv console script is the ordinary case, and exe is blind to it.

    pip bakes the absolute interpreter path into each script's shebang when the
    virtualenv is built, so the kernel executes the release's own python and
    puts it in argv[0]. `exe` follows that interpreter's symlink out to the
    system python, which names no release — which is why comparing exe against
    the active release was the check that could not have found this.
    """

    _process(
        tmp_path,
        4711,
        unit="eidolon-channel-provider.service",
        exe="/usr/bin/python3.13",
        argv=(_venv_python(tmp_path, "old"), "-m", "eidolon_channel.provider"),
    )

    observed = runtime_release.observe(root=tmp_path)

    assert observed["releases"] == {
        "old": [{"pid": 4711, "unit": "eidolon-channel-provider.service"}]
    }


def test_a_process_on_no_release_is_not_reported(tmp_path: Path) -> None:
    _process(
        tmp_path,
        7,
        unit="eidolon-hub-ingress.service",
        exe="/usr/bin/python3",
        argv=("/usr/bin/python3", "/usr/local/libexec/eidolon-hub-lan-ingress"),
    )
    _process(tmp_path, 2, argv=())

    assert runtime_release.observe(root=tmp_path)["releases"] == {}


def test_a_neighbouring_directory_is_not_mistaken_for_a_release(tmp_path: Path) -> None:
    """The prefix match stops at a path separator, not at a string boundary."""

    _process(
        tmp_path,
        31,
        unit="eidolon-kernel.service",
        argv=(str(tmp_path / "opt/eidolon/releases-archive/old/bin/python"),),
    )

    assert runtime_release.observe(root=tmp_path)["releases"] == {}


def test_a_release_root_reached_through_a_symlink_is_matched_either_way(
    tmp_path: Path,
) -> None:
    """argv keeps the literal path; exe comes back resolved. Both must match.

    The moment any parent of the release root is a symlink the two spellings
    diverge, and matching only one of them reports a live release as unused —
    the single mistake this reading exists to prevent.
    """

    store = tmp_path / "store/eidolon"
    (store / "releases/old/eidolon_channel/.venv/bin").mkdir(parents=True)
    (tmp_path / "opt").mkdir(parents=True, exist_ok=True)
    (tmp_path / "opt/eidolon").symlink_to(store)
    resolved = str(store / "releases/old/eidolon_channel/.venv/bin/python")
    literal = str(tmp_path / "opt/eidolon/releases/old/eidolon_channel/.venv/bin/python")
    assert resolved != literal
    _process(tmp_path, 10, unit="eidolon-channel.service", exe=resolved)
    _process(tmp_path, 20, unit="eidolon-channel-provider.service", argv=(literal,))

    observed = runtime_release.observe(root=tmp_path)

    assert observed["releases"] == {
        "old": [
            {"pid": 20, "unit": "eidolon-channel-provider.service"},
            {"pid": 10, "unit": "eidolon-channel.service"},
        ]
    }


def test_a_forked_worker_is_reported_under_the_unit_an_operator_restarts(
    tmp_path: Path,
) -> None:
    """The deepest unit in the cgroup path wins, so a child names its service.

    Reading each unit's MainPID would have missed this process entirely, which
    is the reason every process is read instead.
    """

    _process(
        tmp_path,
        910,
        exe="/usr/bin/python3.13",
        argv=(_venv_python(tmp_path, "old"), "--worker"),
        cgroup="0::/system.slice/eidolon-channel-provider.service/init.scope\n",
    )

    observed = runtime_release.observe(root=tmp_path)

    assert observed["releases"]["old"] == [
        {"pid": 910, "unit": "eidolon-channel-provider.service"}
    ]


def test_a_process_outside_systemd_is_reported_without_a_unit(tmp_path: Path) -> None:
    """Reported, not dropped. Something must still refuse to sweep under it."""

    _process(tmp_path, 4242, argv=(_venv_python(tmp_path, "old"),), cgroup="0::/user.slice\n")

    assert runtime_release.observe(root=tmp_path)["releases"] == {
        "old": [{"pid": 4242, "unit": None}]
    }


def test_an_unreadable_process_table_is_published_as_unreadable(tmp_path: Path) -> None:
    """`status` must not fail on this reading, and must not fake it either."""

    (tmp_path / "proc").rmdir()

    published = runtime_release.report(root=tmp_path)

    assert published["status"] == "unreadable"
    assert published["releases"] == {}
    assert "no release can be proven unused" in str(published["error"])


def test_active_release_ids_reads_every_link_not_just_one(tmp_path: Path) -> None:
    _link(tmp_path, "new")
    stray = tmp_path / contract.CURRENT_LINKS["eidolon_agent"].relative_to("/")
    stray.unlink()
    stray.symlink_to(_release(tmp_path, "old", "eidolon_agent"))

    assert runtime_release.active_release_ids(root=tmp_path) == {"new", "old"}


def test_a_current_link_pointing_outside_the_release_root_is_refused(
    tmp_path: Path,
) -> None:
    """There is no release to converge toward, and no safe guess at one."""

    _link(tmp_path, "new")
    stray = tmp_path / contract.CURRENT_LINKS["eidolon_hub"].relative_to("/")
    stray.unlink()
    stray.symlink_to(tmp_path / "somewhere-else/eidolon_hub")

    with pytest.raises(TargetError, match="points outside the release root: eidolon_hub"):
        runtime_release.active_release_ids(root=tmp_path)


def test_a_component_with_no_link_yet_does_not_invent_a_release(tmp_path: Path) -> None:
    _link(tmp_path, "new")
    (tmp_path / contract.CURRENT_LINKS["eidolon_memory"].relative_to("/")).unlink()

    assert runtime_release.active_release_ids(root=tmp_path) == {"new"}


def _payload() -> dict[str, object]:
    return {"units": list(contract.PRODUCT_UNITS), "capabilities": []}


def _ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess((), 0, "", "")


def _forbidden(reason: str):
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(reason)

    return refuse


def _restarts(
    monkeypatch: pytest.MonkeyPatch, root: Path, *, onto: str | None
) -> list[tuple[str, ...]]:
    """Record every restart, and let it move that unit's processes for real.

    ``onto=None`` is the restart that reports success and changes nothing —
    the case the second reading exists to catch.
    """

    commands: list[tuple[str, ...]] = []

    def restart(_operation: str, command: tuple[str, ...], **_kwargs: object):
        commands.append(command)
        if onto is None:
            return _ok()
        unit = command[-1]
        for entry in sorted((root / "proc").iterdir()):
            cgroup = entry / "cgroup"
            if not cgroup.is_file() or unit not in cgroup.read_text(encoding="utf-8"):
                continue
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
            moved = [
                value.replace(b"/releases/old/", f"/releases/{onto}/".encode())
                for value in arguments
            ]
            (entry / "cmdline").write_bytes(b"\0".join(moved))
        return _ok()

    monkeypatch.setattr(primitives, "checked", restart)
    return commands


def test_converge_leaves_a_host_that_already_agrees_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _link(tmp_path, "new")
    _process(
        tmp_path,
        100,
        unit="eidolon-channel-provider.service",
        argv=(_venv_python(tmp_path, "new"),),
    )
    monkeypatch.setattr(
        primitives, "checked", _forbidden("a converged Host was restarted anyway")
    )

    result = runtime_release.converge(_payload(), root=tmp_path)

    assert result == {
        "status": "converged",
        "release_id": "new",
        "restarted": [],
        "running_releases": {
            "new": [{"pid": 100, "unit": "eidolon-channel-provider.service"}]
        },
    }


def test_converge_restarts_only_the_unit_that_disagrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proven remedy, issued to the one unit that needs it.

    A Host that has just passed activation has no reason to take its database
    down again, so this is targeted rather than a full release restart.
    """

    _link(tmp_path, "new")
    _process(
        tmp_path,
        100,
        unit="eidolon-data.service",
        argv=(_venv_python(tmp_path, "new", "eidolon_data"),),
    )
    _process(
        tmp_path,
        200,
        unit="eidolon-channel-provider.service",
        exe="/usr/bin/python3.13",
        argv=(_venv_python(tmp_path, "old"), "-m", "eidolon_channel.provider"),
    )
    commands = _restarts(monkeypatch, tmp_path, onto="new")

    result = runtime_release.converge(_payload(), root=tmp_path)

    assert commands == [
        ("/usr/bin/systemctl", "restart", "eidolon-channel-provider.service")
    ]
    assert result["status"] == "reconverged"
    assert result["restarted"] == ["eidolon-channel-provider.service"]
    assert result["stale_release_ids"] == ["old"]
    assert result["running_releases"] == {
        "new": [
            {"pid": 200, "unit": "eidolon-channel-provider.service"},
            {"pid": 100, "unit": "eidolon-data.service"},
        ]
    }


def test_converge_restarts_in_product_startup_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dependencies first, which pid order and alphabetical order both get wrong."""

    _link(tmp_path, "new")
    _process(
        tmp_path, 300, unit="eidolon-channel.service", argv=(_venv_python(tmp_path, "old"),)
    )
    _process(tmp_path, 100, unit="eidolon-hub.service", argv=(_venv_python(tmp_path, "old"),))
    commands = _restarts(monkeypatch, tmp_path, onto="new")

    runtime_release.converge(_payload(), root=tmp_path)

    assert [command[-1] for command in commands] == [
        "eidolon-hub.service",
        "eidolon-channel.service",
    ]


def test_converge_believes_the_second_reading_not_the_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart that reports success and changes nothing must not pass.

    That is the same failure shape as the one this module exists for: taking
    an exit status for the fact it was supposed to establish.
    """

    _link(tmp_path, "new")
    _process(
        tmp_path,
        200,
        unit="eidolon-channel-provider.service",
        exe="/usr/bin/python3.13",
        argv=(_venv_python(tmp_path, "old"),),
    )
    _restarts(monkeypatch, tmp_path, onto=None)

    with pytest.raises(TargetError) as failure:
        runtime_release.converge(_payload(), root=tmp_path)

    message = str(failure.value)
    assert "eidolon-channel-provider.service (pid 200) on old" in message
    assert "current links name new" in message


def test_converge_names_a_holder_it_cannot_restart_rather_than_passing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator's shell on an old release is not a unit, and not ignorable."""

    _link(tmp_path, "new")
    _process(tmp_path, 4242, argv=(_venv_python(tmp_path, "old"),), cgroup="0::/user.slice\n")
    commands = _restarts(monkeypatch, tmp_path, onto="new")

    with pytest.raises(TargetError, match=r"no systemd unit \(pid 4242\) on old"):
        runtime_release.converge(_payload(), root=tmp_path)

    assert commands == []


def test_converge_refuses_a_host_whose_links_name_two_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half a cutover has no single release to converge toward, and naming one
    would be inventing the answer."""

    _link(tmp_path, "new")
    stray = tmp_path / contract.CURRENT_LINKS["eidolon_agent"].relative_to("/")
    stray.unlink()
    stray.symlink_to(_release(tmp_path, "old", "eidolon_agent"))
    monkeypatch.setattr(
        primitives, "checked", _forbidden("an ambiguous Host was restarted anyway")
    )

    with pytest.raises(TargetError, match="requires every current link to name one release"):
        runtime_release.converge(_payload(), root=tmp_path)


def test_converge_refuses_a_payload_outside_the_reviewed_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit names come off the Host's own cgroups; what may be restarted still
    comes off the reviewed table."""

    monkeypatch.setattr(
        primitives, "checked", _forbidden("an unreviewed topology was restarted")
    )

    with pytest.raises(TargetError, match="unit set differs"):
        runtime_release.converge(
            {"units": ["anything.service"], "capabilities": []}, root=tmp_path
        )
