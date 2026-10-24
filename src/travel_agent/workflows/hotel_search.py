
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.travel_agent.integrations.liteapi_client import (
    LiteAPIClient,
    LiteAPIError,
)
from src.travel_agent.memory.trip_store import load_trip


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _get_offer_total(
    offer: dict[str, Any],
    expected_rooms: int,
) -> tuple[float, str] | None:
    """Return the combined offer price when all room rates are present."""

    rates = offer.get("rates", [])

    if not isinstance(rates, list) or len(rates) != expected_rooms:
        return None

    total_info = offer.get("offerRetailRate")

    if isinstance(total_info, dict):
        amount = total_info.get("amount")
        currency = total_info.get("currency")

        if isinstance(amount, (int, float)) and currency:
            return float(amount), str(currency)

    amounts: list[float] = []
    currencies: list[str] = []

    for rate in rates:
        if not isinstance(rate, dict):
            return None

        retail_rate = rate.get("retailRate") or {}
        totals = retail_rate.get("total") or []

        if not totals or not isinstance(totals[0], dict):
            return None

        amount = totals[0].get("amount")
        currency = totals[0].get("currency")

        if not isinstance(amount, (int, float)) or not currency:
            return None

        amounts.append(float(amount))
        currencies.append(str(currency))

    if len(set(currencies)) != 1:
        return None

    return sum(amounts), currencies[0]


def search_hotels_for_trip(
    trip_id: str,
    *,
    country_code: str,
    guest_nationality: str = "IN",
    limit: int = 100,
    place_id: str | None = None,
    place_name: str | None = None,
    place_address: str | None = None,
) -> Path:
    """Search and save hotel offers for a previously saved trip."""

    request = load_trip(trip_id)

    if request.status != "ready_to_search":
        raise ValueError("Trip is not ready for a hotel search.")

    if not request.departure_date or not request.return_date:
        raise ValueError("Trip dates are required.")

    if request.return_date <= request.departure_date:
        raise ValueError("Checkout must be after check-in.")

    if request.children:
        raise ValueError(
            "Child ages and per-room occupancy must be collected "
            "before searching for a trip with children."
        )

    if request.rooms < 1 or request.adults < request.rooms:
        raise ValueError(
            "This workflow requires at least one adult per room."
        )

    country_code = country_code.strip().upper()
    guest_nationality = guest_nationality.strip().upper()

    if len(country_code) != 2 or not country_code.isalpha():
        raise ValueError("Country code must contain two letters.")

    if len(guest_nationality) != 2 or not guest_nationality.isalpha():
        raise ValueError("Guest nationality must contain two letters.")

    nights = (request.return_date - request.departure_date).days
    stay_budget = request.hotel_budget_per_night * nights

    adults_per_room = [1] * request.rooms
    for extra in range(request.adults - request.rooms):
        adults_per_room[extra % request.rooms] += 1

    occupancies = [
        {"adults": adults_in_room, "children": []}
        for adults_in_room in adults_per_room
    ]

    client = LiteAPIClient()

    response = client.search_hotel_rates(
        place_id=place_id,
        city_name=None if place_id else request.destination,
        country_code=None if place_id else country_code,
        checkin=request.departure_date.isoformat(),
        checkout=request.return_date.isoformat(),
        occupancies=occupancies,
        currency=request.currency,
        guest_nationality=guest_nationality,
        limit=limit,
    )

    hotels = response.get("data")
    if not isinstance(hotels, list):
        top_level_fields = ", ".join(sorted(str(key) for key in response.keys())[:12])
        data_value = response.get("data")
        data_fields = (
            ", ".join(sorted(str(key) for key in data_value.keys())[:12])
            if isinstance(data_value, dict)
            else None
        )
        shape = f"Top-level fields: {top_level_fields or 'none'}."
        if data_fields:
            shape += f" Fields inside data: {data_fields}."
        raise LiteAPIError(
            "LiteAPI did not return its documented hotel-rate list. "
            f"{shape} No empty result was saved."
        )

    metadata_list = response.get("hotels", [])
    metadata_by_id: dict[str, dict[str, Any]] = {}

    if isinstance(metadata_list, list):
        for item in metadata_list:
            if not isinstance(item, dict):
                continue

            hotel_id = item.get("id") or item.get("hotelId")
            if hotel_id:
                metadata_by_id[str(hotel_id)] = item

    # Keep only the cheapest verifiable offer for each hotel.
    best_by_hotel: dict[str, dict[str, Any]] = {}
    room_rates_returned = 0
    offers_missing_id = 0
    offers_missing_price = 0
    offers_currency_mismatch = 0
    usable_offers = 0

    for hotel in hotels:
        if not isinstance(hotel, dict):
            continue

        hotel_id = str(hotel.get("hotelId", ""))
        metadata = metadata_by_id.get(hotel_id, {})

        hotel_name = (
            metadata.get("name")
            or hotel.get("name")
            or hotel_id
            or "Unknown hotel"
        )

        room_types = hotel.get("roomTypes", [])
        if not isinstance(room_types, list):
            continue

        for offer in room_types:
            if not isinstance(offer, dict):
                continue

            room_rates_returned += 1
            offer_id = offer.get("offerId")
            if not isinstance(offer_id, str) or not offer_id.strip():
                offers_missing_id += 1
                continue

            price = _get_offer_total(offer, request.rooms)
            if price is None:
                offers_missing_price += 1
                continue

            amount, currency = price
            if currency != request.currency:
                offers_currency_mismatch += 1
                continue

            usable_offers += 1
            key = hotel_id or hotel_name

            candidate = {
                "hotel_id": hotel_id,
                "name": hotel_name,
                "city": metadata.get(
                    "city_name",
                    hotel.get("city", request.destination),
                ),
                "address": metadata.get(
                    "address",
                    hotel.get("address", "Not provided"),
                ),
                "country_code": metadata.get(
                    "country_code",
                    country_code,
                ),
                "offer_id": offer_id,
                "total_stay_price": amount,
                "currency": currency,
                "within_budget": amount <= stay_budget,
                "room_details": [
                    {
                        key: rate.get(key)
                        for key in ("name", "roomName", "boardName", "mealPlan", "cancellationPolicies")
                        if rate.get(key) is not None
                    }
                    for rate in offer.get("rates", [])
                    if isinstance(rate, dict)
                ],
            }

            previous = best_by_hotel.get(key)
            if previous is None or amount < previous["total_stay_price"]:
                best_by_hotel[key] = candidate

    sorted_offers = sorted(
        best_by_hotel.values(),
        key=lambda item: item["total_stay_price"],
    )

    results_dir = PROJECT_ROOT / "data" / "hotel_search_results"
    results_dir.mkdir(parents=True, exist_ok=True)

    output_path = results_dir / f"{trip_id}.json"

    payload = {
        "trip_id": trip_id,
        "destination": request.destination,
        "search_location": {
            "place_id": place_id,
            "name": place_name or request.destination,
            "formatted_address": place_address or place_name or request.destination,
        },
        "checkin": request.departure_date.isoformat(),
        "checkout": request.return_date.isoformat(),
        "currency": request.currency,
        "rooms": request.rooms,
        "occupancies": occupancies,
        "budget_per_night_all_rooms": request.hotel_budget_per_night,
        "total_stay_budget": stay_budget,
        "nights": nights,
        "searched_at": datetime.now().astimezone().isoformat(),
        "offers": sorted_offers,
        "search_diagnostics": {
            "provider_hotels_returned": sum(1 for hotel in hotels if isinstance(hotel, dict)),
            "room_rates_returned": room_rates_returned,
            "offers_missing_id": offers_missing_id,
            "offers_missing_price": offers_missing_price,
            "offers_currency_mismatch": offers_currency_mismatch,
            "usable_offers": usable_offers,
        },
    }

    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\nHotel search completed for {request.destination}.")
    print(f"Offers saved: {len(sorted_offers)}")
    print(f"Offers within budget: {sum(o['within_budget'] for o in sorted_offers)}")
    print(f"Saved to: {output_path}")

    return output_path
