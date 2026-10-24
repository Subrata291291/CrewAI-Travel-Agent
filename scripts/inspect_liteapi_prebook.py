
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRIP_ID = input("Enter your saved Trip ID: ").strip()

if not TRIP_ID:
    print("Trip ID cannot be empty.")
    raise SystemExit(1)
PREBOOK_DIR = PROJECT_ROOT / "data" / "hotel_prebooks"


def main() -> None:
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
        print(f"Could not read prebook file: {exc}")
        return

    data = record.get("prebook_response", {}).get("data", {})
    if not isinstance(data, dict):
        print("Unexpected prebook response format.")
        return

    print("\n=== Prebook Summary ===")
    print("Hotel:", record.get("hotel_name"))
    print("Check-in:", data.get("checkin"))
    print("Check-out:", data.get("checkout"))
    print("Currency:", data.get("currency"))
    print("Final price:", data.get("price"))
    print("Price difference (%):", data.get("priceDifferencePercent"))
    print("Cancellation changed:", data.get("cancellationChanged"))
    print("Meal plan changed:", data.get("boardChanged"))

    suggested = data.get("suggestedSellingPrice")
    print(
        "Suggested selling price:",
        json.dumps(suggested, ensure_ascii=False)
        if suggested is not None else "Not provided",
    )

    terms = data.get("termsAndConditions")
    print("\n=== Terms and Conditions ===")
    print(terms if terms else "Not provided in this response.")

    room_types = data.get("roomTypes", [])
    if not isinstance(room_types, list):
        print("Unexpected roomTypes format.")
        return

    rate_count = 0

    for group_index, group in enumerate(room_types, start=1):
        if not isinstance(group, dict):
            continue

        rates = group.get("rates", [])
        if not isinstance(rates, list):
            continue

        print(f"\n=== Room group {group_index} ===")

        for rate in rates:
            if not isinstance(rate, dict):
                continue

            rate_count += 1
            print(f"\nRate {rate_count}")
            print("Occupancy number:", rate.get("occupancyNumber"))
            print("Room name:", rate.get("name"))
            print("Adults:", rate.get("adultCount"))
            print("Children:", rate.get("childCount"))
            print("Children ages:", rate.get("childrenAges"))
            print("Board:", rate.get("boardName"))
            print("Payment types:", rate.get("paymentTypes"))

            print("Retail rate:")
            print(json.dumps(
                rate.get("retailRate"),
                indent=2,
                ensure_ascii=False,
            ))

            print("Cancellation policy:")
            print(json.dumps(
                rate.get("cancellationPolicies"),
                indent=2,
                ensure_ascii=False,
            ))

    print(f"\nTotal nested rates returned: {rate_count}")
    print("Expected room count: 2")

    if rate_count != 2:
        print(
            "WARNING: The number of returned rates does not match "
            "the requested room count. Do not proceed to booking."
        )
    else:
        print("Rate count matches the requested room count.")

    print("\nNo booking or payment was submitted.")


if __name__ == "__main__":
    main()
