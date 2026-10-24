
from src.travel_agent.schemas.travel_request import TravelRequest


QUESTIONS = {
    "origin": "Which city will you travel from?",
    "destination": "Which destination would you like to visit?",
    "departure_date": (
        "What is your departure date? Please use YYYY-MM-DD."
    ),
    "return_date": (
        "What is your return-flight date? Please use YYYY-MM-DD."
    ),
    "nights": (
        "How many nights will you stay at the hotel?"
    ),
}


def get_next_field(request: TravelRequest) -> str | None:
    """Choose the next required field deterministically."""

    if not request.origin:
        return "origin"

    if not request.destination:
        return "destination"

    if not request.departure_date:
        return "departure_date"

    if request.flight_required and request.journey_type == "round_trip" and not request.return_date:
        return "return_date"

    if request.hotel_required and not request.nights:
        return "nights"

    return None


def get_next_question(request: TravelRequest) -> str | None:
    """Return the question corresponding to the next missing field."""

    field = get_next_field(request)
    return QUESTIONS.get(field) if field else None
