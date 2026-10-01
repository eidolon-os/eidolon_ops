"""Resolve operator-owned configuration paths across privilege transitions."""
import os
import pwd
from pathlib import Path


def expand_operator_path(value: str, base: Path) -> Path:
    # Escalating the host adapter must not redirect an existing Host's identity
    # and state to root's home. The configuration owner is filesystem evidence,
    # not a caller-supplied environment identity.
    if os.geteuid() == 0 and (value == '~' or value.startswith('~/')):
        home = Path(pwd.getpwuid(base.stat().st_uid).pw_dir)
        return home / value.removeprefix('~/') if value != '~' else home
    return Path(value).expanduser()
