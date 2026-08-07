from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_ops.process import SubprocessRunner
from eidolon_ops.transport import SSHTransport

pytestmark = pytest.mark.integration


def test_real_subprocess_boundary_with_fake_ssh(config, tmp_path: Path) -> None:
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(
        "#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '{\"status\":\"fake-ssh-ok\"}'\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    transport = SSHTransport(
        config.host,
        SubprocessRunner(),
        ssh=str(fake_ssh),
    )

    result = transport.run_agent("status", {"units": []})

    assert result == {"status": "fake-ssh-ok"}
