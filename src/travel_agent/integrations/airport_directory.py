"""Cached airport search data enriched with maintained city metadata."""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from typing import Any

import airportsdata

from src.travel_agent.integrations.liteapi_client import LiteAPIClient

logger = logging.getLogger(__name__)
_CACHE_SECONDS = 24 * 60 * 60
_cache_loaded_at = 0.0
_cache: list[dict[str, str]] = []
_cache_lock = threading.Lock()


class AirportDirectoryError(RuntimeError):
    """Reference data could not be loaded or normalized."""


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", value).strip().casefold()


def _local_airports() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    by_iata = airportsdata.load("IATA")
    mac_city_by_airport: dict[str, str] = {}
    for city in airportsdata.load_iata_macs().values():
        city_name = city.get("name")
        airports = city.get("airports")
        if isinstance(city_name, str) and isinstance(airports, dict):
            for code in airports:
                if isinstance(code, str):
                    mac_city_by_airport[code.upper()] = city_name
    return by_iata, mac_city_by_airport


def get_airport_directory() -> list[dict[str, str]]:
    """Load and cache airport codes, names, and city/country metadata.

    LiteAPI is the primary reference for selectable IATA codes. The local
    airportsdata package supplies city/municipality names and supplements
    provider records when LiteAPI's directory omits an airport.
    """
    global _cache_loaded_at, _cache
    now = time.monotonic()
    if _cache and now - _cache_loaded_at < _CACHE_SECONDS:
        return _cache

    with _cache_lock:
        now = time.monotonic()
        if _cache and now - _cache_loaded_at < _CACHE_SECONDS:
            return _cache

        try:
            local, mac_city_by_airport = _local_airports()
        except Exception as exc:
            logger.warning("Local airport reference data unavailable: %s", type(exc).__name__)
            raise AirportDirectoryError("Airport city reference data is unavailable. Install the project requirements and try again.") from exc

        provider_rows: list[dict[str, Any]] = []
        country_names: dict[str, str] = {}
        try:
            client = LiteAPIClient(timeout=12)
            provider_rows = client.list_iata_codes()
            try:
                countries = client.list_countries()
                country_names = {
                    row["code"].strip().upper(): row["name"].strip()
                    for row in countries
                    if isinstance(row.get("code"), str) and isinstance(row.get("name"), str)
                }
            except Exception as exc:
                logger.info("LiteAPI country reference data unavailable: %s", type(exc).__name__)
        except Exception as exc:
            # The maintained bundled dataset keeps autocomplete available if
            # credentials or LiteAPI reference data are temporarily unavailable.
            logger.info("LiteAPI airport directory unavailable; using local reference data: %s", type(exc).__name__)

        provider_by_code: dict[str, dict[str, Any]] = {}
        for row in provider_rows:
            code, name = row.get("code"), row.get("name")
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z]{3}", code.strip()) and isinstance(name, str) and name.strip():
                provider_by_code[code.strip().upper()] = row

        records: dict[str, dict[str, str]] = {}
        for code, local_row in local.items():
            code = code.strip().upper() if isinstance(code, str) else ""
            if not re.fullmatch(r"[A-Z]{3}", code):
                continue
            provider = provider_by_code.get(code, {})
            airport_name = provider.get("name") if isinstance(provider.get("name"), str) else local_row.get("name")
            city_name = mac_city_by_airport.get(code) or local_row.get("city") or ""
            country_code = provider.get("countryCode") if isinstance(provider.get("countryCode"), str) else local_row.get("country", "")
            country_code = country_code.strip().upper() if isinstance(country_code, str) else ""
            if not isinstance(airport_name, str) or not airport_name.strip() or not isinstance(city_name, str):
                continue
            records[code] = {
                "iata_code": code,
                "airport_name": airport_name.strip(),
                "city_name": city_name.strip(),
                "country_name": country_names.get(country_code, country_code),
                "country_code": country_code,
                "subdivision": str(local_row.get("subd") or "").strip(),
            }

        # Keep any provider codes the local city dataset does not yet carry.
        # Such records remain searchable by the provider's airport name/code.
        for code, provider in provider_by_code.items():
            if code in records:
                continue
            country_code = provider.get("countryCode", "")
            country_code = country_code.strip().upper() if isinstance(country_code, str) else ""
            records[code] = {
                "iata_code": code,
                "airport_name": provider["name"].strip(),
                "city_name": str(provider.get("city") or "").strip(),
                "country_name": country_names.get(country_code, country_code),
                "country_code": country_code,
                "subdivision": "",
            }

        if not records:
            raise AirportDirectoryError("Airport reference data is temporarily unavailable. Please try again later.")

        _cache = sorted(records.values(), key=lambda airport: (airport["city_name"].casefold(), airport["airport_name"].casefold(), airport["iata_code"]))
        _cache_loaded_at = time.monotonic()
        return _cache


def search_airports(query: str, limit: int = 10) -> list[dict[str, str]]:
    needle = _normalize(query)
    airports = get_airport_directory()
    matches: list[tuple[int, dict[str, str]]] = []
    for airport in airports:
        city = _normalize(airport["city_name"])
        name = _normalize(airport["airport_name"])
        country = _normalize(airport["country_name"])
        subdivision = _normalize(airport["subdivision"])
        code = airport["iata_code"].casefold()
        haystack = " ".join((name, city, country, subdivision, code))
        # Prefer a city/subdivision match for a word such as "Goa", which
        # also happens to be the IATA code GOA for Genoa. Uppercase three
        # letter input remains a direct code lookup.
        if needle == code and query.strip().isupper() and len(query.strip()) == 3:
            score = 0
        elif city == needle or subdivision == needle:
            score = 0
        elif city.startswith(needle) or subdivision.startswith(needle):
            score = 1
        elif name.startswith(needle):
            score = 2
        elif needle == code:
            score = 2
        elif needle in haystack:
            score = 3
        else:
            continue
        matches.append((score, airport))

    matches.sort(key=lambda item: (item[0], item[1]["city_name"].casefold(), item[1]["airport_name"].casefold(), item[1]["iata_code"]))
    return [airport for _, airport in matches[:limit]]


def is_valid_airport_code(code: str) -> bool:
    normalized = code.strip().upper()
    return any(airport["iata_code"] == normalized for airport in get_airport_directory())
