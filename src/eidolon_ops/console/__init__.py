"""A workstation console over the same lifecycle contract the CLI drives.

This is not a second Ops. It builds the same ``HostController``, declares the
same ``Plan``, and renders the same ``Evidence``; what it adds is the thing a
terminal cannot give a twenty-minute release — a step that says it is running
while it is still running — and the confirmation gradient ``plans`` was written
for.

It stays a workstation tool. It binds to loopback, it is never installed on a
product Host, and it holds nothing on disk: a run lives in this process and dies
with it, because a console that persisted release evidence would be a second
authority for it.
"""

from __future__ import annotations

from eidolon_ops.console.errors import ConsoleError

__all__ = ["ConsoleError"]
