# Alza Trade MCP server

A Python 3.12 MCP server covering **all 9 endpoints** in the [Alza Trade API v2 Swagger](https://portalapi.alza.cz/swagger/index.html). Built with **FastMCP 3**, **httpx**, **uv**, Docker and GitHub Actions. Remote clients use **Streamable HTTP** at `/mcp`; local clients can use stdio.

> This is the Alza **partner / marketplace** API. It exposes listing metadata and partner orders. It does **not** expose retail product search, retail prices, customer shopping carts or checkout. A normal alza.cz login is not sufficient. This is an independent integration, not an official Alza product.

## Quick start

```bash
git clone https://github.com/Newpaw/alzaMCP.git
cd alzaMCP
cp .env.example .env
# Edit .env: set ALZA_CLIENT_ID, ALZA_CLIENT_SECRET, and MCP_API_KEY.
docker compose up -d --build
curl http://127.0.0.1:8019/healthz
curl http://127.0.0.1:8019/readyz
```

Alternatively, use the published image without building locally:

```bash
docker compose pull
docker compose up -d --no-build
```

Image: `ghcr.io/newpaw/alzamcp:latest`. Release tags such as `v0.1.0` produce image tags `0.1.0` and `0.1`; every build also publishes `sha-<full-commit-sha>`. Pin a SHA tag or digest for controlled deployments.

Without Alza credentials the process still starts: `/healthz` is 200, `/readyz` is 503, MCP discovery works and upstream tools report a clear configuration error. This is useful for deployment checks; it does not mean live Alza data has been verified.

## Authentication and configuration

Alza requires OAuth2 **client credentials**, with token endpoint `https://identity.alza.cz/connect/token` and scope `marketplace_public`. Obtain a client ID and secret with access to this API from Alza Trade. The server exchanges them using a form-encoded token request, caches the access token in memory and refreshes it before expiration. One 401 triggers one forced token renewal. Credentials and tokens are never exposed as tools, resources or response errors.

The MCP service key and the Alza client secret serve different purposes:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALZA_CLIENT_ID` | empty | Partner application client ID; set with its secret |
| `ALZA_CLIENT_SECRET` | empty | Partner application client secret |
| `ALZA_SCOPE` | `marketplace_public` | Upstream OAuth scope from the Swagger |
| `ALZA_LANGUAGE` | `cs` | Default `Accept-Language`; tools can override it |
| `ALZA_BASE_URL` | `https://portalapi.alza.cz` | Trusted upstream API URL; HTTPS required |
| `ALZA_TOKEN_URL` | `https://identity.alza.cz/connect/token` | Trusted identity URL; HTTPS required |
| `ALZA_TIMEOUT_SECONDS` | `20` | Per-operation HTTP timeout, including reads; connect timeout is 5 s |
| `ALZA_MAX_RETRIES` | `2` | Additional GET attempts on connection errors, 429, 502, 503, 504 |
| `ALZA_MAX_RESPONSE_BYTES` | `20000000` | Maximum decoded API response size |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | Container listener |
| `MCP_TRANSPORT` | `http` | `http` or `stdio` |
| `MCP_API_KEY` | empty | Optional bearer key for `/mcp`; required by the Komodo compose file |
| `MCP_PUBLISHED_PORT` | `8019` | Local compose loopback port |
| `IMAGE_TAG` | `latest` | Image tag used by compose |

Settings can be supplied through environment variables or a local `.env` file. Never commit `.env`. Application secrets belong in Komodo's environment configuration, not in GitHub Actions. The container build does not need any Alza credentials.

Generate a service key:

```bash
uv run python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

With `MCP_API_KEY` set, send `Authorization: Bearer <key>` on each MCP request. This is service-key authentication, **not an OAuth authorization server**. Interactive OAuth-only clients need authentication at an appropriate external gateway. Do not send the Alza client secret to an MCP client. Stdio runs locally and does not use the HTTP bearer key.

## Supported tools

Responses retain the original Alza JSON structure, including pagination metadata. No fields are silently renamed or discarded. Date filters use RFC 3339 timestamps with an explicit offset, for example `2026-10-26T00:00:00+01:00`. Each tool accepts an optional `language` override. Category identifiers must be positive; category filters accept 1–10 identifiers. Arrays are sent as repeated query parameters, matching OpenAPI's default `form`/`explode` query encoding.

| MCP tool | Alza endpoint | Filters |
| --- | --- | --- |
| `get_languages` | `GET /v2/languages` | language |
| `get_categories` | `GET /v2/listings/categories` | `parent_id`, language |
| `get_category_tree` | `GET /v2/listings/categories/tree` | `parent_id`, language |
| `get_manufacturers` | `GET /v2/listings/manufacturers` | language |
| `get_parameters` | `GET /v2/listings/parameters` | optional `category_ids`, language |
| `get_seo_prefixes` | `GET /v2/listings/seo-prefixes` | optional `category_ids`, language |
| `get_listing_templates` | `GET /v2/listings/templates` | required `category_ids`, language |
| `get_orders` | `GET /v2/orders` | creation dates, shipment departure dates, states, pagination, sort, language |
| `get_unshipped_orders` | `GET /v2/orders/unshipped` | shipment departure dates, language |

`get_orders` parameters: `created_from`, `created_to`, `shipment_departure_time_from`, `shipment_departure_time_to`, `states`, `page` (default 1), `page_size` (default 50, max 500), `sort_desc`, `sort_key`, `language`. Call it repeatedly with successive pages when required; the server deliberately does not download all orders automatically. The Swagger does not declare the upstream default or minimum page number; this integration uses the conventional 1-based page number, which should be confirmed with your live account.

State values: `0` Pending, `1` LabelNotGenerated, `2` UnShipped, `3` Shipped, `4` Canceled, `5` CanceledAfterShipping, `6` Delivered, `7` Delayed, `8` DelayedRepeatedly, `9` ShippedLate, `10` CanceledLate, `11` DeliveredLate, `12` AwaitingCopayAmount, `13` AwaitingPreparation. **Alza includes states 7 and 8 when filtering for state 1 or 2.**

Sort keys: `0` Id, `1` Created, `2` State, `3` ShipmentDepartureTime.

`get_parameters` without a category filter may time out according to Alza's documentation. Narrow the filter rather than repeatedly downloading the complete collection. Templates omit unknown category identifiers, as documented by Alza. Very large responses fail explicitly at the configured size limit; no result is silently truncated.

The MCP resource `alza://api-info` describes capabilities without exposing configuration secrets. Every tool advertises read-only, non-destructive, idempotent annotations. All tools operate on the **same partner identity** configured on the server: deploy separate instances for different partners. This is not a multi-tenant user delegation system.

## Connect an MCP client

Remote endpoint after the tunnel is configured: `https://alzamcp.novopacky.com/mcp`.

Example client configuration for clients supporting Streamable HTTP and custom headers (the exact configuration location varies):

```json
{
  "mcpServers": {
    "alza": {
      "type": "http",
      "url": "https://alzamcp.novopacky.com/mcp",
      "headers": {"Authorization": "Bearer <MCP_API_KEY>"}
    }
  }
}
```

Python discovery and live API check:

```python
import asyncio
import os
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main():
    transport = StreamableHttpTransport(
        "https://alzamcp.novopacky.com/mcp",
        headers={"Authorization": f"Bearer {os.environ['MCP_API_KEY']}"},
    )
    async with Client(transport) as client:
        print([tool.name for tool in await client.list_tools()])
        print(await client.call_tool("get_languages", {"language": "cs"}))
        print(await client.call_tool("get_orders", {"page": 1, "page_size": 10}))


asyncio.run(main())
```

For a local stdio client, set environment variables including `MCP_TRANSPORT=stdio` and run `uv run --locked alza-mcp` in the repository. A Docker stdio client can run `docker run --rm -i --env-file .env -e MCP_TRANSPORT=stdio ghcr.io/newpaw/alzamcp:latest`. Do not add `-t`; MCP stdio needs a clean byte stream.

## Komodo and Cloudflare Tunnel

Use `compose.komodo.yaml` as the stack contents, select the target server and configure `MCP_API_KEY`, `ALZA_CLIENT_ID` and `ALZA_CLIENT_SECRET` in the stack environment. The external Docker network `cloudflare` must already exist, and the existing cloudflared connector must be attached to it. The service name / network alias is `alza-mcp`, listener port 8000. No host port is published.

Route the chosen hostname through the existing tunnel to `http://alza-mcp:8000`. TLS terminates at the tunnel edge. The application uses stateless Streamable HTTP with JSON responses, so there is no sticky session requirement. Proxy headers do not become application identity.

**Cloudflare Zero Trust / Access is intentionally not implemented or provisioned by this project.** There is no Cloudflare SDK, token, JWT validation, Access policy, OAuth gateway or CORS workaround in the application. The optional service bearer guard works independently. If an external Access policy is later added, configure the MCP client to satisfy that policy as well. A browser login redirect is not sufficient for a machine client. Check your client's support for Access service-token headers or an external OAuth gateway before enabling such a policy. Do not remove the service key until an equivalent access boundary is in place.

## Operations and updates

* `GET /healthz`: unauthenticated liveness, 200 when running. Docker health checks use this.
* `GET /readyz`: unauthenticated configuration readiness, 200 with both Alza credentials or 503 without them. **It does not contact Alza or prove that credentials work.**
* Verify upstream access by calling `get_languages` after configuring credentials.
* Logs omit HTTP access logs and upstream response bodies. Never enable HTTP debug logging in production.
* OAuth tokens live only in memory and are renewed after a process restart. There is no database, persistent token storage or customer-data cache.
* Retry delay uses jitter and `Retry-After` (seconds or HTTP date), capped at 5 s. Only GET requests retry automatically. A token request is not retried; a subsequent tool call can retry it.
* Up to 8 concurrent upstream operations are allowed. Each response is streamed into a bounded buffer and parsed only after completion. A multi-attempt request can take longer than one configured HTTP timeout; set the MCP client timeout accordingly (for example 120 s).
* Runtime runs as UID/GID 10001, with a read-only filesystem, dropped Linux capabilities, no-new-privileges and a bounded temporary filesystem. Compose sets CPU and memory limits.

Pushes to `main` and version tags run lint, format checks, tests and Docker smoke tests before publishing AMD64/ARM64 images to GHCR. PRs only run checks. The workflow needs only the built-in `GITHUB_TOKEN`, with `packages: write` granted to the publish job. The package must be **public** for anonymous pulls; GitHub may initially create it as private even when the repository is public. If needed, change the package visibility in GitHub Packages settings or configure Komodo with a registry account authorized to pull it.

In Komodo, enable image polling and auto-update to redeploy `latest` after publication. For a manual update, pull and redeploy the stack. For rollback, set `IMAGE_TAG=sha-<previous-good-commit>` and redeploy; disable automatic image updates while pinned. Tag pushes alone do not guarantee a deployment if you run a pinned SHA.

```bash
docker compose logs --tail 100 alza-mcp
docker compose pull
docker compose up -d --no-build
# Roll back to a previously published SHA:
IMAGE_TAG=sha-<previous-good-commit> docker compose up -d --no-build
```

## Development and verification

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
uv run --locked alza-mcp
```

The committed `uv.lock` fixes dependency versions and hashes. Build-time installation uses uv, never pip. The multi-stage image contains only the runtime environment, not uv or development tools. Dependabot tracks uv, Docker and Actions updates.

Tests cover every endpoint through an in-process MCP client, exact query/header encoding, token caching and concurrent refresh, 401 renewal, transient retries, sanitized errors, response-size limits, invalid inputs, health/readiness and actual HTTP JSON-RPC initialization with service-key enforcement. CI also boots the built non-root container before pushing it. These checks do not replace a live Alza account integration test.

The fetched Swagger snapshot is in `spec/alza-v2.json`; it is a reference contract owned by Alza, excluded from the runtime image. The coverage test fails when the snapshot and supported endpoint list diverge. To adopt upstream API changes, refresh the snapshot from `https://portalapi.alza.cz/swagger/v2/swagger.json`, review the diff, update tools/tests, and run all checks. The server does not download or execute a changing remote specification at startup.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `/healthz` is 200, `/readyz` is 503 | Set both partner credentials in Komodo and redeploy |
| MCP HTTP 401 | Send the MCP service key; this is separate from the Alza credentials |
| Tool reports OAuth 400/401 | Check client ID, secret, grant permissions and `marketplace_public` scope |
| Tool reports API 403 | Ask Alza to verify endpoint access for the partner client |
| HTML or redirect instead of MCP JSON | Inspect the external gateway / Access policy; do not disable TLS verification |
| Tunnel 502 | Check container health, Docker network membership and `http://alza-mcp:8000` origin |
| GHCR unauthorized | Make the package public or configure a Komodo registry pull credential |
| 408, timeout or response-size limit | Narrow category/date filters and lower order page size |
| Empty orders on page 1 | Confirm upstream pagination semantics and partner permissions with Alza |

## Sources

* [Alza Trade Swagger](https://portalapi.alza.cz/swagger/index.html) and [OpenAPI JSON](https://portalapi.alza.cz/swagger/v2/swagger.json)
* [FastMCP documentation](https://gofastmcp.com/servers/server)
* [uv Docker integration](https://docs.astral.sh/uv/guides/integration/docker/)

Application code is Apache-2.0 licensed, preserving the repository's original license. The upstream API contract and trademarks remain their respective owners' property.
