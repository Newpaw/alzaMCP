import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastmcp import Client
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from alza_mcp.client import ENDPOINTS, AlzaClient, AlzaError
from alza_mcp.config import Settings
from alza_mcp.server import BearerGuard, create_server


def settings(**kwargs):
    return Settings(
        _env_file=None, alza_client_id="test-client", alza_client_secret="secret", **kwargs
    )


@pytest.mark.parametrize(
    ("tool", "args", "path", "query"),
    [
        ("get_languages", {}, "/v2/languages", {}),
        ("get_categories", {"parent_id": 10}, "/v2/listings/categories", {"parentId": ["10"]}),
        ("get_category_tree", {}, "/v2/listings/categories/tree", {}),
        ("get_manufacturers", {}, "/v2/listings/manufacturers", {}),
        (
            "get_parameters",
            {"category_ids": [1, 2]},
            "/v2/listings/parameters",
            {"catId": ["1", "2"]},
        ),
        ("get_seo_prefixes", {}, "/v2/listings/seo-prefixes", {}),
        (
            "get_listing_templates",
            {"category_ids": [1]},
            "/v2/listings/templates",
            {"catId": ["1"]},
        ),
        (
            "get_orders",
            {
                "states": [1, 7],
                "page": 2,
                "page_size": 20,
                "sort_desc": False,
                "sort_key": 0,
                "created_from": "2026-10-01T00:00:00+02:00",
                "created_to": "2026-10-02T00:00:00+02:00",
                "shipment_departure_time_from": "2026-10-03T00:00:00Z",
                "shipment_departure_time_to": "2026-10-04T00:00:00Z",
            },
            "/v2/orders",
            {
                "state": ["1", "7"],
                "page": ["2"],
                "pageSize": ["20"],
                "sortDesc": ["false"],
                "sortKey": ["0"],
                "createdFrom": ["2026-10-01T00:00:00+02:00"],
                "createdTo": ["2026-10-02T00:00:00+02:00"],
                "shipmentDepartureTimeFrom": ["2026-10-03T00:00:00+00:00"],
                "shipmentDepartureTimeTo": ["2026-10-04T00:00:00+00:00"],
            },
        ),
        ("get_unshipped_orders", {}, "/v2/orders/unshipped", {}),
    ],
)
async def test_all_tools_mapping(tool, args, path, query):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/connect/token":
            form = parse_qs(request.content.decode())
            assert form == {
                "client_id": ["test-client"],
                "client_secret": ["secret"],
                "grant_type": ["client_credentials"],
                "scope": ["marketplace_public"],
            }
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 3600})
        assert request.method == "GET"
        assert request.url.path == path
        assert parse_qs(request.url.query.decode()) == query
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["Accept-Language"] == "en"
        return httpx.Response(200, json={"items": [{"id": 1}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with Client(create_server(settings(), http)) as client:
            result = await client.call_tool(tool, {**args, "language": "en"})
            assert not result.is_error
            await client.call_tool(tool, {**args, "language": "en"})
    assert sum(r.url.path == "/connect/token" for r in requests) == 1


async def test_exact_swagger_coverage_and_metadata():
    spec = json.loads((Path(__file__).parents[1] / "spec/alza-v2.json").read_text())
    assert set(spec["paths"]) == ENDPOINTS
    assert all(set(ops) == {"get"} for ops in spec["paths"].values())
    async with Client(create_server(Settings(_env_file=None))) as client:
        tools = await client.list_tools()
        assert len(tools) == 9
        assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools)
        resources = await client.read_resource("alza://api-info")
        assert "marketplace_public" in resources[0].text


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_listing_templates", {"category_ids": []}),
        ("get_parameters", {"category_ids": list(range(1, 12))}),
        ("get_parameters", {"category_ids": [-1]}),
        ("get_categories", {"parent_id": 0}),
        ("get_languages", {"language": "en\r\nAuthorization: x"}),
        ("get_orders", {"page_size": 501}),
        ("get_orders", {"page": 0}),
        ("get_orders", {"states": [14]}),
        ("get_orders", {"sort_key": 4}),
        ("get_orders", {"created_from": "2026-10-01T00:00:00"}),
        (
            "get_orders",
            {"created_from": "2026-10-02T00:00:00Z", "created_to": "2026-10-01T00:00:00Z"},
        ),
    ],
)
async def test_validation_before_network(tool, args):
    def handler(request):
        pytest.fail("Invalid arguments must not reach upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with Client(create_server(settings(), http)) as client:
            result = await client.call_tool(tool, args, raise_on_error=False)
            assert result.is_error


async def test_missing_credentials_is_safe_tool_error():
    async with Client(create_server(Settings(_env_file=None))) as client:
        result = await client.call_tool("get_languages", {}, raise_on_error=False)
        assert result.is_error
        assert "ALZA_CLIENT_ID" in result.content[0].text


async def test_concurrent_token_requests_share_refresh():
    count = 0

    async def handler(request):
        nonlocal count
        if request.url.path == "/connect/token":
            count += 1
            await asyncio.sleep(0)
            return httpx.Response(200, json={"access_token": "shared", "expires_in": 3600})
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AlzaClient(settings(), http)
        await asyncio.gather(*(client.get("/v2/languages") for _ in range(20)))
    assert count == 1


async def test_401_refreshes_once():
    tokens, calls = 0, 0

    def handler(request):
        nonlocal tokens, calls
        if request.url.path == "/connect/token":
            tokens += 1
            return httpx.Response(200, json={"access_token": f"token-{tokens}", "expires_in": 60})
        calls += 1
        if calls == 1:
            return httpx.Response(401)
        assert request.headers["authorization"] == "Bearer token-2"
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await AlzaClient(settings(), http).get("/v2/languages") == {"ok": True}
    assert tokens == calls == 2


@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_errors_never_leak_upstream_body(status):
    def handler(request):
        if request.url.path == "/connect/token":
            return httpx.Response(200, json={"access_token": "secret-token", "expires_in": 60})
        return httpx.Response(status, text="secret-token private customer data")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(AlzaError) as error:
            await AlzaClient(settings(alza_max_retries=0), http).get("/v2/languages")
    assert str(status) in str(error.value)
    assert "private" not in str(error.value)
    assert "secret-token" not in str(error.value)


async def test_transient_retry(monkeypatch):
    calls = 0

    async def no_sleep(delay):
        assert delay <= 5

    monkeypatch.setattr("alza_mcp.client.asyncio.sleep", no_sleep)

    def handler(request):
        nonlocal calls
        if request.url.path == "/connect/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        calls += 1
        if calls < 3:
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await AlzaClient(settings(), http).get("/v2/languages") == []
    assert calls == 3


async def test_response_size_limit_and_no_arbitrary_paths():
    def handler(request):
        if request.url.path == "/connect/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        return httpx.Response(200, content=b" " * 2048)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AlzaClient(settings(alza_max_response_bytes=1024), http)
        with pytest.raises(AlzaError, match="size limit"):
            await client.get("/v2/languages")
        with pytest.raises(AlzaError, match="Unsupported"):
            await client.get("https://attacker.invalid")


def test_health_readiness_and_bearer_guard():
    app = create_server(Settings(_env_file=None)).http_app(
        path="/mcp",
        stateless_http=True,
        json_response=True,
        middleware=[Middleware(BearerGuard, key="service-secret")],
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        assert client.post("/mcp", json={}).status_code == 401
        assert client.post("/mcp/", json={}).status_code == 401
        response = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer service-secret",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "Alza Trade"


def test_config_rejects_partial_credentials_and_plain_http():
    with pytest.raises(ValueError):
        Settings(_env_file=None, alza_client_id="only-id")
    with pytest.raises(ValueError):
        settings(alza_base_url="http://insecure.invalid")
