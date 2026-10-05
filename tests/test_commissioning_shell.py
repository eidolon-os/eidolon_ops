"""Exercise the real commissioning entry point under macOS-compatible Bash."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("arguments", [[], ["--code", "99999990"], ["--ttl", "600"]])
def test_commissioning_arguments_under_nounset(tmp_path, arguments):
    source = (ROOT / "deploy/dev/run_all.sh").read_text()
    function = source.split("do_product_source_commissioning_code() {", 1)[1]
    function = "do_product_source_commissioning_code() {" + function.split(
        "\ndo_product_source_web_start()", 1
    )[0]
    wrapper = tmp_path / "deploy/supervisor/wrappers/with-env.sh"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    wrapper.chmod(0o755)
    script = """set -euo pipefail
configure_supervisor_profile() { :; }
require_control_runtime() { :; }
OPS_ROOT="$1"
EIDOLON_SOURCE_ADMIN="/source admin"
EIDOLON_PRODUCT_ENV_ROOT="/env root"
shift
""" + function + '\ndo_product_source_commissioning_code "$@"\n'
    result = subprocess.run(
        ["/bin/bash", "-c", script, "test", str(tmp_path), *arguments],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.splitlines() == [
        "/source admin", "/env root/bootstrap.env", "--",
        "/source admin/.venv/bin/eidolon-bootstrapctl", "commissioning-code",
        *arguments,
    ]
