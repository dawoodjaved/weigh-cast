import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from string import ascii_uppercase
from typing import Any
from uuid import uuid4

import numpy as np
from django.core.cache import cache

from weighcast.core.geometry import (
    METERS_PER_MILE,
    Checkpoints,
    FloatArray,
    build_profile,
    sample_checkpoints,
)
from weighcast.core.heatmap import corridor_grid
from weighcast.core.polyline import encode_arrays
from weighcast.core.risk import (
    Classification,
    RiskLevel,
    RouteRisk,
    Weather,
    classify,
    factor_levels,
    rank,
    summarize,
)
from weighcast.routing.types import LatLon, TruckRoute
from weighcast.routing.valhalla import ValhallaClient
from weighcast.weather.openmeteo import OpenMeteoClient, grid_key, sample_weather

HORIZON_HOURS = 48
PLAN_TTL_S = 30 * 60
# Coarser corridor samples → fewer heatmap cells / faster follow-up fetches.
CORRIDOR_SAMPLE_M = 5_000.0
_COORD_NAME = re.compile(r"^-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?$")


class PlanNotFoundError(Exception):
    pass


WEATHER_MODES = frozenset({"live", "storm", "calm"})


@dataclass(frozen=True, slots=True)
class TripRequest:
    origin: LatLon
    destination: LatLon
    departure: datetime
    load_lb: float
    interval_miles: int
    gust_aware: bool
    origin_name: str = ""
    destination_name: str = ""
    weather_mode: str = "live"


async def plan_trip(
    trip: TripRequest, router: ValhallaClient, weather: OpenMeteoClient
) -> dict[str, Any]:
    routes = await router.truck_routes(trip.origin, trip.destination, trip.departure, trip.load_lb)
    profiles = [build_profile(r.lat, r.lon, r.spans) for r in routes]
    checkpoints = [sample_checkpoints(p, trip.interval_miles * METERS_PER_MILE) for p in profiles]
    max_duration_s = max(p.total_duration_s for p in profiles)
    demo_weather = trip.weather_mode in {"storm", "calm"}

    # Places with real names skip Nominatim; resolve in parallel with weather.
    places_task = asyncio.gather(
        _resolved_place(weather, trip.origin, trip.origin_name),
        _resolved_place(weather, trip.destination, trip.destination_name),
    )

    series: dict = {}
    if not demo_weather:
        # Same 0–48h window as the heatmap so checkpoint cells are warm for /api/heatmap.
        start, end = _heatmap_weather_window(trip.departure, max_duration_s)
        keys = list(
            dict.fromkeys(
                grid_key(lat, lon)
                for cp in checkpoints
                for lat, lon in zip(cp.lat, cp.lon, strict=True)
            )
        )
        series = await weather.hourly(keys, start, end)

    departure_ts = trip.departure.timestamp()
    ids = list(ascii_uppercase[: len(routes)])
    risks: dict[str, RouteRisk] = {}
    payloads = []
    for route_id, route, profile, cp in zip(ids, routes, profiles, checkpoints, strict=True):
        eta = departure_ts + cp.eta_offset_s
        if demo_weather:
            blank = Weather(
                wind_mph=np.zeros(eta.shape),
                gust_mph=np.zeros(eta.shape),
                rain_in=np.zeros(eta.shape),
                snow_in=np.zeros(eta.shape),
            )
            sampled = _apply_weather_mode(blank, trip.weather_mode, cp.lon)
        else:
            sampled = _apply_weather_mode(
                sample_weather(series, cp.lat, cp.lon, eta), trip.weather_mode, cp.lon
            )
        result = classify(sampled, trip.load_lb, gust_aware=trip.gust_aware)
        risk = summarize(cp.distance_m, result.level, profile.total_duration_s)
        label = _route_label(route_id, route.via)
        risks[label] = risk
        payloads.append(_route_payload(route_id, route, cp, eta, sampled, result, risk, label))

    recommendation = rank(risks)
    plan_id = uuid4().hex
    origin, destination = await places_task
    body = {
        "plan_id": plan_id,
        "departure": trip.departure.isoformat(),
        "load_lb": trip.load_lb,
        "interval_miles": trip.interval_miles,
        "gust_aware": trip.gust_aware,
        "weather_mode": trip.weather_mode,
        "origin": origin,
        "destination": destination,
        "recommendation": {
            "route_id": _route_id(recommendation.recommended),
            "order": [_route_id(name) for name in recommendation.order],
            "no_safe_route": recommendation.no_safe_route,
            "reason": recommendation.reason,
        },
        "routes": payloads,
    }
    await cache.aset(
        _plan_key(plan_id),
        {
            "departure": trip.departure.isoformat(),
            "load_lb": trip.load_lb,
            "gust_aware": trip.gust_aware,
            "weather_mode": trip.weather_mode,
            "max_duration_s": max_duration_s,
            "corridors": [
                {"lat": c.lat.round(4).tolist(), "lon": c.lon.round(4).tolist()}
                for c in (sample_checkpoints(p, CORRIDOR_SAMPLE_M) for p in profiles)
            ],
            "payload": body,
        },
        PLAN_TTL_S,
    )
    return body


async def load_plan(plan_id: str, weather: OpenMeteoClient | None = None) -> dict[str, Any]:
    plan = await cache.aget(_plan_key(plan_id))
    if not plan or "payload" not in plan:
        raise PlanNotFoundError(plan_id)
    payload = _with_places(plan["payload"])
    if weather is None:
        return payload
    origin = payload.get("origin")
    destination = payload.get("destination")
    if not isinstance(origin, dict) or not isinstance(destination, dict):
        return payload
    resolved_origin = await _resolved_place(
        weather, (float(origin["lat"]), float(origin["lon"])), str(origin.get("name") or "")
    )
    resolved_destination = await _resolved_place(
        weather,
        (float(destination["lat"]), float(destination["lon"])),
        str(destination.get("name") or ""),
    )
    origin_name = resolved_origin["name"]
    destination_name = resolved_destination["name"]
    if origin_name == origin.get("name") and destination_name == destination.get("name"):
        return payload
    updated = {**payload, "origin": resolved_origin, "destination": resolved_destination}
    plan["payload"] = updated
    await cache.aset(_plan_key(plan_id), plan, PLAN_TTL_S)
    return updated


async def corridor_heatmap(
    plan_id: str, weather: OpenMeteoClient, max_cells: int
) -> dict[str, Any]:
    plan = await cache.aget(_plan_key(plan_id))
    if plan is None:
        raise PlanNotFoundError(plan_id)

    departure = datetime.fromisoformat(plan["departure"])
    grid = corridor_grid(
        [(np.asarray(c["lat"]), np.asarray(c["lon"])) for c in plan["corridors"]],
        max_cells=max_cells,
    )
    mode = str(plan.get("weather_mode") or "live")
    hours = HORIZON_HOURS + 1
    if mode in {"storm", "calm"}:
        shape = (len(grid.lat), hours)
        stacked = _apply_weather_mode(
            Weather(
                wind_mph=np.zeros(shape),
                gust_mph=np.zeros(shape),
                rain_in=np.zeros(shape),
                snow_in=np.zeros(shape),
            ),
            mode,
            grid.lon,
        )
    else:
        start, end = _heatmap_weather_window(departure, plan["max_duration_s"])
        keys = [grid_key(lat, lon) for lat, lon in zip(grid.lat, grid.lon, strict=True)]
        series = await weather.hourly(keys, start, end)
        stamps = departure.timestamp() + 3600.0 * np.arange(hours, dtype=np.float64)
        samples = [series[key].at(stamps) for key in keys]
        stacked = Weather(
            wind_mph=np.array([s.wind_mph for s in samples]),
            gust_mph=np.array([s.gust_mph for s in samples]),
            rain_in=np.array([s.rain_in for s in samples]),
            snow_in=np.array([s.snow_in for s in samples]),
        )
        stacked = _apply_weather_mode(stacked, mode, grid.lon)
    factors = factor_levels(stacked, plan["load_lb"], gust_aware=plan["gust_aware"])

    return {
        "plan_id": plan_id,
        "start": departure.isoformat(),
        "hours": HORIZON_HOURS + 1,
        "cell_size_m": round(grid.cell_m),
        "cells": [
            {
                "lat": round(float(lat), 4),
                "lon": round(float(lon), 4),
                "risk": factors["risk"][i].tolist(),
                "wind": factors["wind"][i].tolist(),
                "rain": factors["rain"][i].tolist(),
                "snow": factors["snow"][i].tolist(),
            }
            for i, (lat, lon) in enumerate(zip(grid.lat, grid.lon, strict=True))
        ],
    }


def _route_payload(
    route_id: str,
    route: TruckRoute,
    cp: Checkpoints,
    eta: FloatArray,
    sampled: Weather,
    result: Classification,
    risk: RouteRisk,
    label: str,
) -> dict[str, Any]:
    return {
        "id": route_id,
        "name": label,
        "via": route.via,
        "polyline": encode_arrays(route.lat, route.lon),
        "distance_m": round(route.length_m),
        "duration_s": round(route.duration_s),
        "departure": route.departure.isoformat(),
        "arrival": route.arrival.isoformat(),
        "notices": route.notices,
        "summary": {
            "miles_by_level": [round(m, 2) for m in risk.miles_by_level],
            "high_miles": round(risk.high_miles, 2),
            "severe_miles": round(risk.severe_miles, 2),
            "no_travel_miles": round(risk.no_travel_miles, 2),
            "avg_risk": round(risk.avg_risk, 3),
            "max_level": int(result.level.max()),
        },
        "checkpoints": [
            {
                "distance_m": round(float(cp.distance_m[i])),
                "lat": round(float(cp.lat[i]), 5),
                "lon": round(float(cp.lon[i]), 5),
                "eta": datetime.fromtimestamp(float(eta[i]), UTC).isoformat(timespec="seconds"),
                "vertex_index": int(cp.vertex_index[i]),
                "level": int(result.level[i]),
                "level_label": RiskLevel(int(result.level[i])).label,
                "factor": result.factor[i],
                "wind_mph": round(float(sampled.wind_mph[i]), 1),
                "gust_mph": round(float(sampled.gust_mph[i]), 1),
                "rain_in": round(float(sampled.rain_in[i]), 2),
                "snow_in": round(float(sampled.snow_in[i]), 2),
            }
            for i in range(len(cp))
        ],
    }


def _trip_weather_window(departure: datetime, duration_s: float) -> tuple[datetime, datetime]:
    """Hours needed for checkpoint ETAs only (plan scoring)."""
    pad = timedelta(hours=1)
    return departure, departure + timedelta(seconds=max(duration_s, 0.0)) + pad


def _heatmap_weather_window(departure: datetime, duration_s: float) -> tuple[datetime, datetime]:
    """Full leave-later horizon for the corridor heatmap."""
    span = max(timedelta(hours=HORIZON_HOURS), timedelta(seconds=duration_s))
    return departure, departure + span


def _forecast_window(departure: datetime, duration_s: float) -> tuple[datetime, datetime]:
    """Back-compat alias used by tests / callers expecting the heatmap window."""
    return _heatmap_weather_window(departure, duration_s)


def _route_label(route_id: str, via: str) -> str:
    base = f"Route {route_id}"
    return f"{base} via {via}" if via else base


def _route_id(name: str) -> str:
    parts = name.split()
    if len(parts) >= 2 and parts[0] == "Route":
        return parts[1]
    return name.removeprefix("Route ").split()[0]


def _apply_weather_mode(weather: Weather, mode: str, lon: FloatArray) -> Weather:
    """Overlay demo storm/calm patterns so calm days still demo risk."""
    if mode == "live":
        return weather
    shape = weather.wind_mph.shape
    lon = np.asarray(lon, dtype=np.float64)
    if lon.ndim == 1 and weather.wind_mph.ndim == 2:
        west = (lon < lon.mean()).astype(np.float64)[:, None]
    else:
        west = (lon < float(np.mean(lon))).astype(np.float64)
    if mode == "calm":
        return Weather(
            wind_mph=np.full(shape, 8.0),
            gust_mph=np.full(shape, 12.0),
            rain_in=np.zeros(shape),
            snow_in=np.zeros(shape),
        )
    # storm: western band gets severe wind + rain; eastern stays milder
    wind = 18.0 + west * 40.0
    gust = wind + 8.0
    rain = west * 0.7
    return Weather(
        wind_mph=np.broadcast_to(wind, shape).astype(np.float64).copy(),
        gust_mph=np.broadcast_to(gust, shape).astype(np.float64).copy(),
        rain_in=np.broadcast_to(rain, shape).astype(np.float64).copy(),
        snow_in=np.zeros(shape),
    )


def _plan_key(plan_id: str) -> str:
    return f"plan:{plan_id}"


def _named_place(point: LatLon, name: str) -> dict[str, Any]:
    lat, lon = point
    label = name.strip() or f"{lat:.4f}, {lon:.4f}"
    return {"name": label, "lat": lat, "lon": lon}


def _needs_name(name: str) -> bool:
    label = name.strip()
    return not label or bool(_COORD_NAME.match(label))


async def _resolved_place(weather: OpenMeteoClient, point: LatLon, name: str) -> dict[str, Any]:
    label = name.strip()
    if _needs_name(label):
        looked_up = await weather.reverse(point[0], point[1])
        if looked_up:
            label = looked_up
    return _named_place(point, label)


def _with_places(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("origin") and payload.get("destination"):
        return payload
    extras = _places_from_routes(payload)
    return {**payload, **extras} if extras else payload


def _places_from_routes(payload: dict[str, Any]) -> dict[str, Any]:
    routes = payload.get("routes") or []
    if not routes:
        return {}
    checkpoints = routes[0].get("checkpoints") or []
    if not checkpoints:
        return {}
    start = checkpoints[0]
    end = checkpoints[-1]
    return {
        "origin": _named_place((start["lat"], start["lon"]), ""),
        "destination": _named_place((end["lat"], end["lon"]), ""),
    }
