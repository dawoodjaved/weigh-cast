import logging
from collections.abc import Awaitable, Callable
from typing import Any

from asgiref.sync import async_to_sync
from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from weighcast.api import serializers as s
from weighcast.api.planning import (
    PlanNotFoundError,
    TripRequest,
    corridor_heatmap,
    load_plan,
    plan_trip,
)
from weighcast.http import UpstreamError, shared_client
from weighcast.routing.types import NoRouteError
from weighcast.routing.valhalla import ValhallaClient
from weighcast.weather.openmeteo import OpenMeteoClient

logger = logging.getLogger(__name__)


async def _clients() -> tuple[ValhallaClient, OpenMeteoClient]:
    http = await shared_client()
    return ValhallaClient(http, settings.VALHALLA_URL), OpenMeteoClient(
        http, settings.OPEN_METEO_BASE_URL, settings.OPEN_METEO_API_KEY
    )


class UnprocessableTrip(APIException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = "No route could be planned for this trip."
    default_code = "no_route"


class BadGateway(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "An upstream service failed."
    default_code = "upstream_error"


@require_GET
async def health(request: HttpRequest) -> JsonResponse:
    from django.core.cache import cache

    cache_backend = "redis" if settings.REDIS_URL else "locmem"
    cache_ok = True
    try:
        await cache.aset("health:ping", "1", 10)
        cache_ok = (await cache.aget("health:ping")) == "1"
    except Exception:
        cache_ok = False
        logger.exception("Cache health check failed")

    status_name = "ok" if cache_ok else "degraded"
    code = status.HTTP_200_OK if cache_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return JsonResponse(
        {"status": status_name, "cache": cache_backend, "cache_ok": cache_ok},
        status=code,
    )


class PlanView(APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "plan"

    @extend_schema(
        parameters=[OpenApiParameter("plan_id", str, required=True)],
        responses=s.PlanResponseSerializer,
    )
    def get(self, request: Request) -> Response:
        query = s.HeatmapQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        plan_id = query.validated_data["plan_id"]

        async def run() -> dict[str, Any]:
            _, weather = await _clients()
            return await load_plan(plan_id, weather)

        return Response(_call(run))

    @extend_schema(request=s.PlanRequestSerializer, responses=s.PlanResponseSerializer)
    def post(self, request: Request) -> Response:
        serializer = s.PlanRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        trip = TripRequest(
            origin=(data["origin"]["lat"], data["origin"]["lon"]),
            destination=(data["destination"]["lat"], data["destination"]["lon"]),
            departure=data["departure"],
            load_lb=data["load_lb"],
            interval_miles=int(data["interval_miles"]),
            gust_aware=data["gust_aware"],
            origin_name=str(data["origin"].get("name") or ""),
            destination_name=str(data["destination"].get("name") or ""),
            weather_mode=str(data.get("weather_mode") or "live"),
        )

        async def run() -> dict[str, Any]:
            router, weather = await _clients()
            return await plan_trip(trip, router, weather)

        return Response(_call(run))


class HeatmapView(APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "heatmap"

    @extend_schema(
        parameters=[OpenApiParameter("plan_id", str, required=True)],
        responses=s.HeatmapResponseSerializer,
    )
    def get(self, request: Request) -> Response:
        query = s.HeatmapQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        plan_id = query.validated_data["plan_id"]

        async def run() -> dict[str, Any]:
            _, weather = await _clients()
            return await corridor_heatmap(plan_id, weather, settings.HEATMAP_MAX_CELLS)

        return Response(_call(run))


class GeocodeView(APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "geocode"

    @extend_schema(
        parameters=[OpenApiParameter("q", str, required=True)],
        responses=s.GeocodeResponseSerializer,
    )
    def get(self, request: Request) -> Response:
        query = s.GeocodeQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        text = query.validated_data["q"]

        async def run() -> dict[str, Any]:
            _, weather = await _clients()
            return _place_results(await weather.geocode(text))

        return Response(_call(run))


def _place_results(places: list[tuple[str, float, float]]) -> dict[str, Any]:
    return {"results": [{"name": name, "lat": lat, "lon": lon} for name, lat, lon in places]}


def _call[T](run: Callable[[], Awaitable[T]]) -> T:
    try:
        return async_to_sync(run)()
    except NoRouteError as exc:
        raise UnprocessableTrip(str(exc)) from exc
    except PlanNotFoundError as exc:
        raise NotFound("This plan has expired. Plan the trip again.") from exc
    except UpstreamError as exc:
        logger.warning("Upstream failure: %s", exc)
        raise BadGateway(str(exc)) from exc
