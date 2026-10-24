
import json
from pathlib import Path
from uuid import uuid4

from src.travel_agent.schemas.travel_request import TravelRequest


# Project root /data/trips
PROJECT_ROOT = Path(__file__).resolve().parents[3]
TRIPS_DIR = PROJECT_ROOT / "data" / "trips"


def _trip_path(trip_id: str) -> Path:
    """Return the safe file path for a trip ID."""
    if not trip_id or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for char in trip_id
    ):
        raise ValueError("Invalid trip ID.")

    return TRIPS_DIR / f"{trip_id}.json"


def create_trip(request: TravelRequest) -> str:
    """Create a new trip record and persist it."""
    TRIPS_DIR.mkdir(parents=True, exist_ok=True)

    trip_id = str(uuid4())
    payload = {
        "trip_id": trip_id,
        "travel_request": request.model_dump(mode="json"),
    }

    path = _trip_path(trip_id)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return trip_id


def load_trip(trip_id: str) -> TravelRequest:
    """Load a saved trip and validate its data."""
    path = _trip_path(trip_id)

    if not path.exists():
        raise FileNotFoundError(f"No saved trip found for ID: {trip_id}")

    payload = json.loads(path.read_text(encoding="utf-8"))

    if payload.get("trip_id") != trip_id:
        raise ValueError("Trip ID does not match the saved record.")

    return TravelRequest.model_validate(payload["travel_request"])


def update_trip(trip_id: str, request: TravelRequest) -> None:
    """Replace the saved state for one specific trip."""
    path = _trip_path(trip_id)

    if not path.exists():
        raise FileNotFoundError(f"No saved trip found for ID: {trip_id}")

    payload = {
        "trip_id": trip_id,
        "travel_request": request.model_dump(mode="json"),
    }

    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
