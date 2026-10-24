
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.travel_agent.memory.booking_store import (
    list_bookings_for_trip,
)

TRIP_ID = input("Enter your saved Trip ID: ").strip()

if not TRIP_ID:
    print("Trip ID cannot be empty.")
    raise SystemExit(1)

def main() -> None:
    try:
        bookings = list_bookings_for_trip(TRIP_ID)
    except ValueError as exc:
        print(f"Could not load saved bookings: {exc}")
        return

    print("\n=== Saved Bookings for Trip ===")
    print("Trip ID:", TRIP_ID)
    print("Booking records found:", len(bookings))

    if not bookings:
        print("No booking records found for this trip.")
        return

    for index, booking in enumerate(bookings, start=1):
        print(f"\nBooking {index}")
        print("Hotel:", booking["hotel_name"])
        print("Booking ID:", booking["booking_id"])
        print("Status:", booking["status"])
        print("Sandbox:", booking["is_sandbox"])
        print("Provider status:", booking["provider_status"])
        print("Check-in:", booking["checkin"])
        print("Check-out:", booking["checkout"])
        print(
            "Price:",
            booking["selling_price"],
            booking["currency"],
        )
        print("Created at:", booking["created_at"])
        print("Verified at:", booking["verified_at"])

    print("\nLocal booking lookup completed. No API request was sent.")


if __name__ == "__main__":
    main()
