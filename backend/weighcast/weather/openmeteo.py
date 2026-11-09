import asyncio
import hashlib
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import numpy as np
from django.core.cache import cache

from weighcast.core.geometry import FloatArray
from weighcast.core.risk import Weather
from weighcast.http import UpstreamError, get_with_retry
from weighcast.weather import nws

logger = logging.getLogger(__name__)

HOURLY_VARIABLES = ("wind_speed_10m", "wind_gusts_10m", "rain", "showers", "snowfall")
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
CHUNK_SIZE = 40
# One in-flight Open-Meteo call — Render’s shared egress IP trips free-tier 429s easily.
MAX_CONCURRENT_REQUESTS = 1
# Always pull leave-later horizon so plan + heatmap share one cache entry per cell.
FORECAST_HOURS = 48
# Redis-first TTLs: serve fresh for 45m, keep rows 6h for stale hits / 429 fallback.
FRESH_TTL_S = 45 * 60
CACHE_TTL_S = 6 * 60 * 60
GEOCODE_CACHE_TTL_S = 24 * 60 * 60
GEOCODE_FETCH = 20
GEOCODE_COUNTRIES = frozenset({"US", "CA", "MX"})
# ~11 km cells → far fewer unique Open-Meteo/NWS lookups on a corridor.
GRID_DECIMALS = 1
CIRCUIT_KEY = "om:circuit:429"
CIRCUIT_TTL_S = 10 * 60
GridKey = tuple[float, float]
WeatherRow = dict[str, list[float]]
CacheEntry = dict[str, Any]


@dataclass(frozen=True, slots=True, eq=False)
class HourlySeries:
    time: FloatArray
    wind_mph: FloatArray
    gust_mph: FloatArray
    rain_in: FloatArray
    snow_in: FloatArray

    def at(self, timestamps: FloatArray) -> Weather:
        return Weather(
            wind_mph=np.interp(timestamps, self.time, self.wind_mph),
            gust_mph=np.interp(timestamps, self.time, self.gust_mph),
            rain_in=np.interp(timestamps, self.time, self.rain_in),
            snow_in=np.interp(timestamps, self.time, self.snow_in),
        )


def grid_key(lat: float, lon: float) -> GridKey:
    return (round(float(lat), GRID_DECIMALS), round(float(lon), GRID_DECIMALS))


def sample_weather(
    series: dict[GridKey, HourlySeries],
    lat: FloatArray,
    lon: FloatArray,
    timestamps: FloatArray,
) -> Weather:
    groups: dict[GridKey, list[int]] = {}
    for i, (la, lo) in enumerate(zip(lat, lon, strict=True)):
        groups.setdefault(grid_key(la, lo), []).append(i)

    out = Weather(*(np.zeros(timestamps.shape) for _ in range(4)))
    for key, indices in groups.items():
        sampled = series[key].at(timestamps[indices])
        out.wind_mph[indices] = sampled.wind_mph
        out.gust_mph[indices] = sampled.gust_mph
        out.rain_in[indices] = sampled.rain_in
        out.snow_in[indices] = sampled.snow_in
    return out


class OpenMeteoClient:
    def __init__(
        self, http: httpx.AsyncClient, base_url: str, api_key: str | None = None
    ) -> None:
        self._http = http
        self._base_url = base_url
        self._api_key = (api_key or "").strip() or None
        self._limit = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def hourly(
        self, keys: Iterable[GridKey], start: datetime, end: datetime
    ) -> dict[GridKey, HourlySeries]:
        start_hour = _floor_hour(start)
        end_hour = start_hour + timedelta(hours=FORECAST_HOURS)
        if _floor_hour(end) + timedelta(hours=1) > end_hour:
            end_hour = _floor_hour(end) + timedelta(hours=1)
        unique = list(dict.fromkeys(keys))
        cache_keys = {key: _cache_key(key, start_hour) for key in unique}

        cached = await cache.aget_many(cache_keys.values())
        result: dict[GridKey, HourlySeries] = {}
        stale: dict[GridKey, WeatherRow] = {}
        for key, ck in cache_keys.items():
            entry = cached.get(ck)
            row = _row_from_entry(entry)
            if row is None:
                continue
            if _entry_is_fresh(entry):
                result[key] = _to_series(row)
            else:
                stale[key] = row

        missing = [key for key in unique if key not in result]
        if not missing:
            return result

        # Redis-first: if every miss still has stale data, return now and refresh off-request.
        if all(key in stale for key in missing):
            for key in missing:
                result[key] = _to_series(stale[key])
            asyncio.create_task(
                self._refresh_in_background(missing, start_hour, end_hour, cache_keys),
                name="openmeteo-refresh",
            )
            return result

        circuit_open = bool(await cache.aget(CIRCUIT_KEY))
        if circuit_open:
            logger.warning("Open-Meteo circuit open; using NWS/stale for %s cells", len(missing))
            await self._recover_without_open_meteo(
                missing, start_hour, cache_keys, result, stale
            )
            return result

        try:
            await self._fill_missing(missing, start_hour, end_hour, cache_keys, result)
        except UpstreamError as exc:
            if "429" not in str(exc):
                recovered = _apply_stale(missing, result, stale)
                if any(key not in result for key in missing):
                    raise
                logger.warning(
                    "Open-Meteo unavailable; served stale Redis weather for %s cells",
                    recovered,
                )
                return result
            await cache.aset(CIRCUIT_KEY, "1", CIRCUIT_TTL_S)
            await self._recover_without_open_meteo(
                missing, start_hour, cache_keys, result, stale
            )
        return result

    async def geocode(self, query: str, limit: int = 5) -> list[tuple[str, float, float]]:
        params = {
            "name": query.strip(),
            "count": GEOCODE_FETCH,
            "language": "en",
            "format": "json",
        }
        digest = hashlib.sha256(f"{query.strip().lower()}:{limit}".encode()).hexdigest()
        cache_key = f"om:geocode:{digest}"
        cached: list[tuple[str, float, float]] | None = await cache.aget(cache_key)
        if cached is not None:
            return cached

        response = await get_with_retry(
            self._http, GEOCODE_URL, self._with_key(params), service="Open-Meteo"
        )
        body = response.json()
        if response.status_code >= 400:
            reason = body.get("reason") if isinstance(body, dict) else None
            raise UpstreamError("Open-Meteo", reason or f"responded with {response.status_code}")

        rows = body.get("results") if isinstance(body, dict) else None
        places: list[tuple[str, float, float]] = []
        for row in rows or []:
            if "latitude" not in row or "longitude" not in row:
                continue
            code = row.get("country_code")
            if isinstance(code, str) and code not in GEOCODE_COUNTRIES:
                continue
            places.append(_geocode_place(row))
            if len(places) == limit:
                break
        await cache.aset(cache_key, places, GEOCODE_CACHE_TTL_S)
        return places

    async def reverse(self, lat: float, lon: float) -> str:
        cache_key = f"om:reverse:{lat:.4f}:{lon:.4f}"
        cached = await cache.aget(cache_key)
        if isinstance(cached, str):
            return cached

        params = {
            "lat": f"{lat:.5f}",
            "lon": f"{lon:.5f}",
            "format": "json",
            "addressdetails": "1",
            "zoom": "10",
        }
        try:
            response = await get_with_retry(
                self._http, NOMINATIM_REVERSE_URL, params, service="Nominatim"
            )
        except UpstreamError:
            return ""
        body = response.json()
        if response.status_code >= 400 or not isinstance(body, dict):
            return ""

        name = _nominatim_place(body)
        if name:
            await cache.aset(cache_key, name, GEOCODE_CACHE_TTL_S)
        return name

    def _with_key(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            return params
        return {**params, "apikey": self._api_key}

    async def _refresh_in_background(
        self,
        keys: Sequence[GridKey],
        start_hour: datetime,
        end_hour: datetime,
        cache_keys: dict[GridKey, str],
    ) -> None:
        try:
            if await cache.aget(CIRCUIT_KEY):
                sink: dict[GridKey, HourlySeries] = {}
                await self._fill_from_nws(list(keys), start_hour, cache_keys, sink)
                return
            sink = {}
            await self._fill_missing(list(keys), start_hour, end_hour, cache_keys, sink)
        except Exception:
            logger.warning("Background weather refresh failed", exc_info=True)

    async def _recover_without_open_meteo(
        self,
        missing: list[GridKey],
        start_hour: datetime,
        cache_keys: dict[GridKey, str],
        result: dict[GridKey, HourlySeries],
        stale: dict[GridKey, WeatherRow],
    ) -> None:
        recovered = _apply_stale(missing, result, stale)
        still = [key for key in missing if key not in result]
        if still:
            await self._fill_from_nws(still, start_hour, cache_keys, result)
        leftover = [key for key in missing if key not in result]
        if leftover:
            raise UpstreamError(
                "Open-Meteo",
                "responded with 429 (NWS fallback also unavailable for some cells)",
            )
        if recovered:
            logger.warning(
                "Open-Meteo rate-limited; served stale Redis weather for %s cells", recovered
            )

    async def _fill_from_nws(
        self,
        keys: list[GridKey],
        start_hour: datetime,
        cache_keys: dict[GridKey, str],
        result: dict[GridKey, HourlySeries],
    ) -> None:
        to_cache: dict[str, CacheEntry] = {}
        now = time.time()
        for key in keys:
            if key in result:
                continue
            lat, lon = key
            if not nws.in_conus(lat, lon):
                continue
            try:
                row = await nws.hourly_row(
                    self._http, lat, lon, start_hour, FORECAST_HOURS
                )
            except UpstreamError:
                logger.warning("NWS fallback failed for %s,%s", lat, lon, exc_info=True)
                continue
            result[key] = _to_series(row)
            to_cache[cache_keys[key]] = {"t": now, "row": row, "src": "nws"}
        if to_cache:
            await cache.aset_many(to_cache, CACHE_TTL_S)
            logger.info("NWS fallback filled %s weather cells", len(to_cache))

    async def _fill_missing(
        self,
        missing: list[GridKey],
        start_hour: datetime,
        end_hour: datetime,
        cache_keys: dict[GridKey, str],
        result: dict[GridKey, HourlySeries],
    ) -> None:
        # Re-check Redis in case a sibling worker just wrote the cells.
        cached = await cache.aget_many([cache_keys[key] for key in missing])
        pending: list[GridKey] = []
        for key in missing:
            row = _row_from_entry(cached.get(cache_keys[key]))
            if row is not None and _entry_is_fresh(cached.get(cache_keys[key])):
                result[key] = _to_series(row)
            else:
                pending.append(key)
        if not pending:
            return

        chunks = [pending[i : i + CHUNK_SIZE] for i in range(0, len(pending), CHUNK_SIZE)]
        # Sequential chunks (semaphore=1) to avoid bursting the free Open-Meteo tier.
        to_cache: dict[str, CacheEntry] = {}
        now = time.time()
        for chunk in chunks:
            rows = await self._fetch(chunk, start_hour, end_hour)
            for key, row in zip(chunk, rows, strict=True):
                result[key] = _to_series(row)
                to_cache[cache_keys[key]] = {"t": now, "row": row}
        if to_cache:
            await cache.aset_many(to_cache, CACHE_TTL_S)

    async def _fetch(
        self, keys: Sequence[GridKey], start_hour: datetime, end_hour: datetime
    ) -> list[WeatherRow]:
        fmt = f"{{:.{GRID_DECIMALS}f}}"
        params = self._with_key(
            {
                "latitude": ",".join(fmt.format(lat) for lat, _ in keys),
                "longitude": ",".join(fmt.format(lon) for _, lon in keys),
                "hourly": ",".join(HOURLY_VARIABLES),
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
                "timeformat": "unixtime",
                "timezone": "GMT",
                "start_hour": start_hour.strftime("%Y-%m-%dT%H:%M"),
                "end_hour": end_hour.strftime("%Y-%m-%dT%H:%M"),
            }
        )
        async with self._limit:
            response = await get_with_retry(
                self._http, self._base_url, params, service="Open-Meteo"
            )
        body = response.json()
        if response.status_code >= 400:
            reason = body.get("reason") if isinstance(body, dict) else None
            raise UpstreamError("Open-Meteo", reason or f"responded with {response.status_code}")

        locations = body if isinstance(body, list) else [body]
        if len(locations) != len(keys):
            raise UpstreamError("Open-Meteo", "returned the wrong number of locations")
        return [_compact(location["hourly"]) for location in locations]


def _compact(hourly: dict[str, list[float | None]]) -> WeatherRow:
    def column(name: str) -> FloatArray:
        values = np.array(hourly.get(name) or [], dtype=np.float64)
        return np.nan_to_num(values, nan=0.0)

    if not hourly.get("time"):
        raise UpstreamError("Open-Meteo", "returned no hourly data")
    rain = column("rain")
    showers = column("showers")
    if showers.size == rain.size:
        rain = rain + showers
    return {
        "time": column("time").tolist(),
        "wind_mph": column("wind_speed_10m").tolist(),
        "gust_mph": column("wind_gusts_10m").tolist(),
        "rain_in": rain.tolist(),
        "snow_in": column("snowfall").tolist(),
    }


def _to_series(row: WeatherRow) -> HourlySeries:
    return HourlySeries(
        time=np.asarray(row["time"], dtype=np.float64),
        wind_mph=np.asarray(row["wind_mph"], dtype=np.float64),
        gust_mph=np.asarray(row["gust_mph"], dtype=np.float64),
        rain_in=np.asarray(row["rain_in"], dtype=np.float64),
        snow_in=np.asarray(row["snow_in"], dtype=np.float64),
    )


def _cache_key(key: GridKey, start_hour: datetime) -> str:
    fmt = f"{{:.{GRID_DECIMALS}f}}"
    return f"om:{fmt.format(key[0])},{fmt.format(key[1])}:{start_hour:%Y%m%d%H}:h{FORECAST_HOURS}"


def _apply_stale(
    missing: list[GridKey],
    result: dict[GridKey, HourlySeries],
    stale: dict[GridKey, WeatherRow],
) -> int:
    recovered = 0
    for key in missing:
        if key in result:
            continue
        row = stale.get(key)
        if row is None:
            continue
        result[key] = _to_series(row)
        recovered += 1
    return recovered


def _row_from_entry(entry: Any) -> WeatherRow | None:
    if isinstance(entry, dict) and isinstance(entry.get("row"), dict):
        row = entry["row"]
        if all(k in row for k in ("time", "wind_mph", "gust_mph", "rain_in", "snow_in")):
            return row  # type: ignore[return-value]
    # Legacy flat rows from older deploys.
    if isinstance(entry, dict) and "time" in entry and "wind_mph" in entry:
        return entry  # type: ignore[return-value]
    return None


def _entry_is_fresh(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    # Legacy flat rows: treat as fresh so one deploy does not stampede Open-Meteo.
    if "row" not in entry and "time" in entry:
        return True
    stamped = entry.get("t")
    if not isinstance(stamped, (int, float)):
        return False
    return (time.time() - float(stamped)) < FRESH_TTL_S


def _floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _geocode_place(row: dict[str, Any]) -> tuple[str, float, float]:
    parts = [row.get("name"), row.get("admin1"), row.get("country")]
    name = ", ".join(part for part in parts if isinstance(part, str) and part)
    return (name, float(row["latitude"]), float(row["longitude"]))


def _nominatim_place(body: dict[str, Any]) -> str:
    address = body.get("address")
    if not isinstance(address, dict):
        return ""
    code = address.get("country_code")
    if isinstance(code, str) and code.upper() not in GEOCODE_COUNTRIES:
        return ""
    city = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
        or address.get("county")
        or body.get("name")
    )
    parts = [city, address.get("state"), address.get("country")]
    return ", ".join(part for part in parts if isinstance(part, str) and part)
