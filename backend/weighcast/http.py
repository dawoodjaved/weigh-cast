import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import httpx

logger = logging.getLogger(__name__)
# Alias so tests can patch backoff without replacing global asyncio.sleep.
sleep = asyncio.sleep

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Cap wait so a rate-limited upstream fails fast instead of stalling Score the runs.
MAX_RETRY_WAIT_S = 1.0
DEFAULT_TIMEOUT = httpx.Timeout(8.0, connect=3.0)

_shared: httpx.AsyncClient | None = None
_shared_lock = asyncio.Lock()


class UpstreamError(Exception):
    def __init__(self, service: str, message: str) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        # NWS requires an identifying User-Agent; Open-Meteo accepts the same.
        headers={
            "User-Agent": "weighcast/1.0 (https://weighcast.vercel.app; weighcast-support@example.com)",
            "Accept": "application/geo+json, application/json",
        },
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


async def shared_client() -> httpx.AsyncClient:
    """Process-wide client so TLS handshakes to Valhalla/Open-Meteo are reused."""
    global _shared
    if _shared is not None and not _shared.is_closed:
        return _shared
    async with _shared_lock:
        if _shared is None or _shared.is_closed:
            _shared = new_client()
        return _shared


def retry_delay_s(
    response: httpx.Response | None, attempt: int, backoff_s: float
) -> float:
    """Short backoff only — never hold the user for long Retry-After values."""
    delay = backoff_s * 2**attempt
    if response is not None and response.status_code in {429, 503}:
        header = response.headers.get("Retry-After")
        if header:
            try:
                delay = min(delay, float(header))
            except ValueError:
                pass
    return min(delay, MAX_RETRY_WAIT_S)


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: Mapping[str, Any],
    *,
    service: str,
    attempts: int = 2,
    backoff_s: float = 0.2,
) -> httpx.Response:
    for attempt in range(attempts):
        last_try = attempt == attempts - 1
        response: httpx.Response | None = None
        try:
            response = await client.get(url, params=params)
        except httpx.TransportError as exc:
            if last_try:
                raise UpstreamError(service, "could not be reached") from exc
            logger.warning("%s request failed (%s), retrying", service, exc)
        else:
            if response.status_code not in RETRY_STATUSES:
                return response
            if last_try:
                raise UpstreamError(service, f"responded with {response.status_code}")
            logger.warning("%s responded with %s, retrying", service, response.status_code)
        await sleep(retry_delay_s(response, attempt, backoff_s))
    raise AssertionError("unreachable")
