"""ASGI app: MCP at /mcp (stateless Streamable HTTP, spec 2026-07-28) plus a JSON API at /v1/*.

Run: `deptime serve`, or `uvicorn --factory deptime.server.http_app:create_app` with DEPTIME_HOST set to the host
uvicorn binds: the MCP transport checks Host/Origin only when that host is loopback.
"""

from __future__ import annotations

from collections.abc import Callable
from inspect import signature
from typing import Any

from pydantic import BaseModel, ValidationError, validate_call
from starlette.requests import Request
from starlette.responses import JSONResponse

from deptime import __version__
from deptime.config import Settings, load_coverage
from deptime.index.distribution import start_background_refresh
from deptime.index.location import open_index
from deptime.query import DepTime
from deptime.server.mcp_app import build_mcp_and_tools
from deptime.server.metering import (
    UNMETERED_TOOLS,
    InMemoryQuotaStore,
    MeteringMiddleware,
    identify,
    quota_message,
)


def load_deptime(settings: Settings) -> DepTime:
    coverage = load_coverage(settings.coverage_file)
    return DepTime(open_index(settings.index_path), coverage, settings.index_path)


def rest_calls(
    tools: dict[str, Callable[..., BaseModel]],
) -> dict[str, tuple[Callable[..., BaseModel], frozenset[str]]]:
    """Each MCP tool function as the REST API calls it: its arguments checked against the same types and limits as
    an MCP call (D5), and the names it takes, so a body's other keys are left out as before."""
    return {name: (validate_call(fn), frozenset(signature(fn).parameters)) for name, fn in tools.items()}


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    dt = load_deptime(settings)
    start_background_refresh(dt, settings)  # downloads/refreshes the index without blocking start-up
    quota = InMemoryQuotaStore(settings)  # anonymous rate limits for a public instance (DT-HOST-03)
    mcp, tools = build_mcp_and_tools(
        dt, middleware=[MeteringMiddleware(quota, settings)], website_url=settings.public_url
    )
    calls = rest_calls(tools)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "version": __version__,
                "index_loaded": dt.store is not None,
                "index_built_at": dt.store.built_at() if dt.store else None,
            }
        )

    @mcp.custom_route("/v1/{tool}", methods=["GET", "POST"])
    async def rest(request: Request) -> JSONResponse:
        tool = request.path_params["tool"].replace("-", "_")
        if tool not in UNMETERED_TOOLS:
            caller = identify(request.headers, request.client.host if request.client else None, settings)
            decision = quota.consume(caller)
            if not decision.allowed:
                return JSONResponse(
                    {"error": "quota_exceeded", "message": quota_message(caller, decision, settings)},
                    status_code=429,
                )
        body: Any = {}
        if request.method == "POST":
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"error": "invalid_json"}, status_code=400)
        if tool not in calls:
            return JSONResponse({"error": "bad_request", "missing_or_unknown": repr(tool)}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "bad_request", "detail": "the body must be a JSON object"}, status_code=400
            )
        call, params = calls[tool]
        try:
            result = call(**{k: v for k, v in body.items() if k in params})
        except ValidationError as exc:
            # Without the input (a whole traceback, say) or the context (it can hold an exception object).
            detail = exc.errors(include_url=False, include_input=False, include_context=False)
            return JSONResponse({"error": "bad_request", "detail": detail}, status_code=400)
        return JSONResponse(result.model_dump(mode="json"))

    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=settings.host,
    )
