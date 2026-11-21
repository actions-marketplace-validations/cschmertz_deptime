from dataclasses import replace
from types import SimpleNamespace

import pytest
from mcp import Client
from starlette.testclient import TestClient

from deptime import cli
from deptime.server.http_app import create_app
from deptime.server.mcp_app import build_mcp
from deptime.server.metering import InMemoryQuotaStore, MeteringMiddleware, identify
from deptime.tracebacks import MAX_INPUT_CHARS
from tests.test_tracebacks import HTTPX_TB


@pytest.mark.anyio
async def test_mcp_tools_listed_and_callable(dt):
    mcp = build_mcp(dt)
    async with Client(mcp) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools.tools} == {
            "explain_error",
            "symbol_history",
            "check_symbols",
            "diff_versions",
            "check_upgrade",
            "coverage",
        }
        assert all(t.annotations and t.annotations.read_only_hint for t in tools.tools)
        result = await client.call_tool(
            "explain_error", {"traceback": HTTPX_TB, "installed_versions": {"httpx": "0.28.1"}}
        )
        assert not result.is_error
        assert result.structured_content["findings"][0]["issue"] == "param_removed"
        result = await client.call_tool(
            "check_upgrade",
            {
                "package": "httpx",
                "from_version": "0.27.2",
                "to_version": "0.28.1",
                "files": {"app.py": "import httpx\nhttpx.Client(proxies='http://p')\n"},
            },
        )
        assert not result.is_error and result.structured_content["breaks"] == 1


@pytest.mark.anyio
async def test_every_tool_parameter_is_described(dt):
    # Agents read parameter intent from the schema (Glama's TDQS grades it too); a bare name is a guess.
    async with Client(build_mcp(dt)) as client:
        tools = (await client.list_tools()).tools
    undescribed = [
        f"{t.name}.{name}"
        for t in tools
        for name, schema in t.input_schema.get("properties", {}).items()
        if not schema.get("description")
    ]
    assert undescribed == []


@pytest.mark.anyio
async def test_mcp_explain_error_accepts_a_long_log(dt):
    # Q-02: a whole CI log ending in the traceback must reach the parser, not fail input validation.
    log = "collected test output line\n" * 1200 + HTTPX_TB
    assert len(log) > MAX_INPUT_CHARS
    async with Client(build_mcp(dt)) as client:
        result = await client.call_tool(
            "explain_error", {"traceback": log, "installed_versions": {"httpx": "0.28.1"}}
        )
    assert not result.is_error
    assert result.structured_content["findings"][0]["symbol"] == "httpx.Client.__init__(proxies)"


def _ctx(tool: str, ip: str = "1.2.3.4", headers: dict | None = None):
    request = SimpleNamespace(headers=headers or {}, client=SimpleNamespace(host=ip))
    return SimpleNamespace(method="tools/call", params={"name": tool, "arguments": {}}, request=request)


@pytest.mark.anyio
async def test_metering_blocks_after_anonymous_limit(settings):
    mw = MeteringMiddleware(InMemoryQuotaStore(settings), settings)  # anon limit = 2 in the fixture

    async def ok(ctx):
        return {"content": [{"type": "text", "text": "ok"}]}

    assert (await mw(_ctx("explain_error"), ok))["content"][0]["text"] == "ok"
    assert (await mw(_ctx("explain_error"), ok))["content"][0]["text"] == "ok"
    blocked = await mw(_ctx("explain_error"), ok)
    # The limit message must point people at unlimited local use.
    assert blocked.is_error is True and "uvx deptime mcp" in blocked.content[0].text
    assert blocked.model_dump(by_alias=True)["resultType"] == "complete"  # required by spec 2026-07-28
    assert (await mw(_ctx("coverage"), ok))["content"][0]["text"] == "ok"  # unmetered tool
    assert (await mw(_ctx("explain_error", headers={"authorization": "Bearer trusted-key"}), ok))["content"][
        0
    ]["text"] == "ok"


def test_http_health_and_rest(settings):
    with TestClient(create_app(settings)) as http:
        health = http.get("/healthz").json()
        assert health["ok"] and health["index_loaded"]
        body = http.post(
            "/v1/explain-error", json={"traceback": HTTPX_TB, "installed_versions": {"httpx": "0.28.1"}}
        ).json()
        assert body["findings"][0]["symbol"] == "httpx.Client.__init__(proxies)"
        assert http.get("/v1/coverage").json()["packages"][0]["package"] == "httpx"
        assert http.post("/v1/nope", json={}).status_code == 400


def test_rest_check_upgrade(settings):
    with TestClient(create_app(settings)) as http:
        up = http.post(
            "/v1/check-upgrade",
            json={
                "package": "httpx",
                "from_version": "0.27.2",
                "code": "import httpx\nhttpx.Client(proxies=1)\n",
            },
        ).json()
        assert up["breaks"] == 1 and up["checked"][0]["change"]["to_version"] == "0.28.1"


@pytest.mark.anyio
async def test_spoofed_forwarded_for_does_not_bypass_the_ip_limit(settings):
    # S-01: proxies append the real client, so a client-chosen left-most hop must not reset its quota.
    trusted = replace(settings, trust_proxy_headers=True)
    mw = MeteringMiddleware(InMemoryQuotaStore(trusted), trusted)  # anon limit = 2

    async def ok(ctx):
        return {"content": [{"type": "text", "text": "ok"}]}

    results = []
    for i in range(4):
        headers = {"x-forwarded-for": f"10.0.0.{i}, 203.0.113.7"}  # fake hop, then what the proxy saw
        results.append(await mw(_ctx("explain_error", ip="172.16.0.1", headers=headers), ok))
    assert [isinstance(r, dict) for r in results] == [True, True, False, False]

    proxy_ip = "172.16.0.1"
    two_hops = replace(trusted, trusted_proxy_hops=2)
    assert identify({"x-forwarded-for": "6.6.6.6, 203.0.113.7, 10.1.1.1"}, proxy_ip, two_hops) == identify(
        {"x-forwarded-for": "203.0.113.7, 10.1.1.1"}, proxy_ip, two_hops
    )
    fly = replace(trusted, client_ip_header="Fly-Client-IP")
    fly_headers = {"fly-client-ip": "203.0.113.7", "x-forwarded-for": "6.6.6.6"}
    assert identify(fly_headers, proxy_ip, fly) == identify(
        {"x-forwarded-for": "203.0.113.7"}, proxy_ip, trusted
    )
    # Headers are ignored entirely unless proxy headers are trusted.
    assert identify(fly_headers, proxy_ip, settings) == identify({}, proxy_ip, settings)


def _served_app(monkeypatch, settings, host: str | None):
    """The app `deptime serve --host <host>` would hand to uvicorn, and the address it would bind."""
    import uvicorn

    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: started.update(app=app, host=host, port=port))
    cli.cmd_serve(SimpleNamespace(host=host, port=None), settings)
    return started["app"], started["host"]


def _mcp_statuses(app, *host_headers: str) -> list[int]:
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    with TestClient(app) as http:  # an app's session manager starts once
        return [
            http.post(
                "/mcp", json=initialize, headers={"Host": h, "Accept": "application/json, text/event-stream"}
            ).status_code
            for h in host_headers
        ]


def test_serve_host_also_sets_which_host_names_mcp_answers(monkeypatch, settings):
    """D3: `--host` reached uvicorn but not the MCP transport's Host/Origin check, which follows the configured
    host. Bound to every interface, /mcp refused every request not addressed to localhost (421); bound to loopback
    with DEPTIME_HOST=0.0.0.0, it ran without the check that stops DNS rebinding."""
    app, bound = _served_app(monkeypatch, settings, "0.0.0.0")  # the fixture's DEPTIME_HOST is 127.0.0.1
    assert bound == "0.0.0.0" and _mcp_statuses(app, "deptime.example.com") == [200]

    app, bound = _served_app(monkeypatch, replace(settings, host="0.0.0.0"), "127.0.0.1")
    assert bound == "127.0.0.1" and _mcp_statuses(app, "rebind.example.com", "127.0.0.1:8000") == [421, 200]

    app, bound = _served_app(monkeypatch, settings, None)  # no --host: the setting
    assert bound == "127.0.0.1" and _mcp_statuses(app, "rebind.example.com") == [421]


def test_rest_rejects_a_malformed_body_with_400(settings):
    """D5: a body of the wrong shape crashed the handler (500, with a stack trace in the server log); the REST API
    now checks it against the same parameter types and limits as the MCP tools."""
    bad = [
        ("explain-error", [HTTPX_TB]),  # not an object
        ("explain-error", {"traceback": 5}),
        ("explain-error", {"traceback": HTTPX_TB, "installed_versions": ["httpx==0.28.1"]}),
        ("explain-error", {"traceback": HTTPX_TB, "min_confidence": "certain"}),
        ("explain-error", {}),
        ("diff-versions", {"package": "httpx", "from_version": "0.27.2", "limit": None}),
        ("diff-versions", {"package": "httpx", "from_version": "0.27.2", "limit": 10_000}),
        ("check-symbols", {"package": "httpx", "version": "0.28.1", "symbols": 5}),
        ("check-upgrade", {"package": "httpx", "from_version": "0.27.2", "files": {"a.py": 1}}),
        ("symbol-history", {"package": ["httpx"], "path": "httpx.Client"}),
    ]
    with TestClient(create_app(replace(settings, anon_daily_limit=100))) as http:
        for tool, body in bad:
            r = http.post(f"/v1/{tool}", json=body)
            assert r.status_code == 400 and r.json()["error"] == "bad_request", (tool, body, r.text)
        # Names the tools don't take are still ignored, and the values the MCP tools accept still work.
        r = http.post(
            "/v1/diff-versions",
            json={
                "package": "httpx",
                "from_version": "0.27.2",
                "limit": "5",
                "include_added": "false",
                "x": 1,
            },
        )
        assert r.status_code == 200 and r.json()["to_version"] == "0.28.1"
        r = http.post(
            "/v1/check-symbols",
            json={"package": "httpx", "version": "0.28.1", "symbols": [{"path": "httpx.Client"}]},
        )
        assert r.status_code == 200 and r.json()["results"][0]["status"] == "present"
