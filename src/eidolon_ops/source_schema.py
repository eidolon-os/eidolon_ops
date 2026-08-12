"""The Data schema gate for a source run.

Data owns its own migrations; Ops only runs the entrypoint that component
publishes, with the Host path contract and the generated credentials in the
environment. Nothing here knows what a migration does.
"""

from __future__ import annotations

import os

from eidolon_ops import environment
from eidolon_ops.config import OperationsConfig
from eidolon_ops.errors import OperationsError
from eidolon_ops.paths import HostProfile
from eidolon_ops.process import ProcessRunner, checked


def migrate_data_schema(
    profile: HostProfile,
    config: OperationsConfig,
    runner: ProcessRunner,
) -> None:
    source = config.sources["eidolon_data"].path
    alembic = source / ".venv/bin/alembic"
    if not os.access(alembic, os.X_OK):
        raise OperationsError(f"Data migration entrypoint is missing: {alembic}")
    values = os.environ.copy()
    values.update(profile.environment())
    values.update(
        environment.parse(
            (profile.paths.config_root / "env/data.env").read_text(encoding="utf-8"),
            label="generated env file",
            key=environment.OPS_KEY,
        )
    )
    checked(
        "Mac product-source Data schema gate",
        runner.run(
            (str(alembic), "-c", "alembic.ini", "upgrade", "head"),
            cwd=source,
            env=values,
            timeout=120,
        ),
    )
