
import json
from datetime import date

from src.travel_agent.llm.router import generate
from src.travel_agent.agents.travel_request_extractor import (
    extract_date_updates,
    redact_personal_details,
)


def extract_clarification_answer(
    question: str,
    answer: str,
    expected_field: str,
    *,
    context_date: date | None = None,
) -> dict:
    """Extract only the field requested by the clarification question."""

    allowed_fields = {
        "origin",
        "destination",
        "departure_date",
        "return_date",
        "nights",
        "trip_duration_days",
        "adults",
        "children",
        "rooms",
        "hotel_budget_per_night",
        "total_trip_budget",
        "budget_currency",
        "hotel_preferences",
        "flight_preferences",
        "transport_preferences",
    }

    if expected_field not in allowed_fields:
        raise ValueError(f"Unsupported clarification field: {expected_field}")

    if not answer.strip():
        raise ValueError("Clarification answer cannot be empty.")

    # For ISO dates, deterministic parsing avoids unnecessary LLM ambiguity.
    if expected_field in {"departure_date", "return_date"}:
        parsed, issue = extract_date_updates(
            answer,
            pending_field=expected_field,
            context_date=context_date,
        )
        if issue:
            raise ValueError(issue)
        if expected_field in parsed:
            return {expected_field: parsed[expected_field]}
        other_field = "return_date" if expected_field == "departure_date" else "departure_date"
        if other_field in parsed:
            return {expected_field: parsed[other_field]}

    prompt = f"""
Extract only the requested field from the traveler's answer.

Question asked: {redact_personal_details(question)}
Traveler's answer: {redact_personal_details(answer)}
Expected field: {expected_field}

Return ONLY a valid JSON object containing exactly this one key:
{{
  "{expected_field}": <extracted value>
}}

Rules:
- Do not infer unrelated travel fields.
- Do not invent a date or value.
- For dates, use YYYY-MM-DD.
- If the answer is ambiguous or does not provide the requested information,
  return {{"{expected_field}": null}}.
- Do not return markdown or explanations.
"""

    result = generate(prompt)
    raw = result.text.strip()

    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        )

    data = json.loads(raw)

    if set(data) != {expected_field}:
        raise ValueError(
            f"Expected only the '{expected_field}' field in the response."
        )

    value = data[expected_field]

    if value is None:
        raise ValueError(
            f"Could not extract a valid value for '{expected_field}'."
        )

    return {expected_field: value}
