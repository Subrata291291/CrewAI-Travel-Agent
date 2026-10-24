
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRIP_ID = input("Enter your saved Trip ID: ").strip()

if not TRIP_ID:
    print("Trip ID cannot be empty.")
    raise SystemExit(1)

TRIP_FILE = PROJECT_ROOT / "data" / "trips" / f"{TRIP_ID}.json"
PREBOOK_DIR = PROJECT_ROOT / "data" / "hotel_prebooks"


def main() -> None:
    if not TRIP_FILE.exists():
        print("Saved trip not found.")
        return

    try:
        trip_record = json.loads(
            TRIP_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read trip: {exc}")
        return

    request = trip_record.get("travel_request", {})

    files = sorted(
        PREBOOK_DIR.glob(f"{TRIP_ID}_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if not files:
        print("No saved prebook found.")
        return

    try:
        record = json.loads(files[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read prebook: {exc}")
        return

    response = record.get("prebook_response", {})
    data = response.get("data", {}) if isinstance(response, dict) else {}

    if not isinstance(data, dict):
        print("Unexpected prebook response.")
        return

    prebook_id = data.get("prebookId") or record.get("prebook_id")
    price = data.get("price")
    currency = data.get("currency")
    expected_rooms = request.get("rooms", 0)
    expected_adults = request.get("adults", 0)
    expected_currency = request.get("currency", "INR")
    budget_per_night = request.get("hotel_budget_per_night")

    try:
        from datetime import date

        checkin = date.fromisoformat(request["departure_date"])
        checkout = date.fromisoformat(request["return_date"])
        nights = (checkout - checkin).days
    except (KeyError, TypeError, ValueError):
        print("Trip dates are invalid.")
        return

    if not prebook_id:
        print("Prebook ID is missing.")
        return

    if (
        not isinstance(price, (int, float))
        or isinstance(price, bool)
        or currency != expected_currency
    ):
        print("Final price or currency is invalid.")
        return

    if not isinstance(budget_per_night, (int, float)) or nights <= 0:
        print("Budget or number of nights is invalid.")
        return

    if not isinstance(expected_rooms, int) or expected_rooms < 1:
        print("Invalid room count.")
        return

    if not isinstance(expected_adults, int) or expected_adults < expected_rooms:
        print(
            "This demo requires at least one adult per room. "
            "Handle room assignments separately for other cases."
        )
        return

    if price > budget_per_night * nights:
        print("Final price exceeds the saved trip budget.")
        return

    room_types = data.get("roomTypes", [])
    if not isinstance(room_types, list):
        print("Invalid room-rate data.")
        return

    rates = [
        rate
        for group in room_types
        if isinstance(group, dict)
        for rate in group.get("rates", [])
        if isinstance(rate, dict)
    ]

    if len(rates) != expected_rooms:
        print(
            f"Expected {expected_rooms} room rates, "
            f"but received {len(rates)}. Booking payload not prepared."
        )
        return

    # Assign at least one guest to each room and distribute extras.
    room_adults = [1] * expected_rooms
    for extra in range(expected_adults - expected_rooms):
        room_adults[extra % expected_rooms] += 1

    guests = []
    guest_number = 1

    for room_number, adults_in_room in enumerate(room_adults, start=1):
        for _ in range(adults_in_room):
            guests.append({
                "occupancyNumber": room_number,
                "firstName": f"DemoGuest{guest_number}",
                "lastName": "Example",
                "email": f"guest{guest_number}@example.com",
            })
            guest_number += 1

    payload = {
        "prebookId": prebook_id,
        "clientReference": f"TA-{TRIP_ID}",
        "holder": {
            "firstName": "DemoHolder",
            "lastName": "Example",
            "email": "holder@example.com",
        },
        "guests": guests,
        # Sandbox example only; this script never sends the request.
        "payment": {"method": "ACC_CREDIT_CARD"},
    }

    print("\n=== Booking Payload Dry Run ===")
    print("Hotel:", record.get("hotel_name"))
    print("Trip ID:", TRIP_ID)
    print("Rooms:", expected_rooms)
    print("Guests:", len(guests))
    print("Stay:", checkin, "to", checkout)
    print(f"Prebook price: {currency} {price:.2f}")
    print(
        f"Combined stay budget: {expected_currency} "
        f"{budget_per_night * nights:.2f}"
    )
    print("Prebook ID present:", bool(payload["prebookId"]))
    print("Holder fields:", ", ".join(payload["holder"].keys()))
    print("Guest occupancy numbers:", [
        guest["occupancyNumber"] for guest in guests
    ])
    print("Payment method shown: ACC_CREDIT_CARD (sandbox example only)")
    print("\nPayload structure:")
    safe_payload = {**payload, "prebookId": "<saved prebook ID>"}
    print(json.dumps(safe_payload, indent=2))
    print("\nDRY RUN ONLY: no network request, booking or payment was made.")
    print(
        "Replace demo guest details with verified traveler details "
        "and configure the correct payment flow before implementation."
    )


if __name__ == "__main__":
    main()
