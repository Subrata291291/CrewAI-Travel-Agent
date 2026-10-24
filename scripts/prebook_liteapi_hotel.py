
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

SEARCH_FILE = (
    PROJECT_ROOT
    / "data"
    / "hotel_search_results"
    / f"{TRIP_ID}.json"
)

PREBOOK_URL = "https://book.liteapi.travel/v3.0/rates/prebook"


def main() -> None:
    api_key = (os.getenv("NUITEE_API_KEY") or "").strip()
    if not api_key:
        print("NUITEE_API_KEY is missing.")
        return

    if not SEARCH_FILE.exists():
        print(f"Saved search file not found: {SEARCH_FILE}")
        print("Run scripts/search_liteapi_hotels.py first.")
        return

    try:
        search_data = json.loads(
            SEARCH_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read search results: {exc}")
        return

    offers = search_data.get("offers", [])
    affordable = [
        offer for offer in offers
        if offer.get("within_budget") is True
        and isinstance(offer.get("offer_id"), str)
        and offer["offer_id"].strip()
    ]
    affordable.sort(
        key=lambda offer: offer.get("total_stay_price", float("inf"))
    )

    if not affordable:
        print("No saved offers within budget have a usable offer ID.")
        return

    print("\n=== Select a hotel to prebook ===")
    print("Prebook verifies the selected offer; it does not finalize a booking.")

    for index, offer in enumerate(affordable, start=1):
        print(
            f"{index}. {offer.get('name', 'Unknown hotel')} | "
            f"{offer.get('currency', '')} "
            f"{offer.get('total_stay_price', 'Unknown price')} | "
            f"{offer.get('address', 'Address not provided')}"
        )

    choice = input("\nChoose an offer number: ").strip()
    try:
        selected_index = int(choice) - 1
        if not 0 <= selected_index < len(affordable):
            raise ValueError
    except ValueError:
        print("Invalid selection. No request was sent.")
        return

    selected = affordable[selected_index]

    print("\nSelected hotel:", selected.get("name"))
    print("Reported stay total:", selected.get("total_stay_price"),
          selected.get("currency"))

    confirmation = input(
        "\nThis sends a live prebook request. Availability and price "
        "will be checked, and the rate may be temporarily held. "
        "No final booking or payment will be submitted by this script.\n"
        "Type PREBOOK to continue: "
    ).strip()

    if confirmation != "PREBOOK":
        print("Cancelled. No prebook request was sent.")
        return

    payload = {
        "offerId": selected["offer_id"],
        "usePaymentSdk": False,
    }

    try:
        response = requests.post(
            PREBOOK_URL,
            headers={
                "X-API-Key": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            params={"timeout": 120},
            json=payload,
            timeout=125,
        )
    except requests.Timeout:
        print(
            "Prebook timed out. Its final status is unknown; "
            "check the provider response before retrying."
        )
        return
    except requests.RequestException as exc:
        print(f"Could not connect to the prebook endpoint: {exc}")
        return

    try:
        result = response.json()
    except ValueError:
        print(
            f"Prebook returned non-JSON data "
            f"(HTTP {response.status_code})."
        )
        return

    error = result.get("error") if isinstance(result, dict) else None

    if response.status_code >= 400 or error:
        print("\nPrebook was not confirmed.")
        print("HTTP status:", response.status_code)

        if isinstance(error, dict):
            print("Error code:", error.get("code", "Not provided"))
            print("Message:", error.get("message", "Not provided"))
            print("Description:", error.get("description", "Not provided"))
        else:
            print("Provider returned an unsuccessful response.")
        return

    if not isinstance(result, dict):
        print("Unexpected prebook response format.")
        return

    data = result.get("data")
    if not isinstance(data, dict):
        data = {}

    prebook_id = result.get("prebookId") or data.get("prebookId")

    if not prebook_id:
        print(
            "The provider returned a response, but no prebookId was found. "
            "Do not proceed to booking."
        )
        print("Response keys:", list(result.keys()))
        return

    output_dir = PROJECT_ROOT / "data" / "hotel_prebooks"
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().astimezone()
    output_file = output_dir / (
        f"{TRIP_ID}_{timestamp.strftime('%Y%m%d_%H%M%S')}.json"
    )

    saved_record = {
        "trip_id": TRIP_ID,
        "hotel_name": selected.get("name"),
        "hotel_id": selected.get("hotel_id"),
        "offer_id": selected["offer_id"],
        "searched_total": selected.get("total_stay_price"),
        "searched_currency": selected.get("currency"),
        "prebook_id": prebook_id,
        "prebooked_at": timestamp.isoformat(),
        "prebook_response": result,
    }

    try:
        output_file.write_text(
            json.dumps(saved_record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"Prebook response received, but could not save it: {exc}")
        print("Prebook ID was returned; do not submit a booking yet.")
        return

    print("\nPrebook succeeded.")
    print("Hotel:", selected.get("name"))
    print("Prebook ID saved locally.")
    print("Saved response:", output_file)
    print(
        "Before any booking, inspect the returned final price, "
        "currency, taxes and cancellation terms."
    )


if __name__ == "__main__":
    main()
