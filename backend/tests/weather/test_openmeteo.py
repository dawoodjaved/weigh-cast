import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import numpy as np
import pytest
import respx
from django.core.cache import cache

from weighcast.http import UpstreamError
from weighcast.weather.openmeteo import (
    FRESH_TTL_S,
    GEOCODE_URL,
    NOMINATIM_REVERSE_URL,
    HourlySeries,
    OpenMeteoClient,
    grid_key,
    sample_weather,
)
from tests.fakes import open_meteo_responder

URL = "https://api.open-meteo.test/v1/forecast"
START = datetime(2026, 11, 3, 10, 20, tzinfo=UTC)
END = datetime(2026, 11, 5, 10, 20, tzinfo=UTC)


@pytest.fixture
async def client() -> AsyncIterator[OpenMeteoClient]:
    async with httpx.AsyncClient() as http:
        yield OpenMeteoClient(http, URL)


def windy_by_latitude(lat: float, _: float) -> dict[str, float]:
    return {"wind": lat, "gust": lat + 5, "rain": 0.1, "showers": 0.05, "snow": 0.0}


def test_grid_key_rounds_to_tenths() -> None:
    assert grid_key(41.87814, -87.62979) == (41.9, -87.6)
    assert grid_key(41.84, -87.61) == grid_key(41.849, -87.649)


@respx.mock
async def test_requests_hourly_imperial_window(client: OpenMeteoClient) -> None:
    call = respx.get(URL).mock(side_effect=open_meteo_responder())

    await client.hourly([(41.88, -87.63)], START, END)

    params = call.calls.last.request.url.params
    assert params["hourly"] == "wind_speed_10m,wind_gusts_10m,rain,showers,snowfall"
    assert params["wind_speed_unit"] == "mph"
    assert params["precipitation_unit"] == "inch"
    assert params["timeformat"] == "unixtime"
    assert params["start_hour"] == "2026-11-03T10:00"
    # Caller end (+1h) is past the default +48h floor, so the longer window wins.
    assert params["end_hour"] == "2026-11-05T11:00"
    assert "apikey" not in params


@respx.mock
async def test_hourly_uses_canonical_48h_window_for_short_trips(
    client: OpenMeteoClient,
) -> None:
    call = respx.get(URL).mock(side_effect=open_meteo_responder())
    short_end = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)

    await client.hourly([(41.88, -87.63)], START, short_end)

    params = call.calls.last.request.url.params
    assert params["start_hour"] == "2026-11-03T10:00"
    assert params["end_hour"] == "2026-11-05T10:00"


@respx.mock
async def test_attaches_api_key_when_configured() -> None:
    call = respx.get(URL).mock(side_effect=open_meteo_responder())

    async with httpx.AsyncClient() as http:
        client = OpenMeteoClient(http, URL, api_key="secret-key")
        await client.hourly([(41.88, -87.63)], START, END)

    assert call.calls.last.request.url.params["apikey"] == "secret-key"


@respx.mock
async def test_deduplicates_and_chunks_locations(client: OpenMeteoClient) -> None:
    call = respx.get(URL).mock(side_effect=open_meteo_responder(windy_by_latitude))
    keys = [(30 + i / 100, -90.0) for i in range(120)]

    series = await client.hourly(keys + keys[:30], START, END)

    assert call.call_count == 3
    sizes = sorted(len(c.request.url.params["latitude"].split(",")) for c in call.calls)
    assert sizes == [40, 40, 40]
    assert len(series) == 120
    assert series[(30.5, -90.0)].wind_mph[0] == pytest.approx(30.5)


@respx.mock
async def test_combines_rain_and_showers(client: OpenMeteoClient) -> None:
    respx.get(URL).mock(side_effect=open_meteo_responder(windy_by_latitude))

    series = await client.hourly([(40.0, -90.0)], START, END)

    assert series[(40.0, -90.0)].rain_in[0] == pytest.approx(0.15)


@respx.mock
async def test_serves_repeat_requests_from_cache(client: OpenMeteoClient) -> None:
    call = respx.get(URL).mock(side_effect=open_meteo_responder())

    await client.hourly([(40.0, -90.0), (41.0, -90.0)], START, END)
    await client.hourly([(40.0, -90.0), (42.0, -90.0)], START, END)

    assert call.call_count == 2
    assert call.calls.last.request.url.params["latitude"] == "42.0"


@respx.mock
async def test_serves_stale_redis_row_when_open_meteo_429s(
    client: OpenMeteoClient,
) -> None:
    key = (40.0, -90.0)
    respx.get(URL).mock(side_effect=open_meteo_responder())
    await client.hourly([key], START, END)
    await _age_entries([key], START)

    respx.get(URL).respond(429, json={"reason": "Rate limited"})

    series = await client.hourly([key], START, END)

    assert series[key].wind_mph.size > 0
    await _drain_background()


@respx.mock
async def test_falls_back_to_nws_on_open_meteo_429(client: OpenMeteoClient) -> None:
    key = (41.9, -87.6)
    respx.get(URL).respond(429, json={"reason": "Rate limited"})
    respx.get("https://api.weather.gov/points/41.9000,-87.6000").respond(
        json={
            "properties": {
                "forecastHourly": "https://api.weather.gov/gridpoints/LOT/9,9/forecast/hourly"
            }
        }
    )
    start_ts = int(START.replace(minute=0, second=0, microsecond=0).timestamp())
    periods = [
        {
            "startTime": datetime.fromtimestamp(start_ts + hour * 3600, UTC).strftime(
                "%Y-%m-%dT%H:%M:%S+00:00"
            ),
            "windSpeed": "18 mph",
            "windGust": "25 mph",
            "shortForecast": "Windy",
            "probabilityOfPrecipitation": {"value": 5},
        }
        for hour in range(50)
    ]
    respx.get("https://api.weather.gov/gridpoints/LOT/9,9/forecast/hourly").respond(
        json={"properties": {"periods": periods}}
    )

    series = await client.hourly([key], START, END)

    assert series[key].wind_mph[0] == pytest.approx(18.0)
    assert await cache.aget("om:circuit:429") == "1"


@respx.mock
async def test_stale_hit_returns_before_network_refresh(
    client: OpenMeteoClient,
) -> None:
    key = (41.0, -90.0)
    call = respx.get(URL).mock(side_effect=open_meteo_responder())
    await client.hourly([key], START, END)
    await _age_entries([key], START)
    before = call.call_count

    series = await client.hourly([key], START, END)
    assert series[key].wind_mph.size > 0
    # Redis-first: stale rows are returned without waiting on Open-Meteo.
    assert call.call_count == before
    await _drain_background()
    assert call.call_count > before


async def _age_entries(keys: list[tuple[float, float]], start: datetime) -> None:
    from weighcast.weather import openmeteo as om

    start_hour = om._floor_hour(start)
    cache_keys = [om._cache_key(key, start_hour) for key in keys]
    entries = await cache.aget_many(cache_keys)
    assert entries, f"expected cached weather for {cache_keys}"
    for ck, entry in entries.items():
        aged = {**entry, "t": time.time() - FRESH_TTL_S - 10}
        await cache.aset(ck, aged, 6 * 60 * 60)


async def _drain_background() -> None:
    pending = [task for task in asyncio.all_tasks() if task.get_name() == "openmeteo-refresh"]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@respx.mock
async def test_treats_missing_values_as_zero(client: OpenMeteoClient) -> None:
    respx.get(URL).respond(
        json={
            "hourly": {
                "time": [0, 3600],
                "wind_speed_10m": [10, None],
                "wind_gusts_10m": [None, None],
                "rain": [None, 0.2],
                "showers": [None, None],
                "snowfall": [0, 0],
            }
        }
    )

    series = (await client.hourly([(40.0, -90.0)], START, END))[(40.0, -90.0)]

    np.testing.assert_allclose(series.wind_mph, [10, 0])
    np.testing.assert_allclose(series.gust_mph, [0, 0])
    np.testing.assert_allclose(series.rain_in, [0, 0.2])


@respx.mock
async def test_surfaces_api_errors(client: OpenMeteoClient) -> None:
    respx.get(URL).respond(400, json={"error": True, "reason": "Latitude must be in range"})

    with pytest.raises(UpstreamError, match="Latitude must be in range"):
        await client.hourly([(40.0, -90.0)], START, END)


@respx.mock
async def test_geocode_parses_named_places(client: OpenMeteoClient) -> None:
    call = respx.get(GEOCODE_URL).respond(
        json={
            "results": [
                {
                    "name": "Chicago",
                    "latitude": 41.85,
                    "longitude": -87.65,
                    "admin1": "Illinois",
                    "country": "United States",
                    "country_code": "US",
                },
                {
                    "name": "Chicago",
                    "latitude": 41.73,
                    "longitude": -87.7,
                    "country": "Poland",
                    "country_code": "PL",
                },
                {"name": "Chico", "latitude": 39.73, "longitude": -121.84, "admin1": "California"},
            ]
        }
    )

    places = await client.geocode("  chicago ")

    assert places == [
        ("Chicago, Illinois, United States", 41.85, -87.65),
        ("Chico, California", 39.73, -121.84),
    ]
    params = call.calls.last.request.url.params
    assert params["name"] == "chicago"
    assert params["count"] == "20"
    assert "countryCode" not in params


@respx.mock
async def test_geocode_empty_when_nothing_matches(client: OpenMeteoClient) -> None:
    respx.get(GEOCODE_URL).respond(json={})

    assert await client.geocode("zzzzz") == []


@respx.mock
async def test_geocode_uses_cache(client: OpenMeteoClient) -> None:
    call = respx.get(GEOCODE_URL).respond(
        json={"results": [{"name": "Dallas", "latitude": 32.78, "longitude": -96.8}]}
    )

    first = await client.geocode("Dallas")
    second = await client.geocode("dallas")

    assert first == second == [("Dallas", 32.78, -96.8)]
    assert call.call_count == 1


@respx.mock
async def test_reverse_parses_named_place(client: OpenMeteoClient) -> None:
    call = respx.get(NOMINATIM_REVERSE_URL).respond(
        json={
            "name": "Chicago",
            "address": {
                "city": "Chicago",
                "state": "Illinois",
                "country": "United States",
                "country_code": "us",
            },
        }
    )

    name = await client.reverse(41.8781, -87.6298)

    assert name == "Chicago, Illinois, United States"
    params = call.calls.last.request.url.params
    assert params["lat"] == "41.87810"
    assert params["lon"] == "-87.62980"


@respx.mock
async def test_reverse_uses_cache(client: OpenMeteoClient) -> None:
    call = respx.get(NOMINATIM_REVERSE_URL).respond(
        json={"name": "Dallas", "address": {"city": "Dallas", "country_code": "us"}}
    )

    first = await client.reverse(32.7767, -96.7970)
    second = await client.reverse(32.7767, -96.7970)

    assert first == second == "Dallas"
    assert call.call_count == 1


@respx.mock
async def test_reverse_empty_when_lookup_fails(client: OpenMeteoClient) -> None:
    respx.get(NOMINATIM_REVERSE_URL).respond(500)

    assert await client.reverse(41.8781, -87.6298) == ""


@respx.mock
async def test_rejects_mismatched_location_count(client: OpenMeteoClient) -> None:
    respx.get(URL).respond(json=[{"hourly": {"time": [0]}}])

    with pytest.raises(UpstreamError, match="wrong number"):
        await client.hourly([(40.0, -90.0), (41.0, -90.0)], START, END)


def series(values: list[float]) -> HourlySeries:
    arr = np.asarray(values, dtype=np.float64)
    return HourlySeries(
        time=np.array([0.0, 3600.0, 7200.0]),
        wind_mph=arr,
        gust_mph=arr * 2,
        rain_in=arr / 100,
        snow_in=arr / 10,
    )


def test_interpolates_between_hours() -> None:
    weather = series([10, 30, 50]).at(np.array([1800.0, 5400.0, 9000.0]))

    np.testing.assert_allclose(weather.wind_mph, [20, 40, 50])
    np.testing.assert_allclose(weather.gust_mph, [40, 80, 100])


def test_sample_weather_uses_each_points_cell_and_time() -> None:
    by_key = {(40.0, -90.0): series([10, 20, 30]), (41.0, -90.0): series([50, 60, 70])}

    weather = sample_weather(
        by_key,
        lat=np.array([40.001, 41.0, 40.0]),
        lon=np.array([-90.0, -90.002, -90.0]),
        timestamps=np.array([0.0, 3600.0, 7200.0]),
    )

    np.testing.assert_allclose(weather.wind_mph, [10, 60, 30])
