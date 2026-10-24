
from typing import Any

from src.travel_agent.schemas.travel_request import TravelRequest


def merge_trip_state(
    current: TravelRequest,
    explicit_updates: dict[str, Any],
    *,
    allow_null_updates: bool = False,
) -> TravelRequest:
    """
    Merge only explicitly extracted updates into the existing trip.

    The caller must omit fields that were not stated in the latest message.
    A missing field must never be replaced with an LLM-generated default.
    """

    allowed_fields = set(TravelRequest.model_fields)
    unknown_fields = set(explicit_updates) - allowed_fields

    if unknown_fields:
        raise ValueError(
            f"Unknown travel fields: {sorted(unknown_fields)}"
        )

    merged = current.model_dump()

    # A caller that distinguishes omitted fields can opt into explicit nulls.
    for field, value in explicit_updates.items():
        if value is not None or allow_null_updates:
            merged[field] = value

    
    # Application code determines readiness.
    required_missing = (
        not merged.get("origin")
        or not merged.get("destination")
        or not merged.get("departure_date")
    )

    # This project currently assumes a round-trip flight
    # whenever flights are requested.
    if (
        merged.get("flight_required")
        and merged.get("journey_type", "round_trip") == "round_trip"
        and not merged.get("return_date")
    ):
        required_missing = True

    # Hotel dates can be based on a stay duration, but a flight
    # return date must not be inferred from the number of nights.
    if merged.get("hotel_required"):
        has_hotel_dates = (
            merged.get("departure_date") is not None
            and merged.get("nights") is not None
        )

        if not has_hotel_dates:
            required_missing = True

    merged["status"] = (
        "incomplete" if required_missing else "ready_to_search"
    )

    # Validate the complete merged object again.
    return TravelRequest.model_validate(merged)
