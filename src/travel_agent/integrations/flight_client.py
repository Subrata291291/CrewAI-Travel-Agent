"""LiteAPI Flights adapter. Flight access must be enabled on the account."""
from __future__ import annotations

from typing import Any

from src.travel_agent.integrations.liteapi_client import LiteAPIClient


class FlightProviderNotConfigured(RuntimeError):
    code = "FLIGHT_PROVIDER_NOT_CONFIGURED"


class LiteAPIFlights(LiteAPIClient):
    """Flight API operations documented by Nuitee Connect."""

    def search(self, **criteria: Any) -> dict[str, Any]:
        return self._request("POST", "/flights/rates", json=criteria)

    def verify(self, offer_id: str) -> dict[str, Any]:
        return self._request("POST", "/flights/verify", json={"offerId": offer_id})

    def prebook(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/flights/prebooks", json=payload)

    def book(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/flights/bookings", json=payload)

    def get_booking(self, booking_id: str) -> dict[str, Any]:
        return self._request("GET", f"/flights/bookings/{booking_id}")
