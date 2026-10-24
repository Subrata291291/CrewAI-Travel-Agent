
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

TRIP_ID = input("Enter your saved Trip ID: ").strip()

if not TRIP_ID:
    print("Trip ID cannot be empty.")
    raise SystemExit(1)

TRIP_FILE = PROJECT_ROOT / "data" / "trips" / f"{TRIP_ID}.json"
PREBOOK_DIR = PROJECT_ROOT / "data" / "hotel_prebooks"
BOOKING_DIR = PROJECT_ROOT / "data" / "hotel_bookings"

BOOK_URL = "https://book.liteapi.travel/v3.0/rates/book"


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def main() -> None:
    api_key = (os.getenv("NUITEE_API_KEY") or "").strip()

    # Never allow this test script to submit using a production key.
    if not api_key.startswith(("sand_", "sandbox_")):
        print("STOP: This script accepts sandbox keys only.")
        print("No booking request was sent.")
        return

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

        # Prevent another booking attempt for a trip that already
    # has a confirmed booking or an unresolved previous attempt.
    protected_statuses = {
        "CONFIRMED_SANDBOX",
        "SUBMISSION_STARTED",
        "NEEDS_RECONCILIATION",
        "UNKNOWN",
    }

    if BOOKING_DIR.exists():
        for booking_file in BOOKING_DIR.glob("*.json"):
            try:
                with booking_file.open("r", encoding="utf-8") as file:
                    existing_record = json.load(file)
            except (OSError, json.JSONDecodeError):
                continue

            if (
                existing_record.get("trip_id") == TRIP_ID
                and existing_record.get("status") in protected_statuses
            ):
                print("\nSTOP: This trip already has a booking record")
                print("that is confirmed or needs reconciliation.")
                print("Existing status:", existing_record.get("status"))
                print("Record:", booking_file)
                print("No new booking request was sent.")
                return

    # Load the latest saved prebook for this trip.
    prebook_files = sorted(
        PREBOOK_DIR.glob(f"{TRIP_ID}_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if not prebook_files:
        print("No saved prebook was found. Search and prebook first.")
        return

    try:
        prebook_record = json.loads(
            prebook_files[0].read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read prebook data: {exc}")
        return

    prebook_response = prebook_record.get("prebook_response", {})
    data = (
        prebook_response.get("data", {})
        if isinstance(prebook_response, dict)
        else {}
    )

    if not isinstance(data, dict):
        print("Unexpected prebook response.")
        return

    prebook_id = data.get("prebookId") or prebook_record.get("prebook_id")
    price = data.get("price")
    currency = data.get("currency")
    rooms_expected = request.get("rooms", 0)
    adults_expected = request.get("adults", 0)
    children_expected = request.get("children", 0)
    budget = request.get("hotel_budget_per_night")
    trip_currency = request.get("currency", "INR")

    if not prebook_id:
        print("Prebook ID is missing.")
        return

    if (
        not isinstance(price, (int, float))
        or isinstance(price, bool)
        or currency != trip_currency
    ):
        print("Final price or currency is invalid.")
        return

    if (
        not isinstance(rooms_expected, int)
        or rooms_expected < 1
        or not isinstance(adults_expected, int)
        or adults_expected < rooms_expected
        or children_expected != 0
    ):
        print(
            "This sandbox test requires at least one adult per room "
            "and no children. No booking request was sent."
        )
        return

    if not isinstance(budget, (int, float)):
        print("Saved hotel budget is invalid.")
        return

    try:
        nights = (
            datetime.fromisoformat(request["return_date"])
            - datetime.fromisoformat(request["departure_date"])
        ).days
    except (KeyError, TypeError, ValueError):
        print("Saved trip dates are invalid.")
        return

    total_budget = budget * nights

    if nights <= 0 or price > total_budget:
        print("Stay duration or budget validation failed.")
        return

    # Verify that the prebook has one rate per requested room.
    room_types = data.get("roomTypes", [])
    if not isinstance(room_types, list):
        print("Invalid prebook room data.")
        return

    rates = [
        rate
        for group in room_types
        if isinstance(group, dict)
        for rate in group.get("rates", [])
        if isinstance(rate, dict)
    ]

    if len(rates) != rooms_expected:
        print("Prebook room count does not match the saved trip.")
        return

    if not all(
        isinstance(rate.get("cancellationPolicies"), dict)
        and rate["cancellationPolicies"].get("cancelPolicyInfos")
        for rate in rates
    ):
        print("Cancellation policies are incomplete. Do not book.")
        return

    # Stable reference: helps protect against duplicate submissions
    # for the same prebook, including after a timeout.
    reference_hash = hashlib.sha256(
        str(prebook_id).encode("utf-8")
    ).hexdigest()[:24]

    client_reference = f"TA-SBX-{reference_hash}"
    attempt_file = BOOKING_DIR / f"{client_reference}.json"

    # Do not automatically submit the same prebook again.
    if attempt_file.exists():
        print("A local record already exists for this prebook attempt.")
        print("No new request was sent.")
        print("Inspect the saved record and provider status before retrying.")
        print("Record:", attempt_file)
        return

    # Demo guests only. Never use these details for a real reservation.
    room_adults = [1] * rooms_expected
    for extra in range(adults_expected - rooms_expected):
        room_adults[extra % rooms_expected] += 1

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
        "clientReference": client_reference,
        "holder": {
            "firstName": "DemoHolder",
            "lastName": "Example",
            "email": "holder@example.com",
        },
        "guests": guests,
        "payment": {
            "method": "ACC_CREDIT_CARD",
        },
    }

    print("\n=== Sandbox Booking Review ===")
    print("Environment: SANDBOX")
    print("Hotel:", prebook_record.get("hotel_name"))
    print("Rooms:", rooms_expected)
    print("Demo guests:", len(guests))
    print("Stay:", request["departure_date"], "to", request["return_date"])
    print(f"Prebook price: {currency} {price:.2f}")
    print(f"Combined budget: {trip_currency} {total_budget:.2f}")
    print("Payment method: ACC_CREDIT_CARD (sandbox only)")
    print("No card number or CVV will be sent by this script.")

    confirmation = input(
        "\nThis sends a booking request to the SANDBOX endpoint. "
        "Type BOOK_SANDBOX to continue: "
    ).strip()

    if confirmation != "BOOK_SANDBOX":
        print("Cancelled. No booking request was sent.")
        return

    # Save a pending record BEFORE the network request. If the call times
    # out, the script will not automatically submit it a second time.
    attempt_record = {
        "trip_id": TRIP_ID,
        "hotel_name": prebook_record.get("hotel_name"),
        "prebook_id": prebook_id,
        "client_reference": client_reference,
        "status": "SUBMISSION_STARTED",
        "submitted_at": datetime.now().astimezone().isoformat(),
        "payment_method": "ACC_CREDIT_CARD",
    }

    try:
        save_json(attempt_file, attempt_record)
    except OSError as exc:
        print(f"Could not save the attempt guard: {exc}")
        print("No booking request was sent.")
        return

    try:
        response = requests.post(
            BOOK_URL,
            headers={
                "X-API-Key": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            params={"timeout": 120},
            json=payload,
            timeout=125,
        )
    except requests.RequestException as exc:
        attempt_record["status"] = "UNKNOWN"
        attempt_record["error"] = str(exc)
        save_json(attempt_file, attempt_record)
        print(
            "Booking result is UNKNOWN because the request failed or "
            "timed out. Do not submit again automatically."
        )
        print("Check the provider's booking status first.")
        return

    try:
        result = response.json()
    except ValueError:
        result = {}

    attempt_record["http_status"] = response.status_code

    if not response.ok or not isinstance(result, dict):
        attempt_record["status"] = "NEEDS_RECONCILIATION"
        attempt_record["response"] = result
        save_json(attempt_file, attempt_record)
        print(f"Booking not confirmed (HTTP {response.status_code}).")
        print("Do not retry blindly. Check the saved record and provider status.")
        print("Record:", attempt_file)
        return

    result_data = result.get("data", {})
    if not isinstance(result_data, dict):
        result_data = {}

    booking_id = (
        result.get("bookingId")
        or result_data.get("bookingId")
    )
    confirmation_code = (
        result.get("hotelConfirmationCode")
        or result_data.get("hotelConfirmationCode")
    )

    attempt_record["response"] = result
    attempt_record["booking_id"] = booking_id
    attempt_record["hotel_confirmation_code"] = confirmation_code

    if not booking_id:
        attempt_record["status"] = "NEEDS_RECONCILIATION"
        save_json(attempt_file, attempt_record)
        print(
            "The API returned a successful HTTP response, but no booking ID "
            "was found. Do not treat this as a confirmed booking."
        )
        print("Record:", attempt_file)
        return

    attempt_record["status"] = "CONFIRMED_SANDBOX"
    save_json(attempt_file, attempt_record)

    print("\nSandbox booking confirmed.")
    print("Booking ID:", booking_id)
    print("Hotel confirmation code:", confirmation_code or "Not provided")
    print("Result saved to:", attempt_file)
    print("This is a sandbox test, not a real hotel reservation.")


if __name__ == "__main__":
    main()
