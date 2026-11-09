import httpx
import pytest
import respx

from weighcast.http import UpstreamError, get_with_retry, retry_delay_s


def test_retry_delay_caps_long_retry_after() -> None:
    response = httpx.Response(429, headers={"Retry-After": "30"})
    assert retry_delay_s(response, attempt=0, backoff_s=0.2) == 0.2


def test_retry_delay_stays_short() -> None:
    response = httpx.Response(429)
    assert retry_delay_s(response, attempt=1, backoff_s=0.2) == 0.4


@respx.mock
async def test_get_with_retry_recovers_from_one_429() -> None:
    route = respx.get("https://example.test/wx").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with httpx.AsyncClient() as client:
        response = await get_with_retry(
            client, "https://example.test/wx", {}, service="Open-Meteo", backoff_s=0
        )

    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_get_with_retry_fails_fast_after_429s() -> None:
    respx.get("https://example.test/wx").respond(429, headers={"Retry-After": "0"})

    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamError, match="responded with 429"):
            await get_with_retry(
                client,
                "https://example.test/wx",
                {},
                service="Open-Meteo",
                attempts=2,
                backoff_s=0,
            )
