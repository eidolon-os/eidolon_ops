"""The HTTP surface, which decides nothing.

Every rule this file enforces is read off something else: which operations exist
comes from the catalog, which of them a Host is offered comes from the adapter's
capabilities, and how much confirmation an operation needs comes from its plan.
What is left here is transport — validate the body, refuse an unconfirmed
mutation, hand the work to a run, and stream what the run says.

The confirmation check lives on this side of the wire on purpose. A browser is
where the gradient is *shown*; it cannot be where the gradient is *enforced*, or
the enforcement is one fetch call away from being skipped.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi import Request as HTTPRequest
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic import Field as Body

from eidolon_ops.console import catalog
from eidolon_ops.console.catalog import Confirmation, Operation
from eidolon_ops.console.errors import ConsoleError
from eidolon_ops.console.hosts import HostRegistry
from eidolon_ops.console.runs import Request as RunRequest
from eidolon_ops.console.runs import RunStore
from eidolon_ops.model import Capability
from eidolon_ops.paths import HostDriver
from eidolon_ops.readiness import HostKind, describe

__all__ = ["create_app"]

STATIC_ROOT = Path(__file__).resolve().parent / "static"
#: Artifacts the console suggests a local path for, and what that path ends in.
_ARTIFACTS = {("backup", "output"): ".tar.gz", ("diagnose", "output"): ".tar.gz"}
#: The page names its assets by content hash, so the assets may be cached
#: forever and the page naming them may not be cached at all. Getting this pair
#: wrong is silent: a rebuild lands on disk, the browser keeps serving the old
#: hash it read from a cached page, and the console looks like it ignored the
#: change.
_NO_STORE = {"Cache-Control": "no-store"}
_IMMUTABLE = "public, max-age=31536000, immutable"
_UNBUILT = """<!doctype html><meta charset="utf-8"><title>eidolon-ops console</title>
<body style="font:14px/1.6 ui-monospace,monospace;padding:3rem;max-width:46rem">
<h1>前端还没有构建</h1>
<p>这个控制台的界面是一份构建产物，不进 Git。在仓库根目录跑一次：</p>
<pre>cd web &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>然后刷新本页。API 已经在运行：<a href="/api/hosts">/api/hosts</a></p>
</body>"""


class Confirm(BaseModel):
    """What the operator said before a mutation was allowed to start."""

    acknowledge: bool = False
    #: Typed out in full for an irreversible operation, and compared to the
    #: Host this run is aimed at — not to the one the browser thinks it is on.
    host_id: str | None = None


class OperationBody(BaseModel):
    operation: str
    parameters: dict[str, Any] = Body(default_factory=dict)
    confirm: Confirm = Body(default_factory=Confirm)


def create_app(registry: HostRegistry, *, store: RunStore | None = None) -> FastAPI:
    runs = store if store is not None else RunStore()
    app = FastAPI(
        title="eidolon-ops console",
        summary="One workstation console over the Eidolon Host lifecycle contract",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.registry = registry
    app.state.runs = runs

    @app.exception_handler(ConsoleError)
    async def _refused(_request: HTTPRequest, exc: ConsoleError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "profiles": [str(path) for path in registry.paths],
            "static": STATIC_ROOT.is_dir(),
        }

    @app.get("/api/hosts")
    def hosts() -> dict[str, object]:
        return {
            "hosts": [
                {
                    **entry.to_json(),
                    "active_run": runs.active(entry.host_id),
                }
                for entry in registry.entries()
            ]
        }

    @app.get("/api/hosts/{host_id}")
    def host(host_id: str) -> dict[str, object]:
        entry = registry.entry(host_id)
        return {
            **entry.to_json(),
            "active_run": runs.active(host_id),
            "operations": _operations(registry, host_id, entry.capabilities),
            # What each readiness fact reads, from the one contract that defines
            # them. Sent so the browser can label a red fact without keeping its
            # own copy of a check set that fails closed when it drifts.
            "readiness": _readiness(entry.profile),
        }

    @app.post("/api/hosts/{host_id}/plan")
    def plan(host_id: str, body: OperationBody) -> dict[str, object]:
        """Show what an operation would be, without contacting the Host.

        The plan is pure, so this answers instantly and can be re-asked on every
        keystroke: the operator watches the gradient change as they tick
        ``--wipe-authority-data``, rather than reading about it afterwards.
        """

        spec, params = _prepare(registry, host_id, body)
        return _preview(spec, params, host_id)

    @app.post("/api/hosts/{host_id}/runs", status_code=202)
    def start(host_id: str, body: OperationBody) -> dict[str, object]:
        spec, params = _prepare(registry, host_id, body)
        preview = _preview(spec, params, host_id)
        required = Confirmation(str(preview["confirmation"]))
        _require_confirmation(required, body.confirm, host_id)
        run = runs.start(
            RunRequest(
                host_id=host_id,
                operation=spec.name,
                label=spec.label,
                parameters={key: _displayable(value) for key, value in params.items()},
                plan=preview["plan"],  # type: ignore[arg-type]
                confirmation=str(required),
                mutating=bool(preview["mutating"]),
                allow_keys=spec.sensitive_keys,
                work=lambda sink: spec.invoke(
                    registry.controller(host_id, progress=sink), params
                ),
            )
        )
        return run.to_json()

    @app.get("/api/runs")
    def listing(host_id: str | None = None, limit: int = 50) -> dict[str, object]:
        return {
            "runs": [
                run.summary()
                for run in runs.listing(host_id=host_id, limit=max(1, min(limit, 200)))
            ]
        }

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, object]:
        return runs.get(run_id).to_json()

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: str) -> StreamingResponse:
        runs.get(run_id)  # refuse an unknown run before opening a stream
        return StreamingResponse(
            _sse(runs, run_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    _mount_console(app)
    return app


# -- request handling ---------------------------------------------------------


def _prepare(
    registry: HostRegistry, host_id: str, body: OperationBody
) -> tuple[Operation, dict[str, Any]]:
    entry = registry.entry(host_id)
    if entry.profile is None:
        raise ConsoleError(f"{host_id} does not load: {entry.error}", status=409)
    spec = catalog.operation(body.operation)
    if spec.capability not in entry.capabilities:
        raise ConsoleError(
            f"{host_id} cannot be asked for {spec.name}: it does not hold the "
            f"{spec.capability} capability",
            status=409,
        )
    params = catalog.coerce_parameters(spec, body.parameters, capabilities=entry.capabilities)
    return spec, params


def _preview(spec: Operation, params: dict[str, Any], host_id: str) -> dict[str, object]:
    plan = spec.plan(host_id, params)
    return {
        "operation": spec.name,
        "label": spec.label,
        "parameters": {key: _displayable(value) for key, value in params.items()},
        "plan": plan.to_json(),
        "applies": spec.applies(params),
        "mutating": catalog.required_confirmation(spec, params) is not Confirmation.NONE,
        "confirmation": str(catalog.required_confirmation(spec, params)),
    }


def _require_confirmation(required: Confirmation, confirm: Confirm, host_id: str) -> None:
    if required is Confirmation.NONE:
        return
    if not confirm.acknowledge:
        raise ConsoleError(
            "this operation mutates the Host and was not acknowledged",
            status=428,
        )
    if required is Confirmation.TYPED_HOST_ID and (confirm.host_id or "").strip() != host_id:
        raise ConsoleError(
            f"an irreversible operation needs this Host's id typed out: {host_id}",
            status=428,
        )


def _operations(
    registry: HostRegistry, host_id: str, capabilities: frozenset[Capability]
) -> list[dict[str, object]]:
    suggestions = registry.suggestions(host_id)
    document: list[dict[str, object]] = []
    for spec in catalog.available(capabilities):
        fields = []
        for item in spec.fields:
            if item.capability is not None and item.capability not in capabilities:
                continue
            suffix = _ARTIFACTS.get((spec.name, item.name))
            default = (
                registry.artifact_default(host_id, spec.name, suffix)
                if suffix is not None
                else item.default
            )
            fields.append(
                {
                    "name": item.name,
                    "kind": str(item.kind),
                    "label": item.label,
                    "help": item.help,
                    "required": item.required,
                    "default": default,
                    "choices": list(item.choices),
                    "minimum": item.minimum,
                    "maximum": item.maximum,
                    "gate": item.gate,
                    "suggestions": suggestions.get(item.name, []),
                }
            )
        document.append(
            {
                "name": spec.name,
                "label": spec.label,
                "summary": spec.summary,
                "group": spec.group,
                "capability": str(spec.capability),
                "fields": fields,
                "sensitive_keys": list(spec.sensitive_keys),
            }
        )
    return document


def _readiness(profile: object) -> dict[str, str]:
    driver = getattr(profile, "driver", None)
    if driver is None:
        return {}
    kind = HostKind.SOURCE if driver is HostDriver.LOCAL_SUPERVISORD else HostKind.PRODUCT
    return describe(kind)


def _displayable(value: object) -> object:
    return str(value) if isinstance(value, Path) else value


def _sse(runs: RunStore, run_id: str) -> Iterator[str]:
    for event in runs.stream(run_id):
        if event is None:
            # A release phase can be silent for twenty minutes, which is longer
            # than anything between here and the browser will hold open.
            yield ": keepalive\n\n"
            continue
        payload = json.dumps(event.to_json(), default=str, ensure_ascii=False)
        yield f"event: {event.kind}\ndata: {payload}\n\n"


# -- the page -----------------------------------------------------------------


def _mount_console(app: FastAPI) -> None:
    """Serve the built console, or say plainly that it has not been built.

    The bundle is a build artifact and is not committed, so the honest failure
    is a page that names the two commands that produce it. A blank 404 would
    read as a broken console.
    """

    index = STATIC_ROOT / "index.html"
    assets = STATIC_ROOT / "assets"
    if assets.is_dir():
        app.mount("/assets", _HashedAssets(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def page(path: str) -> HTMLResponse:
        if path.startswith("api/"):
            raise ConsoleError(f"no such endpoint: /{path}", status=404)
        if not index.is_file():
            return HTMLResponse(_UNBUILT, status_code=503, headers=_NO_STORE)
        return HTMLResponse(index.read_text(encoding="utf-8"), headers=_NO_STORE)


class _HashedAssets(StaticFiles):
    """Assets whose file names already contain their own content hash.

    A new build is a new name, so there is nothing to revalidate and no way to
    serve a stale one.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = _IMMUTABLE
        return response
