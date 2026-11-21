"""ASGI app: MCP at /mcp (stateless Streamable HTTP, spec 2026-07-28) plus a JSON API at /v1/*.

Run: `deptime serve`, or `uvicorn --factory deptime.server.http_app:create_app` with DEPTIME_HOST set to the host
uvicorn binds: when that host is loopback, every route checks Host/Origin as the MCP transport does (DT-HOST-07).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from inspect import signature
from typing import Any

from mcp.server.transport_security import (
    DEFAULT_MAX_REQUEST_BODY_SIZE,
    TransportSecurityMiddleware,
    TransportSecuritySettings,
)
from pydantic import BaseModel, ValidationError, validate_call
from starlette.concurrency import run_in_threadpool
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

# The loopback hosts, and the hosts and origins allowed there, as `MCPServer.streamable_http_app` sets them for /mcp
LOOPBACK = ("127.0.0.1", "localhost", "::1")
LOOPBACK_ONLY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
)
MAX_BODY_BYTES = DEFAULT_MAX_REQUEST_BODY_SIZE  # /mcp's cap


def load_deptime(settings: Settings) -> DepTime:
    coverage = load_coverage(settings.coverage_file)
    return DepTime(open_index(settings.index_path), coverage, settings.index_path)


def rest_calls(
    tools: dict[str, Callable[..., BaseModel]],
) -> dict[str, tuple[Callable[..., BaseModel], frozenset[str]]]:
    """Each MCP tool function as the REST API calls it: its arguments checked against the same types and limits as
    an MCP call (D5), and the names it takes, so a body's other keys are left out as before."""
    return {name: (validate_call(fn), frozenset(signature(fn).parameters)) for name, fn in tools.items()}


async def _capped_body(request: Request, cap: int = MAX_BODY_BYTES) -> bytes | None:
    """The request body, or None once it is over `cap` bytes: by its Content-Length, or while it is read."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and (len(declared) > 18 or int(declared) > cap):
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > cap:
            return None
    return bytes(body)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    dt = load_deptime(settings)
    start_background_refresh(dt, settings)  # downloads/refreshes the index without blocking start-up
    quota = InMemoryQuotaStore(settings)  # anonymous rate limits for a public instance (DT-HOST-03)
    mcp, tools = build_mcp_and_tools(
        dt, middleware=[MeteringMiddleware(quota, settings)], website_url=settings.public_url
    )
    calls = rest_calls(tools)
    # Served on loopback, a page in the developer's browser must not reach the server, directly or through a rebound
    # host name (D4). Elsewhere the deployment's own host name is not known here, so no host rule applies.
    security = TransportSecurityMiddleware(LOOPBACK_ONLY if settings.host in LOOPBACK else None)

    async def refused(request: Request) -> JSONResponse | None:
        answer = await security.validate_request(request)
        if answer is None:
            return None
        detail = bytes(answer.body).decode()
        return JSONResponse({"error": "forbidden", "detail": detail}, status_code=answer.status_code)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        if (answer := await refused(request)) is not None:
            return answer
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
        if (answer := await refused(request)) is not None:
            return answer
        content_type = request.headers.get("content-type", "")
        if request.method == "POST" and not content_type.lower().startswith("application/json"):
            # What a web page can send without a CORS preflight is form data or text/plain (D4).
            return JSONResponse(
                {
                    "error": "unsupported_media_type",
                    "detail": "send the body as Content-Type: application/json",
                },
                status_code=415,
            )
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
            if (raw := await _capped_body(request)) is None:
                return JSONResponse(
                    {"error": "too_large", "detail": f"the body is over {MAX_BODY_BYTES} bytes"},
                    status_code=413,
                )
            try:
                body = json.loads(raw)
            except (ValueError, RecursionError):  # RecursionError: nested past the decoder's depth
                return JSONResponse({"error": "invalid_json"}, status_code=400)
        if tool not in calls:
            return JSONResponse({"error": "bad_request", "missing_or_unknown": repr(tool)}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "bad_request", "detail": "the body must be a JSON object"}, status_code=400
            )
        call, params = calls[tool]
        try:
            # In a worker thread, as the MCP transport runs a tool, so a slow call doesn't stall the server (D1).
            result = await run_in_threadpool(call, **{k: v for k, v in body.items() if k in params})
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
