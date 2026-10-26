"""Bounded, asynchronous Alza API client with OAuth client credentials."""

import asyncio
import json
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from alza_mcp.config import Settings

ENDPOINTS = frozenset(
    {
        "/v2/languages",
        "/v2/listings/categories",
        "/v2/listings/categories/tree",
        "/v2/listings/manufacturers",
        "/v2/listings/parameters",
        "/v2/listings/seo-prefixes",
        "/v2/listings/templates",
        "/v2/orders",
        "/v2/orders/unshipped",
    }
)


class AlzaError(Exception):
    """Safe, actionable error which never contains upstream bodies or secrets."""


def retry_delay(value: str | None, attempt: int) -> float:
    """Respect numeric or HTTP-date Retry-After values, capped at five seconds."""
    if value:
        try:
            return max(0, min(float(value), 5))
        except ValueError:
            try:
                return max(
                    0, min((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds(), 5)
                )
            except (ValueError, TypeError, OverflowError):
                pass
    return min(0.5 * 2**attempt + random.uniform(0, 0.2), 5)


class AlzaClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http
        self._token: str | None = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._concurrency = asyncio.Semaphore(8)

    async def token(self) -> str:
        if not self.settings.configured:
            raise AlzaError("Set ALZA_CLIENT_ID and ALZA_CLIENT_SECRET in the server environment.")
        async with self._token_lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            try:
                response = await self.http.post(
                    self.settings.alza_token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.settings.alza_client_id.get_secret_value(),
                        "client_secret": self.settings.alza_client_secret.get_secret_value(),
                        "scope": self.settings.alza_scope,
                    },
                    headers={"Accept": "application/json"},
                )
            except httpx.RequestError as exc:
                raise AlzaError("Alza identity service is unreachable; retry later.") from exc
            if response.status_code != 200:
                raise AlzaError(
                    f"Alza OAuth HTTP {response.status_code}. Check client credentials and scope."
                )
            try:
                payload = response.json()
                token = payload["access_token"]
                ttl = float(payload.get("expires_in", 300))
                if not isinstance(token, str) or not token or ttl <= 0:
                    raise ValueError
            except (ValueError, KeyError, TypeError) as exc:
                raise AlzaError("Alza identity returned an invalid token response.") from exc
            self._token = token
            self._expires_at = time.monotonic() + max(0, ttl - min(30, ttl / 10))
            return token

    async def invalidate(self, rejected_token: str) -> None:
        async with self._token_lock:
            if self._token == rejected_token:
                self._token = None
                self._expires_at = 0

    async def get(
        self, path: str, params: dict[str, Any] | None = None, language: str | None = None
    ) -> Any:
        if path not in ENDPOINTS:
            raise AlzaError("Unsupported Alza API endpoint.")
        pairs = []
        for key, value in (params or {}).items():
            if value is None:
                continue
            for item in value if isinstance(value, list) else [value]:
                pairs.append((key, str(item).lower() if isinstance(item, bool) else str(item)))
        async with self._concurrency:
            return await self._get(path, pairs, language)

    async def _get(self, path: str, pairs: list[tuple[str, str]], language: str | None) -> Any:
        refreshed = False
        attempt = 0
        while True:
            token = await self.token()
            try:
                async with self.http.stream(
                    "GET",
                    self.settings.alza_base_url.rstrip("/") + path,
                    params=pairs,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "Accept-Language": language or self.settings.alza_language,
                    },
                ) as response:
                    status = response.status_code
                    if status == 401 and not refreshed:
                        await self.invalidate(token)
                        refreshed = True
                        continue
                    if status in {429, 502, 503, 504} and attempt < self.settings.alza_max_retries:
                        delay = retry_delay(response.headers.get("Retry-After"), attempt)
                    elif status >= 300:
                        hints = {
                            400: "Check the supplied filters.",
                            401: "Token rejected after refresh; check credentials.",
                            403: "Client lacks permission for this endpoint or scope.",
                            408: "Narrow category filters to reduce API execution time.",
                            429: "Rate limit reached; retry later.",
                        }
                        raise AlzaError(
                            f"Alza API HTTP {status}. " + hints.get(status, "Retry later.")
                        )
                    else:
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > self.settings.alza_max_response_bytes:
                                raise AlzaError(
                                    "Alza response exceeds the configured size limit. "
                                    "Narrow filters or reduce page_size."
                                )
                        try:
                            return json.loads(body)
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise AlzaError("Alza returned an invalid JSON response.") from exc
            except httpx.RequestError as exc:
                if attempt >= self.settings.alza_max_retries:
                    raise AlzaError("Alza API timed out or is unreachable; retry later.") from exc
                delay = retry_delay(None, attempt)
            await asyncio.sleep(delay)
            attempt += 1
