
import json
import sys
from pathlib import Path
from datetime import date

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TRIP_ID = input("Enter your saved Trip ID: ").strip()

if not TRIP_ID:
    print("Trip ID cannot be empty.")
    raise SystemExit(1)

TRIP_FILE = PROJECT_ROOT / "data" / "trips" / f"{TRIP_ID}.json"
PREBOOK_DIR = PROJECT_ROOT / "data" / "hotel_prebooks"


def main() -> None:
    if not TRIP_FILE.exists():
        print("Saved trip file not found.")
        return

    try:
        trip_data = json.loads(TRIP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read saved trip: {exc}")
        return

    request = trip_data.get("travel_request", {})
    budget_per_night = request.get("hotel_budget_per_night")
    currency_expected = request.get("currency", "INR")
    expected_rooms = request.get("rooms", 0)

    try:
        checkin = date.fromisoformat(request["departure_date"])
        checkout = date.fromisoformat(request["return_date"])
    except (KeyError, TypeError, ValueError):
        print("Saved trip dates are missing or invalid.")
        return

    nights = (checkout - checkin).days
    if nights <= 0 or not isinstance(budget_per_night, (int, float)):
        print("Saved budget or stay duration is invalid.")
        return

    total_budget = budget_per_night * nights

    files = sorted(
        PREBOOK_DIR.glob(f"{TRIP_ID}_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if not files:
        print("No saved prebook response found.")
        return

    try:
        record = json.loads(files[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read prebook response: {exc}")
        return

    response = record.get("prebook_response", {})
    data = response.get("data", {}) if isinstance(response, dict) else {}

    if not isinstance(data, dict):
        print("Unexpected prebook response structure.")
        return

    prebook_id = data.get("prebookId") or record.get("prebook_id")
    final_price = data.get("price")
    currency = data.get("currency")
    price_difference = data.get("priceDifferencePercent")

    room_types = data.get("roomTypes", [])
    rates = []

    if isinstance(room_types, list):
        for room_group in room_types:
            if isinstance(room_group, dict):
                nested_rates = room_group.get("rates", [])
                if isinstance(nested_rates, list):
                    rates.extend(
                        rate for rate in nested_rates
                        if isinstance(rate, dict)
                    )

    checks = {
        "Prebook ID exists": bool(prebook_id),
        "Final price is numeric": isinstance(final_price, (int, float)),
        "Currency matches trip": currency == currency_expected,
        "Price difference is zero": price_difference == 0,
        "Rate count matches requested rooms": len(rates) == expected_rooms,
        "Combined price is within budget": (
            isinstance(final_price, (int, float))
            and currency == currency_expected
            and final_price <= total_budget
        ),
        "Every room has cancellation details": all(
            isinstance(rate.get("cancellationPolicies"), dict)
            and bool(
                rate["cancellationPolicies"].get("cancelPolicyInfos")
            )
            for rate in rates
        ),
    }

    print("\n=== Prebook Validation ===")
    print("Hotel:", record.get("hotel_name"))
    print("Trip ID:", TRIP_ID)
    print(f"Expected rooms: {expected_rooms}")
    print(f"Returned rates: {len(rates)}")
    print(f"Total stay budget: {currency_expected} {total_budget:.2f}")
    print(f"Provider final price: {currency} {final_price}")

    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'}: {name}")

    print("\n=== Cancellation Policy Review ===")

    for index, rate in enumerate(rates, start=1):
        policy = rate.get("cancellationPolicies", {})
        print(f"\nRoom rate {index}:")
        print("Refundable tag:", policy.get("refundableTag", "Unknown"))

        for rule in policy.get("cancelPolicyInfos", []):
            print(
                "Cancellation cutoff:",
                rule.get("cancelTime"),
                rule.get("timezone"),
                "| Penalty:",
                rule.get("amount"),
                rule.get("currency"),
            )

    if all(checks.values()):
        print(
            "\nPREBOOK VALIDATION PASSED: ready for manual review."
        )
        print(
            "This does not authorize a booking. Confirm cancellation "
            "terms and final payable price before proceeding."
        )
    else:
        print("\nPREBOOK VALIDATION FAILED: do not proceed to booking.")

    print("\nNo booking or payment was submitted.")


if __name__ == "__main__":
    main()
