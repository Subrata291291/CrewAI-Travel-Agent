
import json
import os
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRIP_ID = input("Enter your saved Trip ID: ").strip()

if not TRIP_ID:
    print("Trip ID cannot be empty.")
    raise SystemExit(1)

BOOKING_DIR = PROJECT_ROOT / "data" / "hotel_bookings"

load_dotenv(PROJECT_ROOT / ".env")


def main() -> None:
    api_key = (os.getenv("NUITEE_API_KEY") or "").strip()

    if not api_key.startswith(("sand_", "sandbox_")):
        print("STOP: This verification script requires a sandbox key.")
        return

    records = []

    for path in BOOKING_DIR.glob("TA-SBX-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        if (
            record.get("trip_id") == TRIP_ID
            and record.get("booking_id")
        ):
            records.append((path, record))

    if not records:
        print("No saved sandbox booking record found for this trip.")
        return

    records.sort(key=lambda item: item[0].stat().st_mtime, reverse=True)
    record_path, record = records[0]
    booking_id = record["booking_id"]

    print("\n=== LiteAPI Booking Verification ===")
    print("Hotel:", record.get("hotel_name", "Unknown"))
    print("Booking ID:", booking_id)
    print("Environment: SANDBOX")
    print("Checking current status with LiteAPI...")

    url = (
        "https://book.liteapi.travel/v3.0/bookings/"
        f"{booking_id}"
    )

    try:
        response = requests.get(
            url,
            headers={
                "X-API-Key": api_key,
                "Accept": "application/json",
            },
            params={"timeout": 30},
            timeout=35,
        )
    except requests.RequestException:
        print(
            "Could not verify status because the request failed. "
            "The existing booking record was not changed."
        )
        print("Do not submit another booking request.")
        return

    if response.status_code == 204:
        print("The API returned 204 No Content.")
        print("No current booking details were returned.")
        return

    try:
        result = response.json()
    except ValueError:
        print(f"API returned non-JSON data (HTTP {response.status_code}).")
        return

    if not response.ok:
        error = result.get("error", {}) if isinstance(result, dict) else {}
        if isinstance(error, dict):
            print("HTTP status:", response.status_code)
            print("Error:", error.get("message", "Request failed"))
        else:
            print("Booking verification failed:", response.status_code)
        print("No new booking was submitted.")
        return

    data = result.get("data", {}) if isinstance(result, dict) else {}
    if not isinstance(data, dict) or not data:
        print("No booking details were returned.")
        return

    remote_status = data.get("status", "Unknown")
    remote_price = data.get("sellingPrice")
    remote_currency = data.get("currency")
    if not remote_currency:
        remote_currency = record.get("searched_currency", "INR")

    verification = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "http_status": response.status_code,
        "booking_status": remote_status,
        "sandbox": data.get("sandbox"),
        "checkin": data.get("checkin"),
        "checkout": data.get("checkout"),
        "hotel_name": data.get("hotelName"),
        "selling_price": remote_price,
        "currency": remote_currency,
    }

    # Keep only the small verification summary, not a second copy
    # of guest contact information.
    record["remote_verification"] = verification

    try:
        record_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"Could not save verification summary: {exc}")
        return

    print("\n=== Provider Status ===")
    print("Booking status:", remote_status)
    print("Sandbox flag:", data.get("sandbox", "Not provided"))
    print("Hotel:", data.get("hotelName", record.get("hotel_name")))
    print("Check-in:", data.get("checkin", "Not provided"))
    print("Check-out:", data.get("checkout", "Not provided"))
    print(
        "Selling price:",
        remote_price if remote_price is not None else "Not provided",
        remote_currency,
    )
    print("Verification summary saved.")
    print("Local record:", record_path)

    if str(remote_status).upper() == "CONFIRMED":
        print("\nPASS: LiteAPI reports this sandbox booking as CONFIRMED.")
    else:
        print(
            "\nThe provider has not reported CONFIRMED status. "
            "Review the result before taking further action."
        )

    print("\nThis verifies a sandbox booking, not a real reservation.")


if __name__ == "__main__":
    main()
