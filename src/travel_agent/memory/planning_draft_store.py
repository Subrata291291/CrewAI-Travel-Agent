from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.travel_agent.schemas.travel_request import TravelRequest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DRAFTS_DIR = PROJECT_ROOT / "data" / "planning_drafts"
_PENDING_FIELD_UNSET = object()
_PENDING_ACTION_UNSET = object()
_CLARIFICATION_STATE_UNSET = object()


def _draft_path(draft_id: str) -> Path:
    if not draft_id or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for char in draft_id
    ):
        raise ValueError("Invalid planning draft ID.")
    return DRAFTS_DIR / f"{draft_id}.json"


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_draft(
    conversation_id: str,
    request: TravelRequest,
    *,
    owner_id: str | None = None,
    missing: list[str] | None = None,
    pending_field: str | None = None,
    pending_action: str | None = None,
    clarification_state: dict[str, Any] | None = None,
    itinerary: str | None = None,
) -> dict[str, Any]:
    draft_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    draft: dict[str, Any] = {
        "draft_id": draft_id,
        "conversation_id": conversation_id,
        "owner_id": owner_id,
        "travel_request": request.model_dump(mode="json"),
        "missing_information": missing or [],
        "pending_field": pending_field or (missing[0] if missing else None),
        "pending_action": pending_action,
        "clarification_state": clarification_state,
        "itinerary": itinerary,
        "created_at": now,
        "last_updated_at": now,
        "saved_trip_id": None,
    }
    _write(_draft_path(draft_id), draft)
    return draft


def draft_belongs_to(draft: dict[str, Any], owner_id: str) -> bool:
    return draft.get("owner_id") == owner_id


def get_draft(draft_id: str) -> dict[str, Any]:
    path = _draft_path(draft_id)
    if not path.exists():
        raise FileNotFoundError("Planning draft not found.")
    try:
        draft = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Planning draft could not be read.") from exc
    if not isinstance(draft, dict) or draft.get("draft_id") != draft_id:
        raise ValueError("Planning draft data is invalid.")
    return draft


def update_draft(
    draft_id: str,
    request: TravelRequest,
    *,
    missing: list[str] | None = None,
    pending_field: str | None | object = _PENDING_FIELD_UNSET,
    pending_action: str | None | object = _PENDING_ACTION_UNSET,
    clarification_state: dict[str, Any] | None | object = _CLARIFICATION_STATE_UNSET,
    itinerary: str | None = None,
) -> dict[str, Any]:
    draft = get_draft(draft_id)
    if draft.get("saved_trip_id"):
        raise ValueError("This draft has already been saved as a trip.")
    draft["travel_request"] = request.model_dump(mode="json")
    if missing is not None:
        draft["missing_information"] = missing
        if pending_field is not _PENDING_FIELD_UNSET:
            draft["pending_field"] = pending_field
        else:
            current_pending = draft.get("pending_field")
            if current_pending in missing:
                draft["pending_field"] = current_pending
            else:
                draft["pending_field"] = missing[0] if missing else None
    elif pending_field is not _PENDING_FIELD_UNSET:
        draft["pending_field"] = pending_field
    if pending_action is not _PENDING_ACTION_UNSET:
        draft["pending_action"] = pending_action
    if clarification_state is not _CLARIFICATION_STATE_UNSET:
        draft["clarification_state"] = clarification_state
    if itinerary is not None:
        draft["itinerary"] = itinerary
    draft["last_updated_at"] = datetime.now(timezone.utc).isoformat()
    _write(_draft_path(draft_id), draft)
    return draft


def mark_draft_saved(draft_id: str, trip_id: str) -> dict[str, Any]:
    draft = get_draft(draft_id)
    if draft.get("saved_trip_id"):
        return draft
    draft["saved_trip_id"] = trip_id
    draft["last_updated_at"] = datetime.now(timezone.utc).isoformat()
    _write(_draft_path(draft_id), draft)
    return draft
