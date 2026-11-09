"""National Weather Service fallback when Open-Meteo is rate-limited (US only)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from django.core.cache import cache

from weighcast.http import UpstreamError, get_with_retry

logger = logging.getLogger(__name__)

POINTS_URL = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
POINTS_TTL_S = 24 * 60 * 60
FORECAST_TTL_S = 45 * 60
_MPH = re.compile(r"(\d+(?:\.\d+)?)")


def in_conus(lat: float, lon: float) -> bool:
    return 24.0 <= lat <= 50.0 and -125.0 <= lon <= -66.0


def parse_mph(text: str | None) -> float:
    if not text:
        return 0.0
    values = [float(match) for match in _MPH.findall(text)]
    return max(values) if values else 0.0


def precip_inches(short_forecast: str, pop: float | None) -> tuple[float, float]:
    """Rough rain/snow rates so risk scoring still has a precip signal."""
    text = short_forecast.lower()
    intensity = 0.0
    if pop is not None and pop >= 50:
        intensity = 0.15
    elif pop is not None and pop >= 30:
        intensity = 0.08
    elif "rain" in text or "shower" in text or "drizzle" in text:
        intensity = 0.05
    snow = 0.0
    rain = 0.0
    if "snow" in text or "blizzard" in text or "flurries" in text:
        snow = max(intensity, 0.3 if "heavy" in text else 0.1)
    elif intensity:
        rain = intensity * (2.0 if "heavy" in text else 1.0)
    return rain, snow


async def hourly_row(
    http: httpx.AsyncClient,
    lat: float,
    lon: float,
    start_hour: datetime,
    hours: int,
) -> dict[str, list[float]]:
    if not in_conus(lat, lon):
        raise UpstreamError("NWS", "location is outside CONUS coverage")

    forecast_url = await _forecast_hourly_url(http, lat, lon)
    periods = await _forecast_periods(http, forecast_url)
    return _periods_to_row(periods, start_hour, hours)


async def _forecast_hourly_url(http: httpx.AsyncClient, lat: float, lon: float) -> str:
    cache_key = f"nws:points:{lat:.2f},{lon:.2f}"
    cached = await cache.aget(cache_key)
    if isinstance(cached, str) and cached:
        return cached

    url = POINTS_URL.format(lat=lat, lon=lon)
    response = await get_with_retry(http, url, {}, service="NWS")
    body = response.json()
    if response.status_code >= 400 or not isinstance(body, dict):
        raise UpstreamError("NWS", f"points lookup failed ({response.status_code})")
    props = body.get("properties") or {}
    forecast_url = props.get("forecastHourly")
    if not isinstance(forecast_url, str) or not forecast_url:
        raise UpstreamError("NWS", "points response missing forecastHourly")
    await cache.aset(cache_key, forecast_url, POINTS_TTL_S)
    return forecast_url


async def _forecast_periods(http: httpx.AsyncClient, forecast_url: str) -> list[dict[str, Any]]:
    cache_key = f"nws:hourly:{forecast_url}"
    cached = await cache.aget(cache_key)
    if isinstance(cached, list) and cached:
        return cached

    response = await get_with_retry(http, forecast_url, {}, service="NWS")
    body = response.json()
    if response.status_code >= 400 or not isinstance(body, dict):
        raise UpstreamError("NWS", f"hourly forecast failed ({response.status_code})")
    periods = (body.get("properties") or {}).get("periods") or []
    if not isinstance(periods, list) or not periods:
        raise UpstreamError("NWS", "hourly forecast returned no periods")
    await cache.aset(cache_key, periods, FORECAST_TTL_S)
    return periods


def _periods_to_row(
    periods: list[dict[str, Any]], start_hour: datetime, hours: int
) -> dict[str, list[float]]:
    by_hour: dict[int, dict[str, Any]] = {}
    for period in periods:
        start = period.get("startTime")
        if not isinstance(start, str):
            continue
        try:
            moment = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            continue
        hour = moment.replace(minute=0, second=0, microsecond=0)
        by_hour[int(hour.timestamp())] = period

    times: list[float] = []
    wind: list[float] = []
    gust: list[float] = []
    rain: list[float] = []
    snow: list[float] = []
    cursor = start_hour.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    last: dict[str, Any] | None = None
    for _ in range(hours + 1):
        ts = int(cursor.timestamp())
        period = by_hour.get(ts) or last or next(iter(by_hour.values()), {})
        last = period or last
        pop = None
        pop_obj = period.get("probabilityOfPrecipitation") if isinstance(period, dict) else None
        if isinstance(pop_obj, dict) and isinstance(pop_obj.get("value"), (int, float)):
            pop = float(pop_obj["value"])
        short = str(period.get("shortForecast") or "") if isinstance(period, dict) else ""
        wind_mph = parse_mph(period.get("windSpeed") if isinstance(period, dict) else None)
        gust_mph = parse_mph(period.get("windGust") if isinstance(period, dict) else None)
        if gust_mph <= 0:
            gust_mph = wind_mph
        rain_in, snow_in = precip_inches(short, pop)
        times.append(float(ts))
        wind.append(wind_mph)
        gust.append(gust_mph)
        rain.append(rain_in)
        snow.append(snow_in)
        cursor += timedelta(hours=1)

    if not times:
        raise UpstreamError("NWS", "could not align hourly periods")
    return {
        "time": times,
        "wind_mph": wind,
        "gust_mph": gust,
        "rain_in": rain,
        "snow_in": snow,
    }
