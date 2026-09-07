"""Release a Host's Owner binding through Bootstrap's own store code.

The binding is Bootstrap's record that the Data plane holds a Workspace for
this Host. It is not an independent fact: ``owner_id`` is a pure function of
the Host id, so what the row carries is the claim, not the identity. A claim
about another plane's store must not outlive it — and a source run's
``reset --wipe-authority-data`` destroys exactly that store while keeping the
Bootstrap root the claim lives in.

Asked of Admin's own interpreter rather than by opening the database here, for
the same reason ``kernel_schema`` asks the Kernel: the answer to "what does
this row mean and how is it cleared" belongs to the component whose schema it
is, and a second implementation in this repository is the one nobody would
think to update. Offline rather than over the control socket, because the
caller has already stopped the Host — and because a Host that cannot start is
one of the Hosts that needs this.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import primitives
from .primitives import TargetError

#: Run by Admin's own interpreter, against Admin's own store class. It reports
#: before and after so the reset can say what it took, and it does not run the
#: schema ladder: migrating another component's authority is not this
#: operation's business, and a database too old to read is a refusal.
RELEASE_SCRIPT = """
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from eidolon_admin_server.bootstrap.adapters.persistence.sqlite import (
    SQLiteBootstrapStateStore,
)

store = SQLiteBootstrapStateStore(Path(sys.argv[1]))
store.open()
try:
    before = store.get_state()
    after = store.release_owner_binding(
        now=datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )
finally:
    store.close()
print(
    json.dumps(
        {
            "released": before.owner_id is not None,
            "before": before.to_dict(),
            "after": after.to_dict(),
        }
    )
)
"""


def release_owner_binding(
    *,
    database: Path,
    interpreter: Path,
    command: Callable[..., subprocess.CompletedProcess[str]] = primitives.run,
) -> dict[str, object]:
    """Forget which Owner this Host holds, keeping identity and Grants.

    Idempotent, and absent is not a failure: a Host with no Bootstrap database
    has no claim to withdraw, and the caller runs this unconditionally.
    """

    if not database.exists():
        return {"released": False, "state": "absent", "database": str(database)}
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise TargetError(
            "the Owner binding cannot be released because Admin's own "
            f"interpreter is missing: {interpreter}. Refusing rather than "
            "destroying the Data authority and leaving Bootstrap claiming it "
            "still exists — which is the state no phone can finish setup on"
        )
    result = command((str(interpreter), "-c", RELEASE_SCRIPT, str(database)), timeout=60)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise TargetError(f"Owner binding release failed: {detail}")
    try:
        document = json.loads(result.stdout)
    except ValueError as exc:
        raise TargetError("Owner binding release returned no report") from exc
    if not isinstance(document, dict) or "released" not in document:
        raise TargetError("Owner binding release returned an unrecognized report")
    return {**document, "state": "released", "database": str(database)}
