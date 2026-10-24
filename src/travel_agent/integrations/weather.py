from __future__ import annotations

import threading
import time
import re
from datetime import date, timedelta
from typing import Any

import requests

from src.travel_agent.integrations.liteapi_client import LiteAPIClient


class WeatherLocationError(ValueError):
    pass


class WeatherProviderError(ValueError):
    pass


_GEOCODE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_GEOCODE_LOCK = threading.Lock()
_GEOCODE_LAST_REQUEST = 0.0
_GEOCODE_TTL_SECONDS = 24 * 60 * 60
_CURRENT_WEATHER_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_WEATHER_CODE_DESCRIPTIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _normalized_place(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _open_meteo_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.get(url, params=params, timeout=12)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise WeatherProviderError("The live weather provider is temporarily unavailable.") from exc
    if not isinstance(payload, dict) or payload.get("error") is True:
        reason = payload.get("reason") if isinstance(payload, dict) else None
        if isinstance(reason, str):
            raise WeatherProviderError(f"The live weather provider rejected the request: {reason}")
        raise WeatherProviderError("The live weather provider returned an invalid response.")
    return payload


def _resolve_current_location(query: str) -> dict[str, Any]:
    normalized = " ".join(query.split()).casefold()
    cached = _GEOCODE_CACHE.get(f"open-meteo:{normalized}")
    if cached and time.monotonic() - cached[0] < _GEOCODE_TTL_SECONDS:
        return cached[1]

    representative_for: str | None = None
    geocode_query = query.strip()
    broad_kashmir = normalized in {"kashmir", "kashmir region", "jammu and kashmir"}
    if broad_kashmir:
        geocode_query = "Srinagar"
        representative_for = query.strip()

    payload = _open_meteo_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": geocode_query, "count": 10, "language": "en", "format": "json"},
    )
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise WeatherLocationError("I couldn’t find that place. Please specify a city or region.")
    viable = [
        item for item in results
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("latitude"), (int, float))
        and isinstance(item.get("longitude"), (int, float))
        and isinstance(item.get("timezone"), str)
    ]
    if not viable:
        raise WeatherLocationError("Location lookup returned no usable coordinates.")

    if broad_kashmir:
        viable = [
            item for item in viable
            if _normalized_place(str(item.get("name", ""))) == "srinagar"
            and str(item.get("country_code", "")).upper() == "IN"
        ]
        if not viable:
            raise WeatherLocationError(
                "I couldn’t verify Srinagar, India as a representative Kashmir location."
            )
    else:
        exact = [
            item for item in viable
            if _normalized_place(str(item.get("name", ""))) == _normalized_place(geocode_query)
        ]
        candidates = exact or viable
        identities = {
            (
                str(item.get("name", "")).casefold(),
                str(item.get("admin1", "")).casefold(),
                str(item.get("country_code", "")).upper(),
            )
            for item in candidates
        }
        if len(identities) > 1:
            choices = sorted({
                ", ".join(part for part in (name.title(), admin.title(), country) if part)
                for name, admin, country in identities
            })
            raise WeatherLocationError(
                "That place name is ambiguous. Please specify a country, for example: "
                + ", ".join(choices[:4])
            )
        viable = candidates

    selected = viable[0]
    location = {
        "name": str(selected["name"]),
        "latitude": float(selected["latitude"]),
        "longitude": float(selected["longitude"]),
        "timezone": str(selected["timezone"]),
        "country": selected.get("country"),
        "country_code": selected.get("country_code"),
        "requested_name": query.strip(),
        "representative_for": representative_for,
        "representative_note": (
            "Srinagar is a representative city, not a region-wide observation; "
            "conditions may differ elsewhere in Kashmir."
            if representative_for else None
        ),
    }
    _GEOCODE_CACHE[f"open-meteo:{normalized}"] = (time.monotonic(), location)
    return location


def get_current_weather(location_query: str, *, units: str = "celsius") -> dict[str, Any]:
    """Return a provider-reported current observation, never a climate estimate."""
    if units not in {"celsius", "fahrenheit"}:
        raise ValueError("Temperature units must be celsius or fahrenheit.")
    if not location_query.strip():
        raise WeatherLocationError("Please specify a city or region for current weather.")

    location = _resolve_current_location(location_query)
    cache_key = f"{location['latitude']:.5f},{location['longitude']:.5f},{units}"
    cached = _CURRENT_WEATHER_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]

    params = {
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "current": (
            "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,"
            "precipitation,weather_code,cloud_cover,pressure_msl,"
            "wind_speed_10m,wind_direction_10m,wind_gusts_10m"
        ),
        "temperature_unit": units,
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
        "timezone": location["timezone"],
    }
    payload = _open_meteo_json(
        "https://api.open-meteo.com/v1/forecast",
        params,
    )
    current = payload.get("current")
    current_units = payload.get("current_units")
    if not isinstance(current, dict) or not isinstance(current.get("time"), str):
        raise WeatherProviderError("The weather provider returned no current observation.")
    if not isinstance(current_units, dict):
        current_units = {}
    weather_code = current.get("weather_code")
    observation = {
        "status": "LIVE_CURRENT_OBSERVATION",
        "provider": "Open-Meteo",
        "location": location,
        "observed_at": current["time"],
        "timezone": payload.get("timezone") or location["timezone"],
        "utc_offset_seconds": payload.get("utc_offset_seconds"),
        "conditions": _WEATHER_CODE_DESCRIPTIONS.get(weather_code, "Conditions unavailable"),
        "weather_code": weather_code,
        "is_day": current.get("is_day"),
        "temperature": current.get("temperature_2m"),
        "temperature_unit": current_units.get("temperature_2m", "°C" if units == "celsius" else "°F"),
        "feels_like": current.get("apparent_temperature"),
        "humidity": current.get("relative_humidity_2m"),
        "wind_speed": current.get("wind_speed_10m"),
        "wind_speed_unit": current_units.get("wind_speed_10m", "km/h"),
        "wind_direction": current.get("wind_direction_10m"),
        "wind_gusts": current.get("wind_gusts_10m"),
        "precipitation": current.get("precipitation"),
        "precipitation_unit": current_units.get("precipitation", "mm"),
        "cloud_cover": current.get("cloud_cover"),
        "pressure": current.get("pressure_msl"),
        "pressure_unit": current_units.get("pressure_msl", "hPa"),
        "forecast_horizon_days": 16,
    }
    if not isinstance(observation["temperature"], (int, float)):
        raise WeatherProviderError("The weather provider returned no current temperature.")
    _CURRENT_WEATHER_CACHE[cache_key] = (time.monotonic(), observation)
    return observation


def _resolve_location(query: str) -> dict[str, Any]:
    global _GEOCODE_LAST_REQUEST
    normalized = " ".join(query.split()).casefold()
    cached = _GEOCODE_CACHE.get(normalized)
    if cached and time.monotonic() - cached[0] < _GEOCODE_TTL_SECONDS:
        return cached[1]

    with _GEOCODE_LOCK:
        cached = _GEOCODE_CACHE.get(normalized)
        if cached and time.monotonic() - cached[0] < _GEOCODE_TTL_SECONDS:
            return cached[1]
        wait = 1.0 - (time.monotonic() - _GEOCODE_LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        try:
            response = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query.strip(), "format": "jsonv2", "limit": 5, "addressdetails": 1},
                headers={"User-Agent": "WayfarerLocalTravelPlanner/1.0"},
                timeout=10,
            )
            _GEOCODE_LAST_REQUEST = time.monotonic()
            response.raise_for_status()
            results = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise WeatherLocationError("Location lookup is temporarily unavailable.") from exc

    if not isinstance(results, list) or not results:
        raise WeatherLocationError("I couldn’t find that location. Please specify a city or region.")
    viable = [
        result for result in results
        if isinstance(result, dict)
        and result.get("lat") is not None
        and result.get("lon") is not None
        and isinstance(result.get("display_name"), str)
    ]
    if not viable:
        raise WeatherLocationError("Location lookup returned no usable coordinates.")

    def score(result: dict[str, Any]) -> float:
        raw = result.get("importance")
        return float(raw) if isinstance(raw, (int, float)) else 0.0

    viable.sort(key=score, reverse=True)
    top = viable[0]
    if len(viable) > 1 and score(top) - score(viable[1]) < 0.05:
        first_address = top.get("address", {})
        second_address = viable[1].get("address", {})
        first_city = first_address.get("city") or first_address.get("town") or first_address.get("state")
        second_city = second_address.get("city") or second_address.get("town") or second_address.get("state")
        if first_city != second_city:
            raise WeatherLocationError(
                f"That location is ambiguous. Please add its state or country (for example, “{query}, India”)."
            )

    location = {
        "name": top["display_name"],
        "latitude": float(top["lat"]),
        "longitude": float(top["lon"]),
    }
    _GEOCODE_CACHE[normalized] = (time.monotonic(), location)
    return location


def _normalize_forecasts(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    daily_by_date: dict[str, dict[str, Any]] = {}
    timezone_name: str | None = None
    groups = payload.get("data")
    if not isinstance(groups, list):
        raise ValueError("LiteAPI returned an unexpected weather response.")

    for group in groups:
        if not isinstance(group, dict):
            continue
        weather_data = group.get("weatherData")
        if isinstance(weather_data, list):
            for item in weather_data:
                detail = item.get("detailedWeatherData") if isinstance(item, dict) else None
                if not isinstance(detail, dict):
                    continue
                if isinstance(detail.get("timezone"), str):
                    timezone_name = detail["timezone"]
                days = detail.get("daily")
                if not isinstance(days, list):
                    continue
                for day in days:
                    if not isinstance(day, dict) or not isinstance(day.get("date"), str):
                        continue
                    temperature = day.get("temp") if isinstance(day.get("temp"), dict) else {}
                    weather = day.get("weather")
                    description = None
                    if isinstance(weather, list) and weather and isinstance(weather[0], dict):
                        description = weather[0].get("description") or weather[0].get("main")
                    daily_by_date[day["date"]] = {
                        "date": day["date"],
                        "summary": day.get("summary") or description,
                        "temperature_min": temperature.get("min"),
                        "temperature_max": temperature.get("max"),
                        "precipitation_probability": day.get("pop"),
                        "precipitation_total": None,
                        "wind_speed": day.get("wind_speed"),
                        "humidity": day.get("humidity"),
                        "conditions": description,
                    }
        daily_weather = group.get("weather")
        if isinstance(daily_weather, list):
            for item in daily_weather:
                daily = item.get("dailyWeather") if isinstance(item, dict) else None
                if not isinstance(daily, dict) or not isinstance(daily.get("date"), str):
                    continue
                entry = daily_by_date.setdefault(daily["date"], {"date": daily["date"]})
                temperature = daily.get("temperature") if isinstance(daily.get("temperature"), dict) else {}
                precipitation = daily.get("precipitation") if isinstance(daily.get("precipitation"), dict) else {}
                wind = daily.get("wind") if isinstance(daily.get("wind"), dict) else {}
                maximum_wind = wind.get("max") if isinstance(wind.get("max"), dict) else {}
                entry.update({
                    "temperature_min": entry.get("temperature_min", temperature.get("min")),
                    "temperature_max": entry.get("temperature_max", temperature.get("max")),
                    "precipitation_total": precipitation.get("total"),
                    "wind_speed": entry.get("wind_speed", maximum_wind.get("speed")),
                })

    forecasts = [daily_by_date[key] for key in sorted(daily_by_date)]
    if not forecasts:
        raise ValueError("LiteAPI did not return forecast days for that period.")
    return forecasts, timezone_name


def get_weather(
    location_query: str,
    start_date: date,
    end_date: date,
    *,
    units: str = "metric",
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("The weather end date must be on or after the start date.")
    if end_date - start_date > timedelta(days=15):
        raise ValueError("Weather requests can cover at most 16 days at a time.")
    location = _resolve_location(location_query)
    client = LiteAPIClient(timeout=20)
    response = client.get_weather(
        latitude=location["latitude"],
        longitude=location["longitude"],
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        units=units,
    )
    forecasts, timezone_name = _normalize_forecasts(response)
    today = date.today()
    short_term = (end_date - today).days <= 7 and (start_date - today).days <= 7
    return {
        "status": "LIVE_PROVIDER_DATA",
        "provider": "LiteAPI",
        "location": location,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timezone": timezone_name,
        "units": units,
        "forecast_horizon": "short_term" if short_term else "long_range",
        "accuracy_note": None if short_term else "Forecasts beyond one week are less reliable and should be treated as a long-range outlook.",
        "forecasts": forecasts,
        "sandbox": response.get("sandbox"),
    }
