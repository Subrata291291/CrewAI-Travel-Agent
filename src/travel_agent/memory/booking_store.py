
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BOOKING_DIR = PROJECT_ROOT / "data" / "hotel_bookings"


def list_bookings_for_trip(trip_id: str) -> list[dict[str, Any]]:
    """Return saved booking summaries associated with one trip ID.

    This function reads local JSON files only. It does not contact LiteAPI.
    """

    if not isinstance(trip_id, str) or not trip_id.strip():
        raise ValueError("Trip ID cannot be empty.")

    if not BOOKING_DIR.exists():
        return []

    bookings: list[dict[str, Any]] = []

    for path in BOOKING_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Could not read booking record: {path.name}"
            ) from exc

        if not isinstance(record, dict):
            continue

        if record.get("trip_id") != trip_id:
            continue

        verification = record.get("remote_verification", {})
        if not isinstance(verification, dict):
            verification = {}

        status = record.get("status", "UNKNOWN")
        sandbox_flag = verification.get("sandbox")

        bookings.append({
            "trip_id": record.get("trip_id"),
            "hotel_name": record.get("hotel_name", "Unknown hotel"),
            "booking_id": record.get("booking_id"),
            "prebook_id": record.get("prebook_id"),
            "provider_booking_id": record.get("provider_booking_id"),
            "client_reference": record.get("client_reference"),
            "confirmation_code": record.get(
                "hotel_confirmation_code"
            ),
            "status": status,
            "is_sandbox": (
                status == "CONFIRMED_SANDBOX"
                or sandbox_flag in (1, True, "1")
            ),
            "checkin": verification.get("checkin"),
            "checkout": verification.get("checkout"),
            "selling_price": verification.get("selling_price"),
            "currency": verification.get("currency"),
            "provider_status": verification.get(
                "booking_status", "Not verified"
            ),
            "created_at": record.get("submitted_at"),
            "verified_at": verification.get("checked_at"),
        })

    bookings.sort(
        key=lambda booking: booking.get("created_at") or "",
        reverse=True,
    )

    return bookings
