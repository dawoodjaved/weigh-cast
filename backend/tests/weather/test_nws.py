from datetime import UTC, datetime

import httpx
import pytest
import respx

from weighcast.weather import nws


def test_parse_mph_ranges() -> None:
    assert nws.parse_mph("10 mph") == 10.0
    assert nws.parse_mph("5 to 15 mph") == 15.0
    assert nws.parse_mph(None) == 0.0


def test_precip_from_forecast_text() -> None:
    rain, snow = nws.precip_inches("Heavy Rain", 80)
    assert rain > 0
    assert snow == 0
    rain, snow = nws.precip_inches("Snow Showers", 40)
    assert snow > 0


@respx.mock
async def test_hourly_row_builds_wind_series() -> None:
    start = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    respx.get("https://api.weather.gov/points/41.9000,-87.6000").respond(
        json={
            "properties": {
                "forecastHourly": "https://api.weather.gov/gridpoints/LOT/1,2/forecast/hourly"
            }
        }
    )
    periods = []
    for hour in range(50):
        ts = start.timestamp() + hour * 3600
        iso = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        periods.append(
            {
                "startTime": iso,
                "windSpeed": "12 mph",
                "windGust": "20 mph",
                "shortForecast": "Mostly Clear",
                "probabilityOfPrecipitation": {"value": 10},
            }
        )
    respx.get("https://api.weather.gov/gridpoints/LOT/1,2/forecast/hourly").respond(
        json={"properties": {"periods": periods}}
    )

    async with httpx.AsyncClient() as http:
        row = await nws.hourly_row(http, 41.9, -87.6, start, 48)

    assert len(row["time"]) == 49
    assert row["wind_mph"][0] == 12.0
    assert row["gust_mph"][0] == 20.0
