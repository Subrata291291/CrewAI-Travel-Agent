
from crewai import Crew, Process, Task

from src.travel_agent.agents.travel_researcher import (
    create_travel_researcher,
)
from src.travel_agent.schemas.travel_request import TravelRequest


def create_travel_planning_crew(
    validated_request: TravelRequest,
    original_message: str,
) -> Crew:
    researcher = create_travel_researcher()

    research_task = Task(
        description=(
            "Prepare a travel-planning brief using the user's original "
            "message and the validated travel data below.\n\n"
            "Original user message:\n"
            "{travel_request}\n\n"
            "Validated travel data (JSON):\n"
            "{validated_request}\n\n"
            "Instructions:\n"
            "1. Treat the validated JSON as the source of truth for "
            "extracted trip fields.\n"
            "2. Identify known details and missing information.\n"
            "3. Clearly distinguish missing information from preferences.\n"
            "3a. Report hotel, flight, and ground transport preferences separately.\n"
            "4. If dates are missing, ask the traveler for their dates "
            "or explain that they can provide them later.\n"
            "5. Do not invent prices, availability, flight details, "
            "hotel details, or booking confirmations.\n"
            "6. Do not claim any external flight or hotel search was run.\n"
            "7. Respect the status field in the validated data.\n"
            "8. Interpret hotel_budget_per_night as the TOTAL combined budget "
            "for all requested rooms per night, never as a per-room budget. "
            "Do not multiply this nightly total by the room count. "
            "Calculate total stay budget as the nightly total multiplied by "
            "the number of nights. Treat total_trip_budget as the full-trip "
            "budget and never substitute it for the hotel nightly budget.\n"
            "9. Respect preferred_transport_mode as a preference. For rail trips, "
            "do not imply a direct train reaches the destination; explain where "
            "additional road transfers may be needed without inventing schedules.\n"
            "10. Treat accommodation_type and interests as preferences, not bookings "
            "or guaranteed itinerary inclusions. Prioritize suitable interests only "
            "after considering travel time, seasonality, accessibility, and budget.\n"
        ),
        expected_output=(
            "A clear travel-planning brief containing:\n"
            "1. Validated trip details\n"
            "2. Missing information\n"
            "3. Hotel requirements and budget, explicitly stating that the "
                "nightly budget is combined across all rooms, separate from "
                "the total trip budget\n"
            "4. Flight requirements\n"
            "5. Questions for the traveler\n"
            "6. Whether the request is ready for a future search\n"
            "7. Any assumptions, explicitly labeled"
        ),
        agent=researcher,
    )

    return Crew(
        agents=[researcher],
        tasks=[research_task],
        process=Process.sequential,
        verbose=True,
    )
