"""Deliver the host agent as one payload, without leaving it on the Host.

The agent used to be one 3000-line file because that is what fits down a pipe.
It is a package now, and this puts it back into a single script: a small loader
followed by the package's own sources, so the injection contract — one stdin
payload, no file written, no dependency on anything installed there — is
unchanged.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

#: The name the package is imported under on the Host. Deliberately not
#: ``eidolon_ops.hostagent``: Ops is never installed on a product Host, and a
#: dotted name would ask the loader to invent the parent package too.
PACKAGE = "eidolon_hostagent"
_SOURCE_ROOT = Path(__file__).with_name("hostagent")

#: Reads the embedded sources out of the payload itself. ``__file__`` is set so
#: a traceback from the Host names the module and line an operator can open,
#: even though no such file exists there.
_LOADER = '''\
import base64, importlib.util, sys


class _Loader:
    def __init__(self, name):
        self.name = name

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        origin = module.__spec__.origin
        source = base64.b64decode(_MODULES[self.name]).decode("utf-8")
        exec(compile(source, origin, "exec"), module.__dict__)


class _Finder:
    def find_spec(self, name, path=None, target=None):
        if name not in _MODULES:
            return None
        relative = name.split(".")[1:] or ["__init__"]
        return importlib.util.spec_from_loader(
            name,
            _Loader(name),
            origin="<%s>/%s.py" % (_PACKAGE, "/".join(relative)),
            is_package=name in _PACKAGES,
        )


sys.meta_path.insert(0, _Finder())
from __PACKAGE__.__main__ import main

raise SystemExit(main())
'''


def injected_script() -> bytes:
    """One self-contained script that runs the host agent and exits."""

    modules: dict[str, str] = {}
    packages = [PACKAGE]
    for path in sorted(_SOURCE_ROOT.glob("*.py")):
        name = PACKAGE if path.stem == "__init__" else f"{PACKAGE}.{path.stem}"
        modules[name] = base64.b64encode(path.read_bytes()).decode("ascii")
    if PACKAGE not in modules or f"{PACKAGE}.__main__" not in modules:
        raise RuntimeError("the host agent package is incomplete")
    header = (
        "#!/usr/bin/env python3\n"
        '"""Injected Eidolon host agent. Generated at delivery; do not edit."""\n'
        f"_PACKAGE = {json.dumps(PACKAGE)}\n"
        f"_PACKAGES = {json.dumps(packages)}\n"
        f"_MODULES = {json.dumps(modules, indent=0, sort_keys=True)}\n"
    )
    return (header + _LOADER.replace("__PACKAGE__", PACKAGE)).encode("utf-8")
