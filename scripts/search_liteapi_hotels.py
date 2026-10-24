
import sys
from pathlib import Path
from typing import Any
import json
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.travel_agent.integrations.liteapi_client import (
    LiteAPIClient,
    LiteAPIError,
)
from src.travel_agent.memory.trip_store import load_trip


TRIP_ID = input("Enter your saved Trip ID: ").strip()

if not TRIP_ID:
    print("Trip ID cannot be empty.")
    raise SystemExit(1)

GOA_PLACE_ID = "ChIJQbc2YxC6vzsRkkDzYv-H-Oo"


def get_offer_total(
    offer: dict[str, Any],
    expected_rooms: int,
) -> tuple[float, str] | None:
    """Return a combined offer total only when all room rates are present."""

    rates = offer.get("rates", [])
    if not isinstance(rates, list) or len(rates) != expected_rooms:
        return None

    # Preferred field: provider's total for all rooms in this offer.
    total_info = offer.get("offerRetailRate")

    if isinstance(total_info, dict):
        amount = total_info.get("amount")
        currency = total_info.get("currency")

        if isinstance(amount, (int, float)) and currency:
            return float(amount), str(currency)

    # Fallback: sum each room's total, only when every room has a price
    # in the same currency.
    amounts: list[float] = []
    currencies: list[str] = []

    for rate in rates:
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


def main() -> None:
    try:
        request = load_trip(TRIP_ID)

        if not request.departure_date or not request.return_date:
            print("Trip is missing check-in or check-out dates.")
            return

        if not request.rooms or not request.adults:
            print("Trip needs at least one room and one adult.")
            return

        if request.children:
            print(
                "Search paused: the saved trip records the number of "
                "children, but not their ages. Add per-room child ages "
                "before searching."
            )
            return

        if request.adults < request.rooms:
            print(
                "Search paused: this test assigns at least one adult "
                "to each room, but there are fewer adults than rooms."
            )
            return

        # Deterministic test allocation: at least one adult per room,
        # then distribute any extra adults across rooms.
        adults_per_room = [1] * request.rooms
        for extra_index in range(request.adults - request.rooms):
            adults_per_room[extra_index % request.rooms] += 1

        occupancies = [
            {"adults": adults_in_room, "children": []}
            for adults_in_room in adults_per_room
        ]

        nationality = input(
            "Guest nationality (2-letter country code, e.g. IN): "
        ).strip().upper()

        if len(nationality) != 2 or not nationality.isalpha():
            print("Please enter a valid two-letter country code.")
            return

        client = LiteAPIClient()

        print("\nSearching hotel rates from LiteAPI...")
        print(f"Destination: {request.destination}")
        print(f"Dates: {request.departure_date} to {request.return_date}")
        print(f"Rooms: {request.rooms}; occupancies: {occupancies}")
        print(f"Currency: {request.currency}")

        response = client.search_hotel_rates(
            city_name="Goa",
            country_code="IN",
            checkin=request.departure_date.isoformat(),
            checkout=request.return_date.isoformat(),
            occupancies=occupancies,
            currency=request.currency,
            guest_nationality=nationality,
            limit=100,
        )

        hotels = response.get("data", [])
        if not isinstance(hotels, list) or not hotels:
            print(
                "\nNo rate offers were returned by this request. "
                "This does not prove that no hotels are available across Goa."
            )
            return

        
        hotel_info_list = response.get("hotels", [])
        hotel_metadata = {}

        if isinstance(hotel_info_list, list):
            for info in hotel_info_list:
                if not isinstance(info, dict):
                    continue

                hotel_id = info.get("id") or info.get("hotelId")
                if hotel_id:
                    hotel_metadata[hotel_id] = info

        nights = (request.return_date - request.departure_date).days
        stay_budget = request.hotel_budget_per_night * nights

        # Keep only the cheapest verifiable offer for each hotel.
        best_by_hotel = {}

        for hotel in hotels:
            if not isinstance(hotel, dict):
                continue

            hotel_id = hotel.get("hotelId", "")
            metadata = hotel_metadata.get(hotel_id, {})
            hotel_name = (
                metadata.get("name")
                or hotel.get("name")
                or hotel_id
                or "Unknown hotel"
            )

            offers = hotel.get("roomTypes", [])
            if not isinstance(offers, list):
                continue

            for offer in offers:
                if not isinstance(offer, dict):
                    continue

                offer_id = offer.get("offerId")

                if not isinstance(offer_id, str) or not offer_id.strip():
                    continue

                price = get_offer_total(offer, request.rooms)
                if price is None:
                    continue

                total_amount, currency = price

                if currency != request.currency:
                    continue

                key = hotel_id or hotel_name
                candidate = {
                    "hotel_id": hotel_id,
                    "name": hotel_name,
                    "address": metadata.get("address", "Not provided"),
                    "city": metadata.get("city_name", "Not provided"),
                    "country": metadata.get("country_code", "Not provided"),
                    "total": total_amount,
                    "offer_id": offer_id,
                    "currency": currency,
                }

                current = best_by_hotel.get(key)
                if current is None or total_amount < current["total"]:
                    best_by_hotel[key] = candidate

        
        all_hotels = sorted(
            best_by_hotel.values(),
            key=lambda item: item["total"],
        )

        results_dir = PROJECT_ROOT / "data" / "hotel_search_results"
        results_dir.mkdir(parents=True, exist_ok=True)

        results_file = results_dir / f"{TRIP_ID}.json"

        saved_payload = {
            "trip_id": TRIP_ID,
            "destination": request.destination,
            "checkin": request.departure_date.isoformat(),
            "checkout": request.return_date.isoformat(),
            "currency": request.currency,
            "guest_nationality": nationality,
            "rooms": request.rooms,
            "occupancies": occupancies,
            "budget_per_night_all_rooms": (
                request.hotel_budget_per_night
            ),
            "total_stay_budget": stay_budget,
            "nights": nights,
            "searched_at": datetime.now().astimezone().isoformat(),
            "offers": [
                {
                    "hotel_id": hotel["hotel_id"],
                    "name": hotel["name"],
                    "city": hotel["city"],
                    "address": hotel["address"],
                    "country_code": hotel["country"],
                    "offer_id": hotel["offer_id"],
                    "total_stay_price": hotel["total"],
                    "currency": hotel["currency"],
                    "within_budget": hotel["total"] <= stay_budget,
                }
                for hotel in all_hotels
                if hotel.get("offer_id")
            ],
        }

        results_file.write_text(
            json.dumps(saved_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        print(
            f"\nSaved {len(saved_payload['offers'])} hotel offers to:"
        )
        print(results_file)


        affordable = [
            hotel for hotel in all_hotels
            if hotel["total"] <= stay_budget
        ]

        if affordable:
            displayed_hotels = affordable
            print(
                f"\nHotels within combined budget: {len(affordable)}"
            )
        else:
            displayed_hotels = all_hotels[:5]
            print(
                "\nNo verifiable hotel offers were within the budget. "
                "Showing the cheapest returned hotels instead."
            )

        print(
            f"Combined stay budget: "
            f"{request.currency} {stay_budget:.2f}"
        )
        print("-" * 60)

        for hotel in displayed_hotels:
            total = hotel["total"]
            nightly_average = total / nights

            print(f"\nHotel: {hotel['name']}")
            print(f"Hotel ID: {hotel['hotel_id'] or 'Not provided'}")
            print(f"City: {hotel['city']}")
            print(f"Address: {hotel['address']}")
            print(f"Country code: {hotel['country']}")
            print(
                f"Total for {request.rooms} rooms, {nights} nights: "
                f"{hotel['currency']} {total:.2f}"
            )
            print(
                f"Combined nightly average: "
                f"{hotel['currency']} {nightly_average:.2f}"
            )

            status = (
                "WITHIN budget"
                if total <= stay_budget
                else "ABOVE budget"
            )
            print(f"Budget status: {status}")
            print(
                "Verify taxes, room details, cancellation policy, "
                "and final payable amount before booking."
            )
            print("-" * 60)


    except FileNotFoundError:
        print("Saved trip not found. Check the Trip ID.")
    except (ValueError, LiteAPIError) as exc:
        print(f"Hotel search failed: {exc}")
    except OSError as exc:
        print(f"Could not access local trip data: {exc}")


if __name__ == "__main__":
    main()
