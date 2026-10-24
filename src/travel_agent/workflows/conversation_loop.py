
from pydantic import ValidationError

from src.travel_agent.agents.clarification_extractor import (
    extract_clarification_answer,
)
from src.travel_agent.agents.travel_request_extractor import (
    extract_travel_request,
)
from src.travel_agent.memory.trip_state import merge_trip_state
from src.travel_agent.schemas.travel_request import TravelRequest
from src.travel_agent.workflows.clarification import (
    get_next_field,
    get_next_question,
)


def collect_travel_request() -> TravelRequest | None:
    """Collect trip details and preserve state across clarifications."""

    message = input("Describe your travel plan: ").strip()

    if not message:
        print("Please enter a travel request.")
        return None

    try:
        current = extract_travel_request(message)
    except Exception as exc:
        print(f"Initial extraction failed: {type(exc).__name__}: {exc}")
        return None

    max_questions = 8

    for _ in range(max_questions):
        field = get_next_field(current)

        if field is None:
            break

        question = get_next_question(current)
        print(f"\nTravel Agent: {question}")
        answer = input("You: ").strip()

        if not answer:
            print("Please provide an answer.")
            continue

        try:
            updates = extract_clarification_answer(
                question=question or "",
                answer=answer,
                expected_field=field,
            )

            current = merge_trip_state(current, updates)

        except (ValueError, ValidationError) as exc:
            print(f"Could not use that answer: {exc}")
            print("Please try again using YYYY-MM-DD for dates.")
            continue
        except Exception as exc:
            print(f"Could not process the answer: {type(exc).__name__}")
            continue

    print("\n--- Collected Travel Request ---")
    print(current.model_dump_json(indent=2))

    if current.status != "ready_to_search":
        print(
            "\nSome required information is still missing. "
            "No flight or hotel search has been started."
        )

    return current
