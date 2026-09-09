from pathlib import Path

import pytest

from eidolon_ops.errors import InstallInputError
from eidolon_ops.hostagent.contract import OPTIONAL_INSTALL_INPUTS
from eidolon_ops.private_inputs import INSTALL_DESTINATION_NAMES, require_safe_input_directory


def inputs(tmp_path: Path) -> Path:
    root = tmp_path / "inputs"
    root.mkdir(mode=0o700)
    for name in INSTALL_DESTINATION_NAMES.values():
        path = root / name
        path.touch(mode=0o600)
    return root


def test_declared_optional_inputs_are_accepted_and_preserved(tmp_path: Path) -> None:
    root = inputs(tmp_path)
    for name in OPTIONAL_INSTALL_INPUTS:
        (root / name).touch(mode=0o600)
    assert require_safe_input_directory(root) == set(INSTALL_DESTINATION_NAMES.values()) | OPTIONAL_INSTALL_INPUTS
    assert all((root / name).exists() for name in OPTIONAL_INSTALL_INPUTS)


@pytest.mark.parametrize("case", ["unknown", "missing", "insecure", "symlink"])
def test_optional_inputs_do_not_weaken_directory_checks(tmp_path: Path, case: str) -> None:
    root = inputs(tmp_path)
    optional = root / next(iter(OPTIONAL_INSTALL_INPUTS))
    if case == "unknown":
        (root / "unexpected").touch(mode=0o600)
    elif case == "missing":
        (root / next(iter(INSTALL_DESTINATION_NAMES.values()))).unlink()
    elif case == "insecure":
        optional.touch(mode=0o644)
    else:
        optional.symlink_to(root / "channel.env")
    with pytest.raises(InstallInputError):
        require_safe_input_directory(root)
