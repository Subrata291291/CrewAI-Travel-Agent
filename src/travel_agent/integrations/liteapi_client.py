
import os
import re
from typing import Any
from datetime import date

import requests
from dotenv import load_dotenv


class LiteAPIError(RuntimeError):
    """Raised when a LiteAPI request fails."""

    def __init__(self, message: str, *, status_code: int | None = None, provider_code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code


class LiteAPIClient:
    """Small server-side client for Nuitee Connect / LiteAPI."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 12,
    ) -> None:
        load_dotenv()

        self.api_key = (
            api_key or os.getenv("NUITEE_API_KEY") or ""
        ).strip()

        self.base_url = (
            base_url or os.getenv("NUITEE_BASE_URL")
            or "https://api.liteapi.travel/v3.0"
        ).rstrip("/")

        self.timeout = timeout

        if not self.api_key:
            raise ValueError(
                "NUITEE_API_KEY is missing from the environment."
            )

        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": self.api_key,
            "Accept": "application/json",
        })

    def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make a request without exposing the API key in logs."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        try:
            response = self.session.request(
                method=method,
                url=url,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.Timeout as exc:
            raise LiteAPIError(
                "LiteAPI request timed out. Try again later."
            ) from exc
        except requests.RequestException as exc:
            raise LiteAPIError(
                "Could not connect to LiteAPI. Check network settings."
            ) from exc

        try:
            if response.status_code == 204:
                return {"data": []}
            payload = response.json()
        except ValueError as exc:
            raise LiteAPIError(
                f"LiteAPI returned non-JSON data "
                f"(HTTP {response.status_code})."
            ) from exc

        if not response.ok:
            detail = ""
            provider_code = None
            if isinstance(payload, dict):
                detail = str(
                    payload.get("message")
                    or payload.get("error")
                    or payload.get("description")
                    or ""
                )
                raw_code = payload.get("errorCode") or payload.get("code") or (payload.get("error") if isinstance(payload.get("error"), str) else None)
                if isinstance(raw_code, (str, int)) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(raw_code)):
                    provider_code = str(raw_code)

            message = f"HTTP {response.status_code}"
            if detail:
                message += f": {detail[:300]}"

            raise LiteAPIError(f"LiteAPI request failed ({message}).", status_code=response.status_code, provider_code=provider_code)

        if not isinstance(payload, dict):
            raise LiteAPIError(
                "LiteAPI returned an unexpected response format."
            )

        return payload

    def search_places(
        self,
        text_query: str,
        *,
        place_type: str = "locality",
    ) -> list[dict[str, Any]]:
        """Search for destination places, such as Goa, India."""
        if not text_query.strip():
            raise ValueError("Place search query cannot be empty.")

        payload = self._request(
            "GET",
            "/data/places",
            params={"textQuery": text_query.strip(), "type": place_type},
        )

        places = payload.get("data")

        if not isinstance(places, list):
            raise LiteAPIError(
                "The place-search response has no valid data list."
            )

        return [
            place for place in places
            if isinstance(place, dict)
        ]

    def list_iata_codes(self) -> list[dict[str, Any]]:
        """Return LiteAPI's complete airport IATA reference directory."""
        payload = self._request("GET", "/data/iataCodes")
        data = payload.get("data")
        if not isinstance(data, list):
            raise LiteAPIError("LiteAPI returned an invalid airport directory.")
        return [row for row in data if isinstance(row, dict)]

    def list_countries(self) -> list[dict[str, Any]]:
        """Return LiteAPI's ISO country-code reference data."""
        payload = self._request("GET", "/data/countries")
        data = payload.get("data")
        if not isinstance(data, list):
            raise LiteAPIError("LiteAPI returned invalid country reference data.")
        return [row for row in data if isinstance(row, dict)]

    def get_weather(
        self,
        *,
        latitude: float,
        longitude: float,
        start_date: str,
        end_date: str,
        units: str = "metric",
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/data/weather",
            params={
                "latitude": str(latitude),
                "longitude": str(longitude),
                "startDate": start_date,
                "endDate": end_date,
                "units": units,
            },
        )



    def search_hotel_rates(
        self,
        *,
        checkin: str,
        checkout: str,
        occupancies: list[dict[str, Any]],
        place_id: str | None = None,
        city_name: str | None = None,
        country_code: str | None = None,
        currency: str = "INR",
        guest_nationality: str = "IN",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search hotel availability and rates for a destination."""

        using_city = bool(city_name and country_code)

        if place_id and using_city:
            raise ValueError(
                "Use either place_id or city_name/country_code, not both."
            )

        guest_nationality = guest_nationality.strip().upper()
        
        if using_city:
            location_payload = {
                "cityName": city_name.strip(),
                "countryCode": country_code.strip().upper(),
            }
        elif place_id and place_id.strip():
            location_payload = {"placeId": place_id.strip()}
        else:
            raise ValueError(
                "Provide a place ID or both city name and country code."
            )

        try:
            checkin_date = date.fromisoformat(checkin)
            checkout_date = date.fromisoformat(checkout)
        except ValueError as exc:
            raise ValueError(
                "Dates must use YYYY-MM-DD format."
            ) from exc

        if checkout_date <= checkin_date:
            raise ValueError("Checkout must be after check-in.")

        if not occupancies:
            raise ValueError("At least one room occupancy is required.")

        
        for index, occupancy in enumerate(occupancies, start=1):
            adults = occupancy.get("adults", 0)
            children_ages = occupancy.get("children", [])

            if not isinstance(adults, int) or adults < 1:
                raise ValueError(
                    f"Room {index} must have at least one adult."
                )

            if not isinstance(children_ages, list):
                raise ValueError(
                    f"Room {index} children must be a list of ages."
                )

            if any(
                not isinstance(age, int) or isinstance(age, bool)
                or age < 0 or age > 17
                for age in children_ages
            ):
                raise ValueError(
                    f"Room {index} has an invalid child age."
                )


        payload = {
            **location_payload,
            "checkin": checkin,
            "checkout": checkout,
            "currency": currency,
            "guestNationality": guest_nationality,
            "occupancies": occupancies,
            "roomMapping": True,
            "maxRatesPerHotel": 1,
            "includeHotelData": True,
            "limit": limit,
            "timeout": 10,
        }

        return self._request(
            "POST",
            "/hotels/rates",
            json=payload,
        )
