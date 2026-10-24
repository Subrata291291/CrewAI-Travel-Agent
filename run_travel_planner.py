
from datetime import date
from pydantic import ValidationError
from src.travel_agent.memory.trip_store import (
    create_trip,
    load_trip,
    update_trip,
)
import subprocess
import sys
from pathlib import Path
from src.travel_agent.workflows.hotel_search import search_hotels_for_trip
from src.travel_agent.integrations.liteapi_client import LiteAPIError
from src.travel_agent.memory.booking_store import (
    list_bookings_for_trip,
)
from src.travel_agent.memory.trip_state import merge_trip_state
from src.travel_agent.workflows.conversation_loop import collect_travel_request
from src.travel_agent.workflows.travel_planning import create_travel_planning_crew


def run_planning(request, trip_id: str) -> None:
    print("\n--- Persistent Trip Memory ---")
    print(f"Trip ID: {trip_id}")
    print(f"Trip: {request.origin} -> {request.destination}")
    print(f"Dates: {request.departure_date} to {request.return_date}")
    print(f"Nights: {request.nights}")
    print(f"Adults: {request.adults}")
    print(f"Rooms: {request.rooms}")
    print(
        f"Total hotel budget per night (all rooms): "
        f"{request.hotel_budget_per_night} {request.currency}"
    )

    if request.status != "ready_to_search":
        print("\nThis trip is incomplete. No planning run was started.")
        return

    crew = create_travel_planning_crew(
        validated_request=request,
        original_message="Trip resumed or collected through the interactive conversation.",
    )

    try:
        result = crew.kickoff(
            inputs={
                "travel_request": (
                    "Trip resumed or collected through the interactive conversation."
                ),
                "validated_request": request.model_dump_json(indent=2),
            }
        )
    except (ValueError, ValidationError) as exc:
        print(f"\nPlanning validation error: {exc}")
        return
    except Exception as exc:
        print(f"\nPlanning failed: {exc}")
        return

    print("\n--- Travel Planning Result ---")
    print(result)
    print("\nNo live flight/hotel search or booking has been performed.")

def view_saved_bookings() -> None:
    """Display local booking records for a saved trip."""

    trip_id = input("Enter your saved Trip ID: ").strip()

    try:
        bookings = list_bookings_for_trip(trip_id)
    except ValueError as exc:
        print(f"\nCould not read saved bookings: {exc}")
        return

    print("\n=== Saved Bookings ===")
    print(f"Trip ID: {trip_id}")
    print(f"Booking records found: {len(bookings)}")

    if not bookings:
        print("No saved bookings found for this trip.")
        return

    for index, booking in enumerate(bookings, start=1):
        print(f"\nBooking {index}")
        print(f"Hotel: {booking['hotel_name']}")
        print(f"Booking ID: {booking['booking_id']}")
        print(f"Status: {booking['status']}")
        print(f"Sandbox: {booking['is_sandbox']}")
        print(f"Provider status: {booking['provider_status']}")
        print(f"Check-in: {booking['checkin']}")
        print(f"Check-out: {booking['checkout']}")
        print(
            f"Price: {booking['selling_price']} "
            f"{booking['currency']}"
        )

    print("\nThese results were loaded from local JSON files.")
    print("No API request was sent.")

def view_hotel_search_results():
    import json
    from pathlib import Path

    print("\n=== Saved Hotel Search Results ===")
    trip_id = input("Enter your saved Trip ID: ").strip()

    if not trip_id:
        print("Trip ID cannot be empty.")
        return

    results_path = (
        Path(__file__).resolve().parent
        / "data"
        / "hotel_search_results"
        / f"{trip_id}.json"
    )

    if not results_path.exists():
        print("No saved hotel search results found for this trip.")
        print("Please search hotels for this trip first.")
        return

    try:
        with results_path.open("r", encoding="utf-8") as file:
            saved_data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read hotel search results: {exc}")
        return

    offers = saved_data.get("offers", [])

    if not offers:
        print("No hotel offers found in the saved results.")
        return

    print(f"\nOffers found: {len(offers)}")

    for index, offer in enumerate(offers, start=1):
        name = offer.get("name", "Unknown hotel")
        address = offer.get("address", "Address unavailable")
        price = offer.get("total_stay_price")
        currency = offer.get("currency", "INR")
        within_budget = offer.get("within_budget")

        print(f"\n{index}. {name}")
        print(f"   Address: {address}")

        if price is not None:
            print(f"   Total stay price: {currency} {float(price):,.2f}")
        else:
            print("   Total stay price: Unavailable")

        if within_budget is True:
            print("   Within budget: Yes")
        elif within_budget is False:
            print("   Within budget: No")
        else:
            print("   Within budget: Not specified")

    print("\nThese results were loaded from a local JSON file.")
    print("No API request was sent.")

def search_saved_trip_hotels():
    print("\n=== Search Hotels for Saved Trip ===")

    trip_id = input("Enter your saved Trip ID: ").strip()

    if not trip_id:
        print("Trip ID cannot be empty.")
        return

    country_code = (
        input("Destination country code [IN]: ").strip().upper() or "IN"
    )

    guest_nationality = (
        input("Guest nationality code [IN]: ").strip().upper() or "IN"
    )

    try:
        output_path = search_hotels_for_trip(
            trip_id,
            country_code=country_code,
            guest_nationality=guest_nationality,
        )

        print("\nHotel search completed successfully.")
        print(f"Results saved to: {output_path}")

    except (ValueError, LiteAPIError, OSError) as exc:
        print(f"\nHotel search failed: {exc}")

def run_hotel_booking_tools():
    project_root = Path(__file__).resolve().parent

    actions = {
        "1": (
            "Prebook a selected hotel",
            "prebook_liteapi_hotel.py",
        ),
        "2": (
            "Inspect saved prebook details",
            "inspect_liteapi_prebook.py",
        ),
        "3": (
            "Validate saved prebook",
            "validate_liteapi_prebook.py",
        ),
        "4": (
            "Prepare booking payload (dry run)",
            "prepare_liteapi_booking.py",
        ),
        "5": (
            "Submit sandbox booking",
            "book_liteapi_sandbox.py",
        ),
        "6": (
            "Verify booking status",
            "verify_liteapi_booking.py",
        ),
    }

    while True:
        print("\n=== Hotel Booking Tools ===")

        for key, (label, _) in actions.items():
            print(f"{key}. {label}")

        print("0. Back to main menu")

        choice = input("Choose an option: ").strip()

        if choice == "0":
            return

        if choice not in actions:
            print("Invalid choice. Please select 0-6.")
            continue

        label, script_name = actions[choice]
        script_path = project_root / "scripts" / script_name

        if not script_path.is_file():
            print(f"Script not found: {script_path}")
            continue

        print(f"\nStarting: {label}")

        try:
            result = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=str(project_root),
                check=False,
            )

            if result.returncode != 0:
                print(
                    f"Script exited with code {result.returncode}."
                )

        except OSError as exc:
            print(f"Could not start the script: {exc}")

        input("\nPress Enter to return to Hotel Booking Tools...")

def main() -> None:
    print("=== AI Travel Agent ===")
    print("1. Create a new trip")
    print("2. Resume a saved trip")
    print("3. Update a saved trip")
    print("4. View saved bookings")
    print("5. Search hotels for a saved trip")
    print("6. View hotel search results")
    print("7. Hotel Booking Tools")

    choice = input(
        "Choose 1, 2, 3, 4, 5, 6, or 7: "
    ).strip()
    
    if choice == "4":
        view_saved_bookings()
        return
    elif choice == "5":
        search_saved_trip_hotels()
        return
    elif choice == "6":
        view_hotel_search_results()
        return
    elif choice == "7":
        run_hotel_booking_tools()
        return

    if choice == "2":
        trip_id = input("Enter your saved Trip ID: ").strip()

        try:
            request = load_trip(trip_id)
        except FileNotFoundError:
            print("\nNo saved trip found with that ID.")
            return
        except (ValueError, OSError, ValidationError) as exc:
            print(f"\nCould not load trip: {exc}")
            return

        print("\nSaved trip loaded successfully.")
        run_planning(request, trip_id)
        return

    if choice != "1":
        print("\nInvalid choice. Please enter 1, 2, or 3.")
        return

    request = collect_travel_request()

    if request is None:
        print("\nNo travel request was collected.")
        return

    if request.status != "ready_to_search":
        print("\nTrip is incomplete. It has not been saved as a ready trip.")
        return

    try:
        trip_id = create_trip(request)
        saved_request = load_trip(trip_id)
    except (ValueError, OSError, ValidationError) as exc:
        print(f"\nCould not save trip: {exc}")
        return

    print("\nTrip saved and verified.")
    run_planning(saved_request, trip_id)


def update_saved_trip() -> None:
    """Update one field in an existing saved trip."""

    trip_id = input("Enter your saved Trip ID: ").strip()

    try:
        request = load_trip(trip_id)
    except FileNotFoundError:
        print("\nNo saved trip found with that ID.")
        return
    except (ValueError, OSError, ValidationError) as exc:
        print(f"\nCould not load trip: {exc}")
        return

    print(f"\nLoaded trip: {request.origin} -> {request.destination}")
    print("\nWhich field would you like to update?")
    print("1. Adults")
    print("2. Children")
    print("3. Rooms")
    print("4. Total hotel budget per night (all rooms)")
    print("5. Hotel preferences")
    print("6. Transport preferences")
    print("7. Departure and return dates")

    choice = input("Choose 1-7: ").strip()

    if choice == "7":
        try:
            departure_raw = input(
                "New departure date (YYYY-MM-DD): "
            ).strip()
            return_raw = input(
                "New return date (YYYY-MM-DD): "
            ).strip()

            departure = date.fromisoformat(departure_raw)
            return_date = date.fromisoformat(return_raw)

            if return_date <= departure:
                raise ValueError(
                    "Return date must be after departure date."
                )

            nights = (return_date - departure).days

            updated_request = merge_trip_state(
                request,
                {
                    "departure_date": departure.isoformat(),
                    "return_date": return_date.isoformat(),
                    "nights": nights,
                },
            )

        except (ValueError, ValidationError) as exc:
            print(f"\nInvalid date update: {exc}")
            print("The saved trip was not intentionally updated.")
            return

    else:
        fields = {
            "1": "adults",
            "2": "children",
            "3": "rooms",
            "4": "hotel_budget_per_night",
            "5": "hotel_preferences",
            "6": "transport_preferences",
        }

        field = fields.get(choice)

        if field is None:
            print("\nInvalid field selection.")
            return

        raw_value = input(
            "Enter the new value "
            "(for preferences, separate items with commas): "
        ).strip()

        if not raw_value:
            print("\nNo value entered. Trip was not changed.")
            return

        try:
            if field in {"adults", "children", "rooms"}:
                value = int(raw_value)
            elif field == "hotel_budget_per_night":
                value = float(raw_value)
            else:
                value = [
                    item.strip()
                    for item in raw_value.split(",")
                    if item.strip()
                ]

            updated_request = merge_trip_state(
                request,
                {field: value},
            )

        except (ValueError, ValidationError) as exc:
            print(f"\nInvalid update: {exc}")
            print("The saved trip was not intentionally updated.")
            return

    try:
        update_trip(trip_id, updated_request)
        verified_request = load_trip(trip_id)
    except (ValueError, OSError, ValidationError) as exc:
        print(f"\nCould not save or verify the update: {exc}")
        return

    print("\nTrip updated and reloaded successfully.")
    print(f"Trip ID: {trip_id}")
    print(
        f"Route: {verified_request.origin} "
        f"-> {verified_request.destination}"
    )
    print(f"Departure: {verified_request.departure_date}")
    print(f"Return: {verified_request.return_date}")
    print(f"Nights: {verified_request.nights}")
    print(f"Adults: {verified_request.adults}")
    print(f"Children: {verified_request.children}")
    print(f"Rooms: {verified_request.rooms}")
    print(
        f"Total hotel budget per night (all rooms): "
        f"{verified_request.hotel_budget_per_night} "
        f"{verified_request.currency}"
    )
    print(f"Hotel preferences: {verified_request.hotel_preferences}")
    print(
        "Transport preferences: "
        f"{verified_request.transport_preferences}"
    )

if __name__ == "__main__":
    main()
