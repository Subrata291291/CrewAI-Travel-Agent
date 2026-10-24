from __future__ import annotations

import json
import hashlib
import logging
import os
import re
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.travel_agent.integrations.flight_client import LiteAPIFlights
from src.travel_agent.integrations.airport_directory import AirportDirectoryError, get_airport_directory, search_airports
from src.travel_agent.integrations.liteapi_client import LiteAPIClient, LiteAPIError
from src.travel_agent.integrations.weather import (
    WeatherLocationError,
    WeatherProviderError,
    get_current_weather,
    get_weather,
)
from src.travel_agent.agents.travel_request_extractor import (
    extract_date_updates,
    extract_travel_updates_with_date_issue,
    local_today,
    normalize_destination,
    redact_personal_details,
)
from src.travel_agent.memory.booking_store import list_bookings_for_trip
from src.travel_agent.memory import auth_store, planning_draft_store
from src.travel_agent.memory.trip_state import merge_trip_state
from src.travel_agent.memory.trip_store import TRIPS_DIR, create_trip, load_trip, update_trip
from src.travel_agent.schemas.travel_request import TravelRequest
from src.travel_agent.workflows.hotel_search import search_hotels_for_trip

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")
FLIGHT_DIR = ROOT / "data" / "flight_search_results"
FLIGHT_BOOKING_DIR = ROOT / "data" / "flight_bookings"
FLIGHT_BOOKING_ATTEMPT_DIR = ROOT / "data" / "flight_booking_attempts"
HOTEL_SEARCH_DIR = ROOT / "data" / "hotel_search_results"
HOTEL_PREBOOK_DIR = ROOT / "data" / "hotel_prebooks"
HOTEL_BOOKING_ATTEMPT_DIR = ROOT / "data" / "hotel_booking_attempts"
HOTEL_SERVICING_ATTEMPT_DIR = ROOT / "data" / "hotel_servicing_attempts"
HOTEL_SERVICE_PREVIEW_DIR = ROOT / "data" / "hotel_service_previews"
FLIGHT_PREBOOK_DIR = ROOT / "data" / "flight_prebooks"
FLIGHT_VERIFICATION_DIR = ROOT / "data" / "flight_verifications"
FLIGHT_DIAGNOSTIC_FILE = ROOT / "data" / "flight_provider_diagnostics.jsonl"

# Load project environment variables before reading CORS settings.
load_dotenv(ROOT / ".env")

app = FastAPI(title="AI Travel Agent API", version="1.0.0")
logger = logging.getLogger(__name__)
_flight_access_verified = False
_current_user: ContextVar[dict[str, str] | None] = ContextVar("wayfarer_current_user", default=None)

default_origins = (
    "http://localhost:5173,"
    "http://127.0.0.1:5173"
)

origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        default_origins,
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*", "X-Wayfarer-Request"],
    allow_credentials=True,
)


@app.exception_handler(RequestValidationError)
async def sanitized_request_validation_error(
    request: Request,
    exc: RequestValidationError,
):
    errors = [
        {
            "loc": list(error.get("loc", ())),
            "msg": error.get("msg", "Invalid request."),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


_PUBLIC_API_PATHS = {
    "/api/health",
    "/api/capabilities",
    "/api/weather",
    "/api/weather/current",
    "/api/airports/search",
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/me",
    "/api/v1/auth/logout",
}


@app.middleware("http")
async def authenticate_api_requests(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or request.method == "OPTIONS":
        return await call_next(request)

    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD"}:
        if origin and origin not in origins:
            return JSONResponse(status_code=403, content={"detail": "Request origin is not allowed."})
        if request.headers.get("x-wayfarer-request") != "1":
            return JSONResponse(status_code=403, content={"detail": "A valid same-site request header is required."})

    user = auth_store.get_session_user(request.cookies.get("wayfarer_session"))
    if path not in _PUBLIC_API_PATHS and user is None:
        return JSONResponse(status_code=401, content={"detail": "Sign in to continue."})

    token = _current_user.set(user)
    try:
        if user:
            trip_match = re.match(r"^/api/trips/([^/]+)", path)
            if trip_match and not auth_store.owns_trip(user["user_id"], trip_match.group(1)):
                return JSONResponse(status_code=404, content={"detail": "Trip not found."})
            booking_match = re.fullmatch(r"/api/bookings/([^/]+)", path)
            if booking_match:
                trip_id = _booking_trip_id(booking_match.group(1))
                if not trip_id or not auth_store.owns_trip(user["user_id"], trip_id):
                    return JSONResponse(status_code=404, content={"detail": "Booking not found."})
            draft_match = re.match(r"^/api/drafts/([^/]+)", path)
            if draft_match:
                try:
                    draft = planning_draft_store.get_draft(draft_match.group(1))
                except (FileNotFoundError, ValueError):
                    return JSONResponse(status_code=404, content={"detail": "Planning draft not found."})
                if not planning_draft_store.draft_belongs_to(draft, user["user_id"]):
                    return JSONResponse(status_code=404, content={"detail": "Planning draft not found."})
        response = await call_next(request)
        if path.startswith("/api/v1/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response
    finally:
        _current_user.reset(token)


def _booking_trip_id(booking_id: str) -> str | None:
    for directory in (ROOT / "data" / "hotel_bookings", FLIGHT_BOOKING_DIR):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", booking_id):
            return None
        record = _json(directory / f"{booking_id}.json", {})
        if isinstance(record, dict) and isinstance(record.get("trip_id"), str):
            return record["trip_id"]
    return None


class AuthRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=128)


class AuthLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


def _set_session_cookie(response: Response, token: str) -> None:
    secure_default = os.getenv("APP_ENV", "development").casefold() == "production"
    secure = os.getenv("AUTH_COOKIE_SECURE", str(secure_default)).casefold() == "true"
    response.set_cookie(
        key="wayfarer_session",
        value=token,
        max_age=auth_store.SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    secure_default = os.getenv("APP_ENV", "development").casefold() == "production"
    secure = os.getenv("AUTH_COOKIE_SECURE", str(secure_default)).casefold() == "true"
    response.delete_cookie(
        "wayfarer_session",
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )


@app.post("/api/v1/auth/register", status_code=201)
def register_account(body: AuthRegisterRequest, response: Response):
    try:
        user = auth_store.create_user(body.full_name, body.email, body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    token, _ = auth_store.create_session(user["user_id"])
    _set_session_cookie(response, token)
    return {"user": user}


@app.post("/api/v1/auth/login")
def login_account(body: AuthLoginRequest, response: Response):
    user = auth_store.authenticate_user(body.email, body.password)
    if user is None:
        raise HTTPException(401, "Email or password is incorrect.")
    token, _ = auth_store.create_session(user["user_id"])
    _set_session_cookie(response, token)
    return {"user": user}


@app.get("/api/v1/auth/me")
def current_account():
    user = _current_user.get()
    if user is None:
        raise HTTPException(401, "Sign in to continue.")
    return {
        "user": user,
        "trip_count": auth_store.owned_trips_count(user["user_id"]),
    }


@app.post("/api/v1/auth/logout")
def logout_account(request: Request, response: Response):
    auth_store.revoke_session(request.cookies.get("wayfarer_session"))
    _clear_session_cookie(response)
    response.status_code = 204
    return response

class TripPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str | None = None
    destination: str | None = None
    departure_date: date | None = None
    return_date: date | None = None
    journey_type: Literal["one_way", "round_trip"] | None = None
    nights: int | None = Field(default=None, ge=1, le=365)
    trip_duration_days: int | None = Field(default=None, ge=1, le=366)
    adults: int | None = Field(default=None, ge=1, le=20)
    children: int | None = Field(default=None, ge=0, le=20)
    rooms: int | None = Field(default=None, ge=1, le=20)
    hotel_budget_per_night: float | None = Field(default=None, gt=0)
    total_trip_budget: float | None = Field(default=None, gt=0)
    budget_currency: str | None = Field(default=None, min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    currency: str | None = Field(default=None, min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    hotel_required: bool | None = None
    flight_required: bool | None = None
    hotel_preferences: list[str] | None = None
    flight_preferences: list[str] | None = None
    transport_preferences: list[str] | None = None
    preferred_transport_mode: Literal["train", "flight", "bus", "car", "other"] | None = None
    accommodation_type: str | None = Field(default=None, max_length=80)
    interests: list[str] | None = Field(default=None, max_length=30)


class HotelSearch(BaseModel):
    country_code: str = Field(min_length=2, max_length=2)
    guest_nationality: str = Field(default="IN", min_length=2, max_length=2)
    limit: int = Field(default=100, ge=1, le=100)
    place_id: str | None = Field(default=None, min_length=1, max_length=500)
    place_name: str | None = Field(default=None, max_length=200)
    place_address: str | None = Field(default=None, max_length=300)


class FlightSearch(BaseModel):
    origin: str = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    destination: str = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    departure_date: date
    return_date: date | None = None
    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=9)
    infants: int = Field(default=0, ge=0, le=9)
    children_ages: list[int] = Field(default_factory=list)
    infant_ages: list[int] = Field(default_factory=list)
    cabin_class: Literal["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"] = "ECONOMY"
    currency: str = Field(default="INR", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    country: str = Field(default="IN", min_length=2, max_length=2)

    @model_validator(mode="after")
    def check_itinerary(self):
        if self.origin.upper() == self.destination.upper():
            raise ValueError("Origin and destination must differ.")
        if self.return_date and self.return_date <= self.departure_date:
            raise ValueError("Return date must be after departure date.")
        if self.infants > self.adults:
            raise ValueError("Infants cannot exceed adult passengers.")
        if self.children_ages and len(self.children_ages) != self.children:
            raise ValueError("Provide one age per child, or omit child ages.")
        if self.infant_ages and len(self.infant_ages) != self.infants:
            raise ValueError("Provide one age per infant, or omit infant ages.")
        if any(x < 2 or x > 11 for x in self.children_ages):
            raise ValueError("Flight child ages must be 2 through 11.")
        if any(x < 0 or x >= 2 for x in self.infant_ages):
            raise ValueError("Flight infant ages must be under 2.")
        return self


class OfferSelection(BaseModel):
    offer_id: str = Field(min_length=1, max_length=4096)


class HotelPrebookRequest(BaseModel):
    offer_id: str = Field(min_length=1)


class HotelGuest(BaseModel):
    occupancy_number: int = Field(ge=1)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)


class HotelBookingRequest(BaseModel):
    prebook_id: str = Field(min_length=1, max_length=200)
    holder_first_name: str = Field(min_length=1, max_length=100)
    holder_last_name: str = Field(min_length=1, max_length=100)
    holder_email: str = Field(min_length=3, max_length=254)
    holder_phone: str = Field(min_length=1, max_length=40)
    guests: list[HotelGuest] = Field(min_length=1, max_length=40)
    confirm_sandbox_booking: Literal[True]


class HotelCancellationRequest(BaseModel):
    preview_token: str = Field(min_length=1, max_length=80)
    confirm_cancellation: Literal[True]


class HotelHolderAmendmentRequest(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    confirm_amendment: Literal[True]


class HotelRoomSummary(BaseModel):
    name: str | None = None
    occupancy: int | None = None
    adult_count: int | None = None
    child_count: int | None = None
    board_name: str | None = None


class HotelCancellationTerm(BaseModel):
    cancel_time: str | None = None
    amount: float | None = None
    currency: str | None = None
    type: str | None = None
    timezone: str | None = None


class HotelPrebookSummary(BaseModel):
    trip_id: str
    prebook_id: str | None = None
    offer_id: str | None = None
    hotel_name: str | None = None
    prebooked_at: str | None = None
    searched_total: float | None = None
    searched_currency: str | None = None
    final_price: float | None = None
    currency: str | None = None
    price_change: float | None = None
    room_count: int | None = None
    room_details: list[HotelRoomSummary] = Field(default_factory=list)
    cancellation_terms: list[HotelCancellationTerm] = Field(default_factory=list)
    tax_details: list[dict[str, Any]] = Field(default_factory=list)
    prebook_status: str = "PREBOOKED_SANDBOX"
    booking_status: str | None = None
    booking_id: str | None = None
    booking_sandbox: bool | None = None
    booking_client_reference: str | None = None
    booking_submitted: bool = False


class FlightPrebookRequest(BaseModel):
    offer_id: str = Field(min_length=1, max_length=4096)
    verification_id: str = Field(min_length=1, max_length=80)
    contact: dict[str, Any]
    passengers: list[dict[str, Any]] = Field(min_length=1)
    confirm_price: Literal[True]


class FlightBookingRequest(BaseModel):
    prebook_id: str = Field(min_length=1)
    confirm_booking: Literal[True]


class FlightPaymentReturn(BaseModel):
    prebook_id: str = Field(min_length=1, max_length=200)
    transaction_id: str = Field(min_length=1, max_length=200)


def _sandbox_key_mode() -> bool:
    """Only positively identify documented sandbox key prefixes; unknown fails closed."""
    key = os.getenv("NUITEE_API_KEY", "").strip().lower()
    return bool(key and key.startswith(("sand_", "sandbox_")))


def _flight_payment_mode() -> str:
    mode = os.getenv("FLIGHT_PAYMENT_MODE", "nuitee_sdk").strip().lower()
    return "nuitee_sdk" if mode in ("nuitee_sdk", "stripe") else "unsupported"


def _flight_checkout_enabled() -> bool:
    return bool(
        _flight_access_verified
        and _sandbox_key_mode()
        and os.getenv("ALLOW_FLIGHT_PREBOOK", "false").lower() == "true"
        and os.getenv("ALLOW_FLIGHT_BOOKING", "false").lower() == "true"
        and _flight_payment_mode() == "nuitee_sdk"
    )


def _require_sandbox_booking() -> str:
    key = os.getenv("NUITEE_API_KEY", "").strip()
    if not key:
        raise HTTPException(503, {"code": "PROVIDER_NOT_CONFIGURED", "message": "Set NUITEE_API_KEY on the backend."})
    if not _sandbox_key_mode():
        raise HTTPException(403, {"code": "SANDBOX_KEY_REQUIRED", "message": "Sandbox booking is disabled because the configured key cannot be verified as a sandbox key."})
    return key


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=2500)


def _compact_chat_history(history: Any) -> Any:
    if not isinstance(history, list):
        return history

    per_turn_limit = 2400
    total_limit = 24000
    compacted: list[Any] = []
    for turn in history[-20:]:
        if not isinstance(turn, dict) or not isinstance(turn.get("content"), str):
            compacted.append(turn)
            continue
        content = turn["content"]
        if len(content) > per_turn_limit:
            marker = "\n[Earlier conversation content compacted]\n"
            available = per_turn_limit - len(marker)
            head = available * 2 // 3
            content = f"{content[:head]}{marker}{content[-(available - head):]}"
        compacted.append({**turn, "content": content})

    while compacted and sum(
        len(turn.get("content", ""))
        for turn in compacted
        if isinstance(turn, dict) and isinstance(turn.get("content"), str)
    ) > total_limit:
        compacted.pop(0)
    return compacted


class ChatRequest(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def bound_history_context(cls, value: Any) -> Any:
        if isinstance(value, dict) and "history" in value:
            value = dict(value)
            value["history"] = _compact_chat_history(value["history"])
        return value

    message: str = Field(min_length=1, max_length=5000)
    trip_id: str | None = None
    draft_id: str | None = None
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    draft_plan: bool = False
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)


class ChatResponse(BaseModel):
    reply: str
    trip_id: str | None = None
    trip_saved: bool = False
    travel_request: dict[str, Any] | None = None
    missing: list[str] = Field(default_factory=list)
    status_message: str | None = None
    trip_summary: str | None = None
    planning_brief: str | None = None
    draft_id: str | None = None
    pending_field: str | None = None
    conversation_id: str | None = None
    has_planning_draft: bool = False
    weather_result: dict[str, Any] | None = None
    hotel_results: list[dict[str, Any]] | None = None


class PlanningDraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(min_length=1, max_length=100)
    travel_request: TravelRequest


class PlanningDraftPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: TripPatch
    itinerary: str | None = Field(default=None, max_length=20000)


def _trip_or_404(trip_id: str) -> TravelRequest:
    user = _current_user.get()
    if user is None or not auth_store.owns_trip(user["user_id"], trip_id):
        raise HTTPException(404, "Trip not found.")
    try:
        return load_trip(trip_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Trip not found.") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _draft_or_404(draft_id: str, conversation_id: str | None = None) -> dict[str, Any]:
    try:
        draft = planning_draft_store.get_draft(draft_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Planning draft not found.") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    user = _current_user.get()
    if user is None or not planning_draft_store.draft_belongs_to(draft, user["user_id"]):
        raise HTTPException(404, "Planning draft not found.")
    if conversation_id and draft.get("conversation_id") != conversation_id:
        raise HTTPException(404, "Planning draft not found in this conversation.")
    return draft


def _json(path: Path, fallback: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "Saved data could not be read.") from exc


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _hotel_booking_record(trip_id: str, booking_id: str) -> tuple[Path, dict[str, Any]]:
    booking_dir = ROOT / "data" / "hotel_bookings"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", booking_id):
        raise HTTPException(422, "Invalid hotel booking reference.")
    if booking_dir.exists():
        for path in booking_dir.glob("*.json"):
            record = _json(path, {})
            if (
                isinstance(record, dict)
                and record.get("trip_id") == trip_id
                and booking_id in (record.get("booking_id"), record.get("provider_booking_id"))
                and record.get("is_sandbox") is True
            ):
                if record.get("status") not in ("CONFIRMED_SANDBOX", "CONFIRMED"):
                    raise HTTPException(409, "Only a provider-confirmed sandbox hotel booking can be serviced.")
                provider_booking_id = record.get("provider_booking_id")
                if not isinstance(provider_booking_id, str) or not provider_booking_id:
                    raise HTTPException(409, "This local record has no provider booking ID and cannot be serviced.")
                return path, record
    raise HTTPException(404, "Confirmed sandbox hotel booking not found for this trip.")


def _hotel_booking_client() -> LiteAPIClient:
    key = _require_sandbox_booking()
    return LiteAPIClient(api_key=key, base_url="https://book.liteapi.travel/v3.0", timeout=30)


def _hotel_provider_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _prebook_details(record: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize both saved LiteAPI prebook envelope formats into UI details."""
    response = record.get("prebook_response")
    data = response if isinstance(response, dict) else {}
    if isinstance(data.get("data"), dict) and not data.get("roomTypes"):
        data = data["data"]
    groups = data.get("roomTypes") if isinstance(data.get("roomTypes"), list) else []
    rooms: list[dict[str, Any]] = []
    cancellations: list[dict[str, Any]] = []
    taxes: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        rates = group.get("rates") if isinstance(group.get("rates"), list) else []
        for rate in rates:
            if not isinstance(rate, dict):
                continue
            rooms.append({
                "name": rate.get("name") or rate.get("roomName") or group.get("name"),
                "occupancy": rate.get("occupancyNumber") or rate.get("adultCount"),
                "adult_count": rate.get("adultCount"),
                "child_count": rate.get("childCount"),
                "board_name": rate.get("boardName") or rate.get("mealPlan"),
            })
            policy = rate.get("cancellationPolicies")
            retail = rate.get("retailRate") if isinstance(rate.get("retailRate"), dict) else {}
            for tax in retail.get("taxesAndFees", []) if isinstance(retail.get("taxesAndFees"), list) else []:
                if isinstance(tax, dict):
                    taxes.append({k: tax.get(k) for k in ("description", "amount", "currency", "included") if tax.get(k) is not None})
            infos = policy.get("cancelPolicyInfos", []) if isinstance(policy, dict) else []
            for info in infos if isinstance(infos, list) else []:
                if isinstance(info, dict):
                    cancellations.append({"cancel_time": info.get("cancelTime"), **{k: info.get(k) for k in ("amount", "currency", "type", "timezone") if info.get(k) is not None}})
    top_policy = data.get("cancelPolicyInfos")
    if not cancellations and isinstance(top_policy, list):
        cancellations = [{"cancel_time": info.get("cancelTime"), **{k: info.get(k) for k in ("amount", "currency", "type", "timezone") if info.get(k) is not None}} for info in top_policy if isinstance(info, dict)]
    return data, rooms, cancellations, taxes


def _flight_client() -> LiteAPIFlights:
    try:
        return LiteAPIFlights()
    except ValueError as exc:
        raise HTTPException(503, {"code": "FLIGHT_PROVIDER_NOT_CONFIGURED", "message": "Set NUITEE_API_KEY and request Flights API access for this Nuitee Connect account."}) from exc


def _provider_error(
    exc: Exception,
    *,
    access_check: bool = False,
    provider: Literal["flight", "hotel", "weather"] = "flight",
) -> HTTPException:
    message = str(exc)
    status_code = getattr(exc, "status_code", None)
    status_match = re.search(r"HTTP\s+(\d{3})", message)
    provider_status = status_code or (int(status_match.group(1)) if status_match else None)
    provider_code = getattr(exc, "provider_code", None)
    access_denied = provider_status in (401, 403) or (access_check and provider_status == 404)
    provider_name = provider.upper()
    provider_label = "Flights" if provider == "flight" else provider.title()
    code = f"{provider_name}_ACCESS_DISABLED" if access_denied else f"{provider_name}_PROVIDER_ERROR"
    safe_provider_code = provider_code if isinstance(provider_code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", provider_code) else None
    diagnostic = {"at": datetime.now(timezone.utc).isoformat(), "stage": "provider_request", "provider": provider, "provider_status": provider_status, "provider_code": safe_provider_code, "exception_type": type(exc).__name__}
    diagnostic_file = (
        FLIGHT_DIAGNOSTIC_FILE
        if provider == "flight"
        else ROOT / "data" / f"{provider}_provider_diagnostics.jsonl"
    )
    try:
        diagnostic_file.parent.mkdir(parents=True, exist_ok=True)
        with diagnostic_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(diagnostic, separators=(",", ":")) + "\n")
    except OSError:
        logger.warning("Could not persist sanitized %s provider diagnostic", provider)
    logger.warning("%s provider request failed: code=%s status=%s provider_code=%s exception=%s", provider_label, code, provider_status or "unknown", safe_provider_code or "unknown", type(exc).__name__)
    safe_message = (
        f"{provider_label} API access is not enabled for this account; request access from Nuitee Connect."
        if access_denied
        else f"The {provider_label.lower()} provider could not complete this request. Check the request and account access, then try again."
    )
    return HTTPException(503 if access_denied else 502, {"code": code, "provider_status": provider_status, "provider_code": safe_provider_code, "message": safe_message})


def _unwrap_flight_data(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return next((item for item in data if isinstance(item, dict)), {})
    return response


def _flight_price(data: dict[str, Any]) -> tuple[float | None, str | None]:
    pricing = data.get("pricing") if isinstance(data.get("pricing"), dict) else {}
    display = pricing.get("display") if isinstance(pricing.get("display"), dict) else {}
    amount = display.get("total") or data.get("price")
    currency = display.get("currency") or data.get("currency")
    return (float(amount) if isinstance(amount, (int, float)) else None, str(currency) if currency else None)


def _flight_verification_path(trip_id: str, verification_id: str) -> Path:
    if not re.fullmatch(r"[A-Fa-f0-9-]{36}", verification_id):
        raise HTTPException(422, "Invalid verification reference.")
    return FLIGHT_VERIFICATION_DIR / f"{trip_id}_{verification_id}.json"


def _validate_flight_checkout(trip: TravelRequest, saved: dict[str, Any], body: FlightPrebookRequest) -> dict[str, Any]:
    if body.confirm_price is not True:
        raise HTTPException(422, "Review and explicitly confirm the current provider fare before prebooking.")
    verification_path = _flight_verification_path(trip.trip_id if hasattr(trip, "trip_id") else saved.get("trip_id", ""), body.verification_id)
    verification = _json(verification_path)
    if not isinstance(verification, dict) or verification.get("offer_id") != body.offer_id:
        raise HTTPException(409, {"code": "VERIFY_REQUIRED", "message": "Verify this selected offer again before checkout."})
    verified_at = datetime.fromisoformat(verification["verified_at"])
    if (datetime.now(timezone.utc) - verified_at.astimezone(timezone.utc)).total_seconds() > 300:
        raise HTTPException(409, {"code": "VERIFY_EXPIRED", "message": "The verified fare expired. Verify this offer again before checkout."})
    criteria = saved.get("criteria") if isinstance(saved.get("criteria"), dict) else {}
    total_passengers = sum(int(criteria.get(key, 0)) for key in ("adults", "children", "infants"))
    if len(body.passengers) != total_passengers:
        raise HTTPException(422, "Passenger count must match the saved flight search.")
    expected_counts = {0: int(criteria.get("adults", 0)), 1: int(criteria.get("children", 0)), 2: int(criteria.get("infants", 0))}
    actual_counts = {kind: sum(1 for passenger in body.passengers if passenger.get("passengerType") == kind) for kind in (0, 1, 2)}
    if actual_counts != expected_counts:
        raise HTTPException(422, "Passenger types must match the adult, child, and infant counts in the flight search.")
    contact = body.contact
    required_contact = ("firstName", "lastName", "email", "phoneCountryCode", "phoneNumber")
    if any(not isinstance(contact.get(field), str) or not contact[field].strip() for field in required_contact):
        raise HTTPException(422, "Contact first and last name, email, country calling code, and phone number are required.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", contact["email"]):
        raise HTTPException(422, "Enter a valid contact email address.")
    departure_date = date.fromisoformat(criteria["legs"][0]["date"])
    for number, passenger in enumerate(body.passengers, start=1):
        required = ("firstName", "lastName", "birthday", "gender", "nationality", "documentType", "documentNumber", "documentIssueCountry", "documentExpiry")
        if any(not isinstance(passenger.get(field), str) or not passenger[field].strip() for field in required):
            raise HTTPException(422, f"Passenger {number} requires name, birth date, gender, nationality, and passport details.")
        if not re.fullmatch(r"[A-Za-z]{2}", passenger["nationality"]) or not re.fullmatch(r"[A-Za-z]{2}", passenger["documentIssueCountry"]):
            raise HTTPException(422, f"Passenger {number} nationality and document issuing country must be ISO alpha-2 codes.")
        try:
            birthday = date.fromisoformat(passenger["birthday"])
            expiry = date.fromisoformat(passenger["documentExpiry"])
        except ValueError as exc:
            raise HTTPException(422, f"Passenger {number} birth date and passport expiry must be valid dates.") from exc
        if birthday >= departure_date or expiry <= departure_date:
            raise HTTPException(422, f"Passenger {number} birth date must precede departure and passport must be valid through departure.")
        age = departure_date.year - birthday.year - ((departure_date.month, departure_date.day) < (birthday.month, birthday.day))
        passenger_type = passenger.get("passengerType")
        if (passenger_type == 0 and age < 12) or (passenger_type == 1 and not 2 <= age <= 11) or (passenger_type == 2 and age >= 2):
            raise HTTPException(422, f"Passenger {number} age does not match the selected passenger type.")
        passenger["gender"] = passenger["gender"].upper()
        if passenger["gender"] not in ("M", "F"):
            raise HTTPException(422, f"Passenger {number} gender must be M or F.")
    return verification


@app.get("/api/health")
def health():
    return {"status": "ok", "environment": os.getenv("APP_ENV", "development"), "flight_provider_configured": bool(os.getenv("NUITEE_API_KEY"))}


@app.get("/api/capabilities")
def capabilities():
    """Local configuration only; deliberately makes no provider requests."""
    payment_mode = _flight_payment_mode()
    return {
        "sandbox_key_configured": _sandbox_key_mode(),
        "hotel_api_configured": bool(os.getenv("NUITEE_API_KEY", "").strip()),
        "flight_search_access": "enabled" if _flight_access_verified else "not_checked",
        "flight_access_verified": _flight_access_verified,
        "flight_checkout_enabled": _flight_checkout_enabled(),
        "flight_checkout_flags_enabled": os.getenv("ALLOW_FLIGHT_PREBOOK", "false").lower() == "true" and os.getenv("ALLOW_FLIGHT_BOOKING", "false").lower() == "true",
        "flight_payment_mode": payment_mode,
        "flight_payment_sdk": "liteapi",
        "weather_api_configured": bool(os.getenv("NUITEE_API_KEY", "").strip()),
        "production_booking_enabled": False,
    }


@app.get("/api/airports/search")
def airport_search(q: str = Query(min_length=2, max_length=80), limit: int = Query(default=10, ge=1, le=20)):
    if len(q.strip()) < 2:
        raise HTTPException(422, "Enter at least two characters to search airports.")
    try:
        return {"data": search_airports(q.strip(), limit=limit)}
    except AirportDirectoryError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        logger.warning("Airport directory lookup failed: %s", type(exc).__name__)
        raise HTTPException(503, "Airport reference data is temporarily unavailable. Please try again later.") from exc


@app.get("/api/hotels/places/search")
def hotel_place_search(q: str = Query(min_length=2, max_length=100)):
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(422, "Enter at least two characters to search hotel locations.")
    try:
        places = LiteAPIClient().search_places(query, place_type="locality")
        return {
            "data": [
                {
                    "place_id": place["placeId"],
                    "display_name": place["displayName"],
                    "formatted_address": place["formattedAddress"],
                    "types": place.get("types", []),
                }
                for place in places
                if all(
                    isinstance(place.get(key), str) and place[key].strip()
                    for key in ("placeId", "displayName", "formattedAddress")
                )
            ]
        }
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc
    except LiteAPIError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/weather")
def weather_forecast(
    location: str = Query(min_length=2, max_length=120),
    start_date: date | None = None,
    end_date: date | None = None,
    units: Literal["metric", "imperial"] = "metric",
):
    first_day = start_date or date.today()
    last_day = end_date or first_day
    try:
        return get_weather(location, first_day, last_day, units=units)
    except WeatherLocationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LiteAPIError as exc:
        raise _provider_error(exc, provider="weather") from exc
    except Exception as exc:
        logger.warning("Weather lookup failed: exception=%s", type(exc).__name__)
        raise HTTPException(503, "Weather data is temporarily unavailable.") from exc


@app.get("/api/weather/current")
def current_weather(
    location: str = Query(min_length=2, max_length=120),
):
    try:
        return get_current_weather(location)
    except WeatherLocationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except WeatherProviderError as exc:
        logger.warning("Current weather provider failed: exception=%s", type(exc).__name__)
        raise HTTPException(503, "Live current weather is temporarily unavailable.") from exc
    except Exception as exc:
        logger.warning("Current weather lookup failed: exception=%s", type(exc).__name__)
        raise HTTPException(503, "Live current weather is temporarily unavailable.") from exc


@app.post("/api/flights/capabilities/check")
def flight_capability_check(body: FlightSearch):
    """Explicit user-triggered read-only-rate access check; does not persist offers."""
    global _flight_access_verified
    _flight_access_verified = False
    _require_sandbox_booking()
    legs = [{"origin": body.origin.upper(), "destination": body.destination.upper(), "date": body.departure_date.isoformat(), "direction": "OUTBOUND"}]
    if body.return_date:
        legs.append({"origin": body.destination.upper(), "destination": body.origin.upper(), "date": body.return_date.isoformat(), "direction": "INBOUND"})
    criteria: dict[str, Any] = {"legs": legs, "adults": body.adults, "children": body.children, "infants": body.infants, "cabinClass": body.cabin_class, "currency": body.currency.upper(), "country": body.country.upper()}
    if body.children_ages: criteria["childrenAges"] = body.children_ages
    if body.infant_ages: criteria["infantAges"] = body.infant_ages
    try:
        response = _flight_client().search(**criteria)
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, list) or any(
            not isinstance(group, dict)
            or ("journeys" in group and not isinstance(group["journeys"], list))
            for group in data
        ):
            raise LiteAPIError("LiteAPI Flights returned an unexpected search response format.")
        journey_count = sum(len(group.get("journeys", [])) for group in data if isinstance(group, dict) and isinstance(group.get("journeys", []), list)) if isinstance(data, list) else 0
        _flight_access_verified = True
        return {"flight_search_access": "enabled", "journeys_returned": journey_count, "booking_access": "not_checked", "message": "Flight rate access responded. Booking and payment permissions still require their explicit checkout steps."}
    except Exception as exc:
        _flight_access_verified = False
        raise _provider_error(exc, access_check=True) from exc


@app.post("/api/trips", status_code=201)
def create_trip_api(request: TravelRequest):
    try:
        trip_id = create_trip(request)
        user = _current_user.get()
        if user is None:
            raise HTTPException(401, "Sign in to continue.")
        auth_store.own_trip(user["user_id"], trip_id)
        return {"trip_id": trip_id, "travel_request": request.model_dump(mode="json")}
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/trips")
def list_trips():
    items = []
    user = _current_user.get()
    if user is None:
        raise HTTPException(401, "Sign in to continue.")
    owned_ids = auth_store.list_owned_trip_ids(user["user_id"])
    if TRIPS_DIR.exists():
        for path in sorted(TRIPS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            payload = _json(path, {})
            if payload.get("trip_id") not in owned_ids:
                continue
            try:
                req = TravelRequest.model_validate(payload.get("travel_request", {}))
                items.append({"trip_id": payload.get("trip_id"), "travel_request": req.model_dump(mode="json")})
            except ValidationError:
                continue
    return items


@app.get("/api/trips/{trip_id}")
def get_trip(trip_id: str):
    return {"trip_id": trip_id, "travel_request": _trip_or_404(trip_id).model_dump(mode="json")}


@app.post("/api/drafts", status_code=201)
def create_planning_draft(body: PlanningDraftCreate):
    try:
        user = _current_user.get()
        if user is None:
            raise HTTPException(401, "Sign in to continue.")
        return planning_draft_store.create_draft(
            body.conversation_id,
            body.travel_request,
            owner_id=user["user_id"],
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(400, "The planning draft could not be created.") from exc


@app.get("/api/drafts/{draft_id}")
def get_planning_draft(draft_id: str, conversation_id: str | None = None):
    draft = _draft_or_404(draft_id, conversation_id)
    return draft


@app.patch("/api/drafts/{draft_id}")
def patch_planning_draft(draft_id: str, body: PlanningDraftPatch):
    draft = _draft_or_404(draft_id)
    try:
        current = TravelRequest.model_validate(draft.get("travel_request", {}))
        updated = merge_trip_state(
            current,
            body.changes.model_dump(exclude_unset=True),
            allow_null_updates=True,
        )
        return planning_draft_store.update_draft(
            draft_id,
            updated,
            itinerary=body.itinerary,
        )
    except ValidationError as exc:
        raise HTTPException(422, exc.errors()) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, "The planning draft could not be saved.") from exc


@app.post("/api/drafts/{draft_id}/save", status_code=201)
def save_planning_draft(draft_id: str):
    draft = _draft_or_404(draft_id)
    saved_trip_id = draft.get("saved_trip_id")
    if isinstance(saved_trip_id, str):
        return {
            "draft_id": draft_id,
            "trip_id": saved_trip_id,
            "travel_request": _trip_or_404(saved_trip_id).model_dump(mode="json"),
            "trip_saved": True,
        }
    request = TravelRequest.model_validate(draft.get("travel_request", {}))
    from src.travel_agent.workflows.clarification import get_next_field

    missing = get_next_field(request)
    if missing:
        raise HTTPException(422, {
            "code": "PLANNING_DRAFT_INCOMPLETE",
            "missing": missing,
            "message": "Add the missing trip details before saving this draft.",
        })
    try:
        ready_request = merge_trip_state(request, {})
        trip_id = create_trip(ready_request)
        user = _current_user.get()
        if user is None:
            raise HTTPException(401, "Sign in to continue.")
        auth_store.own_trip(user["user_id"], trip_id)
        planning_draft_store.mark_draft_saved(draft_id, trip_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(500, "The planning draft could not be saved as a trip.") from exc
    return {
        "draft_id": draft_id,
        "trip_id": trip_id,
        "travel_request": ready_request.model_dump(mode="json"),
        "trip_saved": True,
    }


@app.patch("/api/trips/{trip_id}")
def patch_trip(trip_id: str, changes: TripPatch):
    current = _trip_or_404(trip_id)
    try:
        updated = merge_trip_state(
            current,
            changes.model_dump(exclude_unset=True),
            allow_null_updates=True,
        )
        update_trip(trip_id, updated)
        return {"trip_id": trip_id, "travel_request": updated.model_dump(mode="json")}
    except ValidationError as exc:
        detail = [
            {
                "loc": list(error["loc"]),
                "msg": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        raise HTTPException(422, detail=detail) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/trips/{trip_id}/hotels/search", status_code=202)
def hotel_search(trip_id: str, body: HotelSearch):
    trip = _trip_or_404(trip_id)
    if trip.hotel_budget_per_night is None:
        raise HTTPException(
            422,
            detail=[
                {
                    "loc": ["hotel_budget_per_night"],
                    "msg": "Set the combined per-night hotel budget for all rooms in Overview, save the trip, and try again.",
                    "type": "missing",
                }
            ],
        )
    try:
        search_hotels_for_trip(
            trip_id,
            country_code=body.country_code,
            guest_nationality=body.guest_nationality,
            limit=body.limit,
            place_id=body.place_id,
            place_name=body.place_name,
            place_address=body.place_address,
        )
        return _json(HOTEL_SEARCH_DIR / f"{trip_id}.json", {})
    except (ValueError, LiteAPIError) as exc:
        raise HTTPException(422 if isinstance(exc, ValueError) else 502, str(exc)) from exc


@app.get("/api/trips/{trip_id}/hotels/results")
def hotel_results(trip_id: str):
    trip = _trip_or_404(trip_id)
    saved = _json(HOTEL_SEARCH_DIR / f"{trip_id}.json", {"trip_id": trip_id, "offers": []})
    if not isinstance(saved, dict):
        return {"trip_id": trip_id, "offers": [], "stale": True}
    adults_per_room = [1] * trip.rooms
    for extra in range(max(0, trip.adults - trip.rooms)):
        adults_per_room[extra % trip.rooms] += 1
    occupancies = [{"adults": adults_in_room, "children": []} for adults_in_room in adults_per_room]
    matches = (saved.get("destination") == trip.destination and saved.get("checkin") == (trip.departure_date.isoformat() if trip.departure_date else None) and saved.get("checkout") == (trip.return_date.isoformat() if trip.return_date else None) and saved.get("rooms") == trip.rooms and saved.get("occupancies") == occupancies and saved.get("currency") == trip.currency and saved.get("budget_per_night_all_rooms") == trip.hotel_budget_per_night)
    return {**saved, "stale": bool(saved.get("searched_at") and not matches)}


@app.get("/api/trips/{trip_id}/hotels/bookings")
def hotel_bookings(trip_id: str):
    _trip_or_404(trip_id)
    return list_bookings_for_trip(trip_id)


@app.get("/api/trips/{trip_id}/hotels/bookings/{booking_id}/service-preview")
def hotel_service_preview(trip_id: str, booking_id: str):
    _trip_or_404(trip_id)
    _, record = _hotel_booking_record(trip_id, booking_id)
    client = _hotel_booking_client()
    try:
        remote = _hotel_provider_data(
            client._request(
                "GET",
                f"/bookings/{record['provider_booking_id']}",
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _provider_error(exc, provider="hotel") from exc
    preview_token = str(uuid4())
    preview = {
        "preview_token": preview_token,
        "trip_id": trip_id,
        "provider_booking_id": record["provider_booking_id"],
        "provider_status": str(remote.get("status") or "UNKNOWN").upper(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "consumed": False,
    }
    HOTEL_SERVICE_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_json_write(HOTEL_SERVICE_PREVIEW_DIR / f"{preview_token}.json", preview)
    remote_verification = record.get("remote_verification")
    if not isinstance(remote_verification, dict):
        remote_verification = {}
    return {
        "preview_token": preview_token,
        "booking_id": record.get("booking_id"),
        "provider_booking_id": record.get("provider_booking_id"),
        "hotel_name": record.get("hotel_name"),
        "trip_id": trip_id,
        "provider_status": preview["provider_status"],
        "checkin": remote.get("checkin") or remote_verification.get("checkin"),
        "checkout": remote.get("checkout") or remote_verification.get("checkout"),
        "price": remote.get("sellingPrice") or remote.get("price") or remote_verification.get("selling_price"),
        "currency": remote.get("currency") or remote_verification.get("currency"),
        "booking_reference": remote.get("bookingRef") or remote.get("hotelConfirmationCode") or record.get("hotel_confirmation_code"),
        "cancellation_policy": remote.get("cancellationPolicy") or remote.get("cancelPolicyInfos") or remote.get("cancellationPolicies"),
    }


@app.post("/api/trips/{trip_id}/hotels/bookings/{booking_id}/cancel")
def cancel_hotel_booking(trip_id: str, booking_id: str, body: HotelCancellationRequest):
    _trip_or_404(trip_id)
    booking_path, record = _hotel_booking_record(trip_id, booking_id)
    if body.confirm_cancellation is not True:
        raise HTTPException(422, "Explicit cancellation confirmation is required.")
    provider_booking_id = record["provider_booking_id"]
    try:
        normalized_preview_token = str(UUID(body.preview_token))
    except ValueError as exc:
        raise HTTPException(422, "Invalid cancellation review token.") from exc
    preview_path = HOTEL_SERVICE_PREVIEW_DIR / f"{normalized_preview_token}.json"
    preview = _json(preview_path)
    if (
        not isinstance(preview, dict)
        or preview.get("trip_id") != trip_id
        or preview.get("provider_booking_id") != provider_booking_id
        or preview.get("consumed") is True
    ):
        raise HTTPException(409, "Review the current provider cancellation details before confirming.")
    try:
        preview_time = datetime.fromisoformat(str(preview.get("created_at", "")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(409, "The cancellation review is invalid. Load it again before confirming.") from exc
    if (datetime.now(timezone.utc) - preview_time.astimezone(timezone.utc)).total_seconds() > 300:
        raise HTTPException(409, "The cancellation review has expired. Refresh the provider terms before confirming.")
    if preview.get("provider_status") != "CONFIRMED":
        raise HTTPException(409, "The provider does not currently report this booking as confirmed.")
    client = _hotel_booking_client()
    operation_id = hashlib.sha256(f"hotel-cancel:{provider_booking_id}".encode()).hexdigest()
    HOTEL_SERVICING_ATTEMPT_DIR.mkdir(parents=True, exist_ok=True)
    attempt_path = HOTEL_SERVICING_ATTEMPT_DIR / f"{operation_id}.json"
    if attempt_path.exists():
        prior = _json(attempt_path, {})
        raise HTTPException(409, {
            "code": "HOTEL_CANCELLATION_ATTEMPT_EXISTS",
            "status": prior.get("status", "REQUIRES_RECONCILIATION"),
            "message": "A cancellation request already exists. Reconcile its provider status; do not resubmit it.",
        })
    attempt = {
        "operation_id": operation_id,
        "operation": "HOTEL_CANCELLATION",
        "trip_id": trip_id,
        "provider_booking_id": provider_booking_id,
        "status": "PENDING",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with attempt_path.open("x", encoding="utf-8") as stream:
            json.dump(attempt, stream, indent=2)
    except FileExistsError:
        raise HTTPException(409, "A cancellation request already exists; reconcile it before taking further action.")
    preview["consumed"] = True
    _atomic_json_write(preview_path, preview)
    try:
        cancellation = _hotel_provider_data(
            client._request("PUT", f"/bookings/{provider_booking_id}")
        )
        verification = _hotel_provider_data(
            client._request("GET", f"/bookings/{provider_booking_id}")
        )
        provider_status = str(
            verification.get("status") or cancellation.get("status") or "UNKNOWN"
        ).upper()
        attempt.update({
            "status": provider_status if provider_status in ("CANCELLED", "CANCELLED_WITH_CHARGES") else "REQUIRES_RECONCILIATION",
            "provider_status": provider_status,
            "cancellation_fee": cancellation.get("cancellation_fee") or cancellation.get("cancellationFee"),
            "refund_amount": cancellation.get("refund_amount") or cancellation.get("refundAmount"),
            "currency": cancellation.get("currency") or verification.get("currency"),
            "verified_at": datetime.now(timezone.utc).isoformat(),
        })
        attempt_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
        if attempt["status"] in ("CANCELLED", "CANCELLED_WITH_CHARGES"):
            record["status"] = attempt["status"]
            record["remote_verification"] = {
                **(record.get("remote_verification") if isinstance(record.get("remote_verification"), dict) else {}),
                "booking_status": attempt["status"],
                "checked_at": attempt["verified_at"],
            }
            _atomic_json_write(booking_path, record)
    except Exception as exc:
        diagnostic = _provider_error(exc, provider="hotel")
        detail = diagnostic.detail if isinstance(diagnostic.detail, dict) else {}
        attempt.update({
            "status": "REQUIRES_RECONCILIATION",
            "error_type": type(exc).__name__,
            "provider_status": detail.get("provider_status"),
            "provider_code": detail.get("provider_code"),
        })
        attempt_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
    return attempt


@app.post("/api/trips/{trip_id}/hotels/bookings/{booking_id}/cancel/verify")
def verify_hotel_cancellation(trip_id: str, booking_id: str):
    _trip_or_404(trip_id)
    booking_path, record = _hotel_booking_record(trip_id, booking_id)
    provider_booking_id = record["provider_booking_id"]
    operation_id = hashlib.sha256(f"hotel-cancel:{provider_booking_id}".encode()).hexdigest()
    attempt_path = HOTEL_SERVICING_ATTEMPT_DIR / f"{operation_id}.json"
    attempt = _json(attempt_path)
    if not isinstance(attempt, dict) or attempt.get("operation") != "HOTEL_CANCELLATION":
        raise HTTPException(404, "No cancellation attempt exists for this hotel booking.")
    try:
        remote = _hotel_provider_data(
            _hotel_booking_client()._request("GET", f"/bookings/{provider_booking_id}")
        )
    except Exception as exc:
        raise _provider_error(exc, provider="hotel") from exc
    provider_status = str(remote.get("status") or "UNKNOWN").upper()
    attempt.update({
        "provider_status": provider_status,
        "status": provider_status if provider_status in ("CANCELLED", "CANCELLED_WITH_CHARGES") else "REQUIRES_RECONCILIATION",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "cancellation_fee": remote.get("cancellation_fee") or remote.get("cancellationFee"),
        "refund_amount": remote.get("refund_amount") or remote.get("refundAmount"),
        "currency": remote.get("currency"),
    })
    attempt_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
    if attempt["status"] in ("CANCELLED", "CANCELLED_WITH_CHARGES"):
        record["status"] = attempt["status"]
        verification = record.get("remote_verification")
        if not isinstance(verification, dict):
            verification = {}
        verification.update({"booking_status": attempt["status"], "checked_at": attempt["verified_at"]})
        record["remote_verification"] = verification
        _atomic_json_write(booking_path, record)
    return attempt


@app.post("/api/trips/{trip_id}/hotels/bookings/{booking_id}/amend-holder")
def amend_hotel_booking_holder(
    trip_id: str,
    booking_id: str,
    body: HotelHolderAmendmentRequest,
):
    _trip_or_404(trip_id)
    _, record = _hotel_booking_record(trip_id, booking_id)
    if body.confirm_amendment is not True:
        raise HTTPException(422, "Explicit booking-holder amendment confirmation is required.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", body.email):
        raise HTTPException(422, "Enter a valid booking-holder email address.")
    provider_booking_id = record["provider_booking_id"]
    client = _hotel_booking_client()
    details_hash = hashlib.sha256(
        f"{body.first_name.strip()}|{body.last_name.strip()}|{body.email.strip().lower()}".encode()
    ).hexdigest()
    operation_id = hashlib.sha256(
        f"hotel-amend:{provider_booking_id}:{details_hash}".encode()
    ).hexdigest()
    HOTEL_SERVICING_ATTEMPT_DIR.mkdir(parents=True, exist_ok=True)
    attempt_path = HOTEL_SERVICING_ATTEMPT_DIR / f"{operation_id}.json"
    if attempt_path.exists():
        prior = _json(attempt_path, {})
        raise HTTPException(409, {
            "code": "HOTEL_AMENDMENT_ATTEMPT_EXISTS",
            "status": prior.get("status", "REQUIRES_RECONCILIATION"),
            "message": "An amendment for these exact holder details already exists. Check its provider status before submitting again.",
        })
    attempt = {
        "operation_id": operation_id,
        "operation": "HOTEL_HOLDER_AMENDMENT",
        "trip_id": trip_id,
        "provider_booking_id": provider_booking_id,
        "status": "PENDING",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with attempt_path.open("x", encoding="utf-8") as stream:
            json.dump(attempt, stream, indent=2)
    except FileExistsError:
        raise HTTPException(409, "This amendment already exists; check its provider status before retrying.")
    try:
        result = _hotel_provider_data(
            client._request(
                "PUT",
                f"/bookings/{provider_booking_id}/amend",
                json={
                    "firstName": body.first_name.strip(),
                    "lastName": body.last_name.strip(),
                    "email": body.email.strip(),
                },
            )
        )
        provider_status = str(result.get("status") or "PENDING").upper()
        attempt.update({
            "status": provider_status,
            "provider_status": provider_status,
            "message": result.get("message") if isinstance(result.get("message"), str) else None,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        diagnostic = _provider_error(exc, provider="hotel")
        detail = diagnostic.detail if isinstance(diagnostic.detail, dict) else {}
        attempt.update({
            "status": "REQUIRES_RECONCILIATION",
            "error_type": type(exc).__name__,
            "provider_status": detail.get("provider_status"),
            "provider_code": detail.get("provider_code"),
        })
    attempt_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
    return attempt


@app.post("/api/trips/{trip_id}/hotels/prebook")
def hotel_prebook(trip_id: str, body: HotelPrebookRequest):
    _trip_or_404(trip_id)
    saved = _json(HOTEL_SEARCH_DIR / f"{trip_id}.json", {})
    selected = next((offer for offer in saved.get("offers", []) if offer.get("offer_id") == body.offer_id), None)
    if selected is None:
        raise HTTPException(404, "Offer is not part of this trip's saved hotel search results.")
    if selected.get("within_budget") is not True:
        raise HTTPException(422, "Only a verified offer within the saved trip budget can be prebooked.")
    key = _require_sandbox_booking()
    try:
        client = LiteAPIClient(api_key=key, base_url="https://book.liteapi.travel/v3.0", timeout=125)
        result = client._request("POST", "/rates/prebook", params={"timeout": 120}, json={"offerId": body.offer_id, "usePaymentSdk": False})
    except (LiteAPIError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from exc
    data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
    prebook_id = result.get("prebookId") or data.get("prebookId")
    if not prebook_id:
        raise HTTPException(502, "Provider response did not contain a prebook ID; no booking was submitted.")
    record = {"trip_id": trip_id, "hotel_name": selected.get("name"), "hotel_id": selected.get("hotel_id"), "offer_id": body.offer_id, "searched_total": selected.get("total_stay_price"), "searched_currency": selected.get("currency"), "prebook_id": prebook_id, "prebooked_at": datetime.now(timezone.utc).isoformat(), "prebook_response": {k: data.get(k) for k in ("prebookId", "price", "currency", "roomTypes", "cancelPolicyInfos") if k in data}}
    HOTEL_PREBOOK_DIR.mkdir(parents=True, exist_ok=True)
    path = HOTEL_PREBOOK_DIR / f"{trip_id}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    final_price = data.get("price")
    searched_price = record["searched_total"]
    price_change = final_price - searched_price if isinstance(final_price, (int, float)) and isinstance(searched_price, (int, float)) else None
    room_groups = data.get("roomTypes", [])
    cancellations = data.get("cancelPolicyInfos") or [rate.get("cancellationPolicies") for group in room_groups if isinstance(group, dict) for rate in group.get("rates", []) if isinstance(rate, dict) and rate.get("cancellationPolicies")]
    return {"trip_id": trip_id, "hotel_name": record["hotel_name"], "prebook_id": prebook_id, "searched_total": searched_price, "final_price": final_price, "price_change": price_change, "currency": data.get("currency"), "room_count": sum(len(g.get("rates", [])) for g in room_groups if isinstance(g, dict)), "cancellation_policy": cancellations, "status": "PREBOOKED_SANDBOX", "booking_submitted": False}


@app.post("/api/trips/{trip_id}/hotels/book")
def hotel_book(trip_id: str, body: HotelBookingRequest):
    """Submit only after explicit UI confirmation; unknown outcomes are never retried."""
    trip = _trip_or_404(trip_id)
    key = _require_sandbox_booking()
    if body.confirm_sandbox_booking is not True:
        raise HTTPException(422, "Explicit sandbox booking confirmation is required.")
    matches = list(HOTEL_PREBOOK_DIR.glob(f"{trip_id}_*.json")) if HOTEL_PREBOOK_DIR.exists() else []
    prebook_path = next((p for p in matches if _json(p, {}).get("prebook_id") == body.prebook_id), None)
    if prebook_path is None:
        raise HTTPException(404, "This prebook does not belong to the selected trip.")
    record = _json(prebook_path, {})
    try:
        created = datetime.fromisoformat(str(record.get("prebooked_at", "")).replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() > 600:
            raise HTTPException(410, "This prebook is no longer fresh enough to book safely. Create a new hotel prebook from current search results.")
    except ValueError:
        raise HTTPException(410, "Prebook time is unavailable. Create a fresh hotel prebook before booking.")
    response_data, rooms, _, _ = _prebook_details(record)
    provider_prebook_id = response_data.get("prebookId") or record.get("prebook_id")
    room_types = response_data.get("roomTypes") if isinstance(response_data.get("roomTypes"), list) else []
    expected_rooms = sum(len(room.get("rates", [])) for room in room_types if isinstance(room, dict))
    expected_rooms = expected_rooms or trip.rooms
    if not provider_prebook_id or len(body.guests) != expected_rooms:
        raise HTTPException(422, "Provide exactly one primary guest for each room in the prebook.")
    occupancy_numbers = [guest.occupancy_number for guest in body.guests]
    if sorted(occupancy_numbers) != list(range(1, expected_rooms + 1)):
        raise HTTPException(422, "Provide one primary guest for each prebook room, using each room number once.")
    if any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", guest.email) for guest in body.guests) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", body.holder_email):
        raise HTTPException(422, "Enter a valid email address for the booking holder and every guest.")

    import hashlib
    reference = "WF-SBX-" + hashlib.sha256(provider_prebook_id.encode("utf-8")).hexdigest()[:24]
    HOTEL_BOOKING_ATTEMPT_DIR.mkdir(parents=True, exist_ok=True)
    attempt_path = HOTEL_BOOKING_ATTEMPT_DIR / f"{reference}.json"
    if attempt_path.exists():
        prior = _json(attempt_path, {})
        raise HTTPException(409, {"code": "BOOKING_ATTEMPT_EXISTS", "status": prior.get("status", "REQUIRES_RECONCILIATION"), "message": "A booking attempt already exists for this prebook. Check its provider status; it will not be submitted again."})
    attempt = {"trip_id": trip_id, "provider": "LiteAPI Hotels", "provider_mode": "SANDBOX", "prebook_id": provider_prebook_id, "client_reference": reference, "status": "BOOKING_PENDING_CONFIRMATION", "created_at": datetime.now(timezone.utc).isoformat(), "payment_method": "ACC_CREDIT_CARD"}
    # Create the duplicate-submission guard atomically before the provider request.
    try:
        with attempt_path.open("x", encoding="utf-8") as stream:
            json.dump(attempt, stream, indent=2)
    except FileExistsError:
        prior = _json(attempt_path, {})
        raise HTTPException(409, {
            "code": "BOOKING_ATTEMPT_EXISTS",
            "status": prior.get("status", "REQUIRES_RECONCILIATION"),
            "message": "A booking attempt already exists for this prebook. Reconcile its provider status; it will not be submitted again.",
        })
    guests = [guest.model_dump(by_alias=False) for guest in body.guests]
    guests = [{"occupancyNumber": guest["occupancy_number"], "firstName": guest["first_name"], "lastName": guest["last_name"], "email": guest["email"]} for guest in guests]
    payload = {
        "prebookId": provider_prebook_id,
        "clientReference": reference,
        "holder": {"firstName": body.holder_first_name, "lastName": body.holder_last_name, "email": body.holder_email, "phone": body.holder_phone},
        "guests": guests,
        "payment": {"method": "ACC_CREDIT_CARD"},
    }
    client = LiteAPIClient(api_key=key, base_url="https://book.liteapi.travel/v3.0", timeout=125)
    provider_booking_id: str | None = None
    try:
        result = client._request("POST", "/rates/book", params={"timeout": 120}, json=payload)
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        provider_booking_id = data.get("bookingId")
        if not provider_booking_id:
            attempt.update({"status": "REQUIRES_RECONCILIATION", "message": "Provider response did not include a booking ID."})
            attempt_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
            return attempt
        verification = client._request("GET", f"/bookings/{provider_booking_id}", params={"timeout": 8})
        verified = verification.get("data") if isinstance(verification.get("data"), dict) else verification
        provider_status = str(verified.get("status") or data.get("status") or "").upper()
        status = "CONFIRMED_SANDBOX" if provider_status == "CONFIRMED" else "REQUIRES_RECONCILIATION"
        local_booking_id = str(uuid4())
        confirmed_at = datetime.now(timezone.utc).isoformat()
        safe_record = {
            "booking_id": local_booking_id, "trip_id": trip_id, "provider": "LiteAPI Hotels",
            "hotel_name": record.get("hotel_name") or "Hotel", "prebook_id": provider_prebook_id,
            "provider_booking_id": provider_booking_id, "client_reference": reference,
            "status": status, "is_sandbox": True, "payment_method": "ACC_CREDIT_CARD",
            "hotel_confirmation_code": verified.get("hotelConfirmationCode") or data.get("hotelConfirmationCode"),
            "submitted_at": confirmed_at,
            "remote_verification": {"booking_status": provider_status or "UNKNOWN", "sandbox": 1, "checked_at": confirmed_at,
                "checkin": verified.get("checkin"), "checkout": verified.get("checkout"),
                "selling_price": verified.get("sellingPrice") or data.get("price"),
                "currency": verified.get("currency") or data.get("currency")},
        }
        booking_dir = ROOT / "data" / "hotel_bookings"
        booking_dir.mkdir(parents=True, exist_ok=True)
        booking_file = booking_dir / f"{local_booking_id}.json"
        booking_file.write_text(json.dumps(safe_record, indent=2, ensure_ascii=False), encoding="utf-8")
        attempt.update({"status": status, "booking_id": local_booking_id, "provider_booking_id": provider_booking_id, "hotel_confirmation_code": safe_record["hotel_confirmation_code"], "provider_status": provider_status, "verified_at": confirmed_at, "currency": safe_record["remote_verification"]["currency"], "price": safe_record["remote_verification"]["selling_price"]})
        attempt_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
        return {k: safe_record.get(k) for k in ("booking_id", "trip_id", "hotel_name", "prebook_id", "provider_booking_id", "client_reference", "status", "is_sandbox", "hotel_confirmation_code", "submitted_at", "remote_verification")}
    except Exception as exc:
        attempt.update({"status": "REQUIRES_RECONCILIATION", "error_type": type(exc).__name__, "provider_booking_id": provider_booking_id})
        if provider_booking_id:
            local_booking_id = str(uuid4())
            safe_record = {"booking_id": local_booking_id, "trip_id": trip_id, "provider": "LiteAPI Hotels", "hotel_name": record.get("hotel_name") or "Hotel", "prebook_id": provider_prebook_id, "provider_booking_id": provider_booking_id, "client_reference": reference, "status": "REQUIRES_RECONCILIATION", "is_sandbox": True, "submitted_at": datetime.now(timezone.utc).isoformat(), "remote_verification": {"booking_status": "NOT_VERIFIED", "sandbox": 1, "checked_at": None}}
            (ROOT / "data" / "hotel_bookings").mkdir(parents=True, exist_ok=True)
            (ROOT / "data" / "hotel_bookings" / f"{local_booking_id}.json").write_text(json.dumps(safe_record, indent=2), encoding="utf-8")
            attempt["booking_id"] = local_booking_id
        attempt_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
        if isinstance(exc, HTTPException):
            raise
        logger.warning("Hotel sandbox booking requires reconciliation: attempt=%s error=%s", reference, type(exc).__name__)
        raise HTTPException(502, {"code": "BOOKING_REQUIRES_RECONCILIATION", "message": f"The booking result could not be verified. Do not retry this prebook; check the Nuitee sandbox dashboard using client reference {reference}."}) from exc


@app.get("/api/trips/{trip_id}/hotels/prebooks", response_model=list[HotelPrebookSummary])
def hotel_prebooks(trip_id: str):
    trip = _trip_or_404(trip_id)
    if not HOTEL_PREBOOK_DIR.exists():
        return []
    summaries = []
    for path in HOTEL_PREBOOK_DIR.glob(f"{trip_id}_*.json"):
        record = _json(path, {})
        data, rooms, cancellations, taxes = _prebook_details(record)
        final_price = data.get("price")
        searched_price = record.get("searched_total")
        price_change = final_price - searched_price if isinstance(final_price, (int, float)) and isinstance(searched_price, (int, float)) else None
        prebook_id = record.get("prebook_id") or data.get("prebookId")
        matching = [b for b in list_bookings_for_trip(trip_id) if prebook_id and b.get("prebook_id") == prebook_id]
        verified = next((b for b in matching if b.get("status") == "CONFIRMED_SANDBOX" and str(b.get("provider_status", "")).upper() == "CONFIRMED" and b.get("is_sandbox") is True), None)
        reference = "WF-SBX-" + hashlib.sha256(str(prebook_id or "").encode("utf-8")).hexdigest()[:24]
        attempt = _json(HOTEL_BOOKING_ATTEMPT_DIR / f"{reference}.json", {})
        has_attempt = isinstance(attempt, dict) and attempt.get("trip_id") == trip_id
        summaries.append({"trip_id": trip_id, "prebook_id": prebook_id, "offer_id": record.get("offer_id"), "hotel_name": record.get("hotel_name"), "prebooked_at": record.get("prebooked_at"), "searched_total": searched_price, "searched_currency": record.get("searched_currency"), "final_price": final_price, "currency": data.get("currency"), "price_change": price_change, "room_count": len(rooms) if rooms else (trip.rooms if not data.get("roomTypes") else 0), "room_details": rooms, "cancellation_terms": cancellations, "tax_details": taxes, "prebook_status": "PREBOOKED_SANDBOX", "booking_status": "CONFIRMED_SANDBOX" if verified else (attempt.get("status") if has_attempt else ("ASSOCIATION_UNVERIFIED" if matching else None)), "booking_id": verified.get("booking_id") if verified else (matching[0].get("booking_id") if matching else attempt.get("booking_id") if has_attempt else None), "booking_sandbox": verified.get("is_sandbox") if verified else (matching[0].get("is_sandbox") if matching else True if has_attempt else None), "booking_client_reference": attempt.get("client_reference") if has_attempt else (matching[0].get("client_reference") if matching else None), "booking_submitted": bool(matching or has_attempt)})
    return sorted(summaries, key=lambda item: item.get("prebooked_at") or "", reverse=True)


@app.post("/api/trips/{trip_id}/hotels/prebooks/{prebook_id}/verify")
def verify_hotel_booking(trip_id: str, prebook_id: str):
    """Reconcile an existing hotel booking attempt without resubmitting it."""
    _trip_or_404(trip_id)
    reference = "WF-SBX-" + hashlib.sha256(prebook_id.encode("utf-8")).hexdigest()[:24]
    attempt_path = HOTEL_BOOKING_ATTEMPT_DIR / f"{reference}.json"
    attempt = _json(attempt_path)
    if not isinstance(attempt, dict) or attempt.get("trip_id") != trip_id or attempt.get("prebook_id") != prebook_id:
        raise HTTPException(404, "No booking attempt for this prebook belongs to the selected trip.")
    provider_booking_id = attempt.get("provider_booking_id")
    if not provider_booking_id:
        raise HTTPException(409, {
            "code": "PROVIDER_BOOKING_ID_UNAVAILABLE",
            "message": f"The provider did not return a booking ID. Do not resubmit this prebook; reconcile it with Nuitee using client reference {attempt.get('client_reference')}.",
        })
    key = _require_sandbox_booking()
    try:
        client = LiteAPIClient(api_key=key, base_url="https://book.liteapi.travel/v3.0", timeout=30)
        response = client._request("GET", f"/bookings/{provider_booking_id}")
    except Exception as exc:
        logger.warning("Hotel booking reconciliation failed: exception=%s", type(exc).__name__)
        raise HTTPException(502, {
            "code": "HOTEL_BOOKING_RECONCILIATION_FAILED",
            "message": "The provider booking status could not be checked. Do not submit this prebook again; retry status reconciliation later.",
        }) from exc
    verified = response.get("data") if isinstance(response.get("data"), dict) else response
    provider_status = str(verified.get("status") or "").upper()
    status = (
        "CONFIRMED_SANDBOX"
        if provider_status == "CONFIRMED"
        else "CANCELLED_SANDBOX"
        if provider_status in {"CANCELLED", "CANCELED"}
        else "REQUIRES_RECONCILIATION"
    )
    local_booking_id = attempt.get("booking_id") or str(uuid4())
    prebook_record = next(
        (
            _json(path, {})
            for path in HOTEL_PREBOOK_DIR.glob(f"{trip_id}_*.json")
            if _json(path, {}).get("prebook_id") == prebook_id
        ),
        {},
    ) if HOTEL_PREBOOK_DIR.exists() else {}
    checked_at = datetime.now(timezone.utc).isoformat()
    safe_record = {
        "booking_id": local_booking_id,
        "trip_id": trip_id,
        "provider": "LiteAPI Hotels",
        "hotel_name": prebook_record.get("hotel_name") or "Hotel",
        "prebook_id": prebook_id,
        "provider_booking_id": provider_booking_id,
        "client_reference": reference,
        "status": status,
        "is_sandbox": True,
        "hotel_confirmation_code": verified.get("hotelConfirmationCode"),
        "submitted_at": attempt.get("created_at"),
        "remote_verification": {
            "booking_status": provider_status or "UNKNOWN",
            "sandbox": 1,
            "checked_at": checked_at,
            "checkin": verified.get("checkin"),
            "checkout": verified.get("checkout"),
            "selling_price": verified.get("sellingPrice") or verified.get("price"),
            "currency": verified.get("currency"),
        },
    }
    booking_dir = ROOT / "data" / "hotel_bookings"
    booking_dir.mkdir(parents=True, exist_ok=True)
    (booking_dir / f"{local_booking_id}.json").write_text(
        json.dumps(safe_record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    attempt.update({
        "booking_id": local_booking_id,
        "provider_status": provider_status or "UNKNOWN",
        "status": status,
        "verified_at": checked_at,
    })
    attempt_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
    return {
        **safe_record,
        "confirmation_reference": safe_record["hotel_confirmation_code"],
    }


def _flight_journey_route_issues(journey: dict[str, Any], requested_legs: list[dict[str, str]]) -> list[str]:
    expected = {
        str(leg.get("direction", "")).upper(): leg
        for leg in requested_legs
        if isinstance(leg, dict) and leg.get("direction")
    }
    segments = journey.get("segments")
    segments = segments if isinstance(segments, list) else []
    issues: list[str] = []
    directions: dict[str, list[dict[str, Any]]] = {"OUTBOUND": [], "INBOUND": []}
    missing_direction = False
    for segment in segments:
        if not isinstance(segment, dict):
            issues.append("A segment was not an object")
            continue
        direction = str(segment.get("direction", "")).upper()
        if direction == "RETURN":
            direction = "INBOUND"
        if direction in directions:
            directions[direction].append(segment)
        elif not direction:
            missing_direction = True
        else:
            issues.append("A segment had an unknown direction")
    if missing_direction and not issues:
        def matches_leg(chain: list[dict[str, Any]], leg: dict[str, Any], direction: str) -> bool:
            if not chain:
                return False
            if any(
                str(segment.get("direction", "")).upper() not in ("", direction, "RETURN" if direction == "INBOUND" else direction)
                for segment in chain
            ):
                return False
            if str(chain[0].get("originCode", "")).upper() != str(leg.get("origin", "")).upper():
                return False
            if str(chain[-1].get("destinationCode", "")).upper() != str(leg.get("destination", "")).upper():
                return False
            for previous, current in zip(chain, chain[1:]):
                if str(previous.get("destinationCode", "")).upper() != str(current.get("originCode", "")).upper():
                    return False
            expected_date = leg.get("date")
            return not expected_date or all(
                not str(segment.get("departureTime", ""))[:10]
                or str(segment.get("departureTime", ""))[:10] == expected_date
                for segment in chain
            )

        outbound_leg = expected.get("OUTBOUND")
        inbound_leg = expected.get("INBOUND")
        inferred: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None
        if len(expected) == 1 and outbound_leg and matches_leg(segments, outbound_leg, "OUTBOUND"):
            inferred = (segments, [])
        elif len(expected) == 2 and outbound_leg and inbound_leg:
            candidates = [
                (segments[:split], segments[split:])
                for split in range(1, len(segments))
                if matches_leg(segments[:split], outbound_leg, "OUTBOUND")
                and matches_leg(segments[split:], inbound_leg, "INBOUND")
            ]
            if len(candidates) == 1:
                inferred = candidates[0]
            elif len(candidates) > 1:
                issues.append("Segments without directions could not be assigned unambiguously")
        if inferred:
            directions["OUTBOUND"], directions["INBOUND"] = inferred
        elif not issues:
            issues.append("Segments without directions could not be matched to the requested legs")
    for direction, expected_leg in expected.items():
        chain = directions.get(direction, [])
        if not chain:
            issues.append(f"No {direction.lower()} segments were returned")
            continue
        origin = str(chain[0].get("originCode", "")).upper()
        destination = str(chain[-1].get("destinationCode", "")).upper()
        expected_origin = str(expected_leg.get("origin", "")).upper()
        expected_destination = str(expected_leg.get("destination", "")).upper()
        if origin != expected_origin:
            issues.append(f"{direction} starts at {origin or 'unknown'} instead of {expected_origin}")
        if destination != expected_destination:
            issues.append(f"{direction} ends at {destination or 'unknown'} instead of {expected_destination}")
        for previous, current in zip(chain, chain[1:]):
            if str(previous.get("destinationCode", "")).upper() != str(current.get("originCode", "")).upper():
                issues.append(f"{direction} segment chain is disconnected")
                break
        expected_date = expected_leg.get("date")
        if expected_date:
            for segment in chain:
                departure = str(segment.get("departureTime", ""))[:10]
                if departure and departure != expected_date:
                    issues.append(f"{direction} segment departs on {departure} instead of {expected_date}")
    if len(expected) == 1 and directions["INBOUND"]:
        issues.append("Unexpected inbound segments for a one-way request")
    return issues


def _airport_matches_trip_city(airport: dict[str, str], trip_city: str) -> bool:
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.casefold())
    expected = normalize(trip_city)
    city = normalize(airport.get("city_name", ""))
    subdivision = normalize(airport.get("subdivision", ""))
    aliases = {"newdelhi": "delhi", "delhi": "delhi"}
    expected = aliases.get(expected, expected)
    city = aliases.get(city, city)
    return bool(expected and (city == expected or subdivision == expected))


def _flight_route_diagnostics(results: list[dict[str, Any]], requested_legs: list[dict[str, str]]) -> dict[str, Any]:
    """Compare provider segment chains with the requested outbound/return legs."""
    checked = matched = 0
    diagnostics: list[dict[str, Any]] = []
    for group in results:
        for journey in group.get("journeys", []):
            if not isinstance(journey, dict):
                continue
            checked += 1
            issues = _flight_journey_route_issues(journey, requested_legs)
            if not issues:
                matched += 1
            elif len(diagnostics) < 12:
                segments = journey.get("segments") if isinstance(journey.get("segments"), list) else []
                diagnostics.append({
                    "journey_key": str(journey.get("journeyKey", ""))[:120],
                    "issues": issues[:8],
                    "segments": [
                        {"direction": str(segment.get("direction", ""))[:20], "origin": str(segment.get("originCode", ""))[:8], "destination": str(segment.get("destinationCode", ""))[:8]}
                        for segment in segments[:12] if isinstance(segment, dict)
                    ],
                })
    return {"checked_journeys": checked, "matched_journeys": matched, "mismatched_journeys": checked - matched, "diagnostics": diagnostics}


def _verified_route_offer_ids(saved: dict[str, Any]) -> set[str]:
    criteria = saved.get("criteria") if isinstance(saved, dict) else None
    requested_legs = criteria.get("legs") if isinstance(criteria, dict) else None
    if not isinstance(requested_legs, list):
        return set()
    valid: set[str] = set()
    for group in saved.get("data", []):
        if not isinstance(group, dict):
            continue
        for journey in group.get("journeys", []):
            if not isinstance(journey, dict) or _flight_journey_route_issues(journey, requested_legs):
                continue
            for offer in journey.get("offers", []):
                if isinstance(offer, dict) and isinstance(offer.get("offerId"), str):
                    valid.add(offer["offerId"])
    return valid


def _saved_flight_selection(saved: dict[str, Any], offer_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    criteria = saved.get("criteria") if isinstance(saved, dict) else None
    requested_legs = criteria.get("legs") if isinstance(criteria, dict) else None
    if not isinstance(requested_legs, list):
        return None
    for group in saved.get("data", []):
        if not isinstance(group, dict):
            continue
        for journey in group.get("journeys", []):
            if not isinstance(journey, dict) or _flight_journey_route_issues(journey, requested_legs):
                continue
            for offer in journey.get("offers", []):
                if isinstance(offer, dict) and offer.get("offerId") == offer_id:
                    return journey, offer
    return None


def _saved_flight_search_matches_trip(
    saved: dict[str, Any],
    trip_id: str,
    trip: TravelRequest,
) -> bool:
    route = saved.get("trip_route") if isinstance(saved, dict) else None
    criteria = saved.get("criteria") if isinstance(saved, dict) else None
    if not isinstance(route, dict) or not isinstance(criteria, dict):
        return False
    expected_return = (
        trip.return_date.isoformat()
        if trip.journey_type == "round_trip" and trip.return_date
        else None
    )
    legs = criteria.get("legs")
    if not isinstance(legs, list):
        return False
    expected_legs = 2 if expected_return else 1
    if len(legs) != expected_legs:
        return False
    outbound = legs[0] if isinstance(legs[0], dict) else {}
    if outbound.get("date") != (trip.departure_date.isoformat() if trip.departure_date else None):
        return False
    if expected_return:
        inbound = legs[1] if isinstance(legs[1], dict) else {}
        if inbound.get("date") != expected_return:
            return False
    expected_route = {
        "origin": trip.origin,
        "destination": trip.destination,
        "departure_date": trip.departure_date.isoformat() if trip.departure_date else None,
        "return_date": expected_return,
        "journey_type": trip.journey_type,
        "adults": trip.adults,
        "children": trip.children,
    }
    return (
        saved.get("trip_id") == trip_id
        and all(route.get(key) == value for key, value in expected_route.items())
        and criteria.get("adults") == trip.adults
        and criteria.get("children") == trip.children
        and criteria.get("infants", 0) == 0
    )


@app.post("/api/trips/{trip_id}/flights/search", status_code=202)
def flight_search(trip_id: str, body: FlightSearch):
    global _flight_access_verified
    _flight_access_verified = False
    trip = _trip_or_404(trip_id)
    if not trip.origin or not trip.destination or not trip.departure_date:
        raise HTTPException(422, "Set the trip origin, destination, and departure date before searching flights.")
    expected_return = (
        trip.return_date
        if trip.journey_type == "round_trip"
        else None
    )
    if body.departure_date != trip.departure_date or body.return_date != expected_return:
        raise HTTPException(422, "Flight dates must match the selected trip. Update and save the trip before searching.")
    if body.adults != trip.adults or body.children != trip.children or body.infants != 0:
        raise HTTPException(422, "Flight passenger counts must match the selected trip. Infants are not recorded on this trip.")
    try:
        # Validate against the complete cached directory, not just suggestions.
        airport_directory = get_airport_directory()
    except AirportDirectoryError as exc:
        raise HTTPException(503, str(exc)) from exc
    airports_by_code = {
        airport["iata_code"]: airport
        for airport in airport_directory
    }
    origin_airport = airports_by_code.get(body.origin.upper())
    destination_airport = airports_by_code.get(body.destination.upper())
    if origin_airport is None or destination_airport is None:
        raise HTTPException(422, "Choose both airports from the airport suggestions before searching.")
    if not _airport_matches_trip_city(origin_airport, trip.origin):
        raise HTTPException(
            422,
            f"The selected origin airport is in {origin_airport.get('city_name') or origin_airport.get('country_name')}, not the saved trip origin {trip.origin}. Choose an airport for the selected trip.",
        )
    if not _airport_matches_trip_city(destination_airport, trip.destination):
        raise HTTPException(
            422,
            f"The selected destination airport is in {destination_airport.get('city_name') or destination_airport.get('country_name')}, not the saved trip destination {trip.destination}. Choose an airport for the selected trip.",
        )
    if body.departure_date <= date.today() or body.return_date and body.return_date <= date.today():
        raise HTTPException(422, "Flight dates must be in the future.")
    today = date.today()
    month_index = today.month - 1 + 11
    year, month = today.year + month_index // 12, month_index % 12 + 1
    latest_day = min(today.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    latest = date(year, month, latest_day)
    if body.departure_date > latest or body.return_date and body.return_date > latest:
        raise HTTPException(422, "Flight dates must be within the next 11 months.")
    legs = [{"origin": body.origin.upper(), "destination": body.destination.upper(), "date": body.departure_date.isoformat(), "direction": "OUTBOUND"}]
    if body.return_date:
        legs.append({"origin": body.destination.upper(), "destination": body.origin.upper(), "date": body.return_date.isoformat(), "direction": "INBOUND"})
    criteria: dict[str, Any] = {"legs": legs, "adults": body.adults, "children": body.children, "infants": body.infants, "cabinClass": body.cabin_class, "currency": body.currency.upper(), "country": body.country.upper()}
    if body.children_ages: criteria["childrenAges"] = body.children_ages
    if body.infant_ages: criteria["infantAges"] = body.infant_ages
    try:
        response = _flight_client().search(**criteria)
        results = response.get("data")
        if not isinstance(results, list) or any(not isinstance(item, dict) or not isinstance(item.get("journeys", []), list) for item in results):
            raise LiteAPIError("LiteAPI Flights returned an unexpected search response format.")
        route_validation = _flight_route_diagnostics(results, legs)
        if route_validation["mismatched_journeys"]:
            logger.warning("LiteAPI returned journeys that did not match requested route: %s", json.dumps(route_validation, separators=(",", ":")))
        FLIGHT_DIR.mkdir(parents=True, exist_ok=True)
        saved = {
            "trip_id": trip_id,
            "trip_route": {
                "origin": trip.origin,
                "destination": trip.destination,
                "departure_date": trip.departure_date.isoformat(),
                "return_date": expected_return.isoformat() if expected_return else None,
                "journey_type": trip.journey_type,
                "adults": trip.adults,
                "children": trip.children,
            },
            "searched_at": datetime.now(timezone.utc).isoformat(),
            "criteria": criteria,
            "route_validation": route_validation,
            "data": results,
        }
        (FLIGHT_DIR / f"{trip_id}.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")
        _flight_access_verified = True
        return saved
    except Exception as exc:
        _flight_access_verified = False
        raise _provider_error(exc, access_check=True) from exc


@app.get("/api/trips/{trip_id}/flights/results")
def flight_results(trip_id: str):
    trip = _trip_or_404(trip_id)
    saved = _json(FLIGHT_DIR / f"{trip_id}.json", {"trip_id": trip_id, "data": []})
    if not isinstance(saved, dict) or not _saved_flight_search_matches_trip(saved, trip_id, trip):
        return {
            "trip_id": trip_id,
            "data": [],
            "stale": True,
            "message": "Saved flight results do not match this trip. Search again after selecting airports for the current route.",
        }
    filtered_groups = []
    criteria = saved.get("criteria", {})
    for group in saved.get("data", []):
        if not isinstance(group, dict):
            continue
        journeys = [
            journey
            for journey in group.get("journeys", [])
            if isinstance(journey, dict)
            and not _flight_journey_route_issues(journey, criteria.get("legs", []))
        ]
        filtered_groups.append({**group, "journeys": journeys})
    return {**saved, "data": filtered_groups, "stale": False}


@app.post("/api/trips/{trip_id}/flights/verify")
def flight_verify(trip_id: str, body: OfferSelection):
    trip = _trip_or_404(trip_id)
    saved = _json(FLIGHT_DIR / f"{trip_id}.json", {})
    if not _saved_flight_search_matches_trip(saved, trip_id, trip):
        raise HTTPException(409, "Saved flight results no longer match the selected trip. Search again before verifying a fare.")
    known = _verified_route_offer_ids(saved)
    if body.offer_id not in known:
        raise HTTPException(404, "Offer is not part of a route-matching saved search result.")
    try:
        response = _flight_client().verify(body.offer_id)
        data = _unwrap_flight_data(response)
        verification_id = str(uuid4())
        FLIGHT_VERIFICATION_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = {"trip_id": trip_id, "offer_id": body.offer_id, "verification_id": verification_id, "verified_at": datetime.now(timezone.utc).isoformat(), "price": _flight_price(data)[0], "currency": _flight_price(data)[1], "expiration": data.get("expiration"), "response": response}
        _flight_verification_path(trip_id, verification_id).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        return {"data": data, "verification_id": verification_id, "verified_at": snapshot["verified_at"]}
    except Exception as exc:
        raise _provider_error(exc) from exc


@app.post("/api/trips/{trip_id}/flights/prebook")
def flight_prebook(trip_id: str, body: FlightPrebookRequest):
    trip = _trip_or_404(trip_id)
    payment_mode = _flight_payment_mode()
    if payment_mode == "unsupported":
        raise HTTPException(403, {"code": "FLIGHT_PAYMENT_MODE_UNSUPPORTED", "message": "Flight checkout is disabled. Configure FLIGHT_PAYMENT_MODE=nuitee_sdk to use LiteAPI's documented payment SDK flow."})
    if os.getenv("ALLOW_FLIGHT_PREBOOK", "false").lower() != "true":
        raise HTTPException(403, {"code": "FLIGHT_PREBOOK_DISABLED", "message": "Flight prebooking is disabled. Keep it disabled until Nuitee confirms Flights access and the selected payment flow is authorized."})
    if not _flight_access_verified:
        raise HTTPException(403, {"code": "FLIGHT_ACCESS_UNVERIFIED", "message": "Run a successful Flights access check or flight search with the sandbox key before prebooking."})
    saved = _json(FLIGHT_DIR / f"{trip_id}.json", {})
    if not _saved_flight_search_matches_trip(saved, trip_id, trip):
        raise HTTPException(409, "Saved flight results no longer match the selected trip. Search again before checkout.")
    known = _verified_route_offer_ids(saved)
    if body.offer_id not in known:
        raise HTTPException(404, "Offer is not part of a route-matching saved flight search result.")
    selection = _saved_flight_selection(saved, body.offer_id)
    if selection is None:
        raise HTTPException(409, "The selected flight offer could not be matched to its saved route. Search again before checkout.")
    selected_journey, selected_offer = selection
    _require_sandbox_booking()
    verification = _validate_flight_checkout(trip, saved, body)
    # Reverify immediately before prebook. A changed price creates a new
    # verification reference and requires a separate user confirmation.
    try:
        latest_response = _flight_client().verify(body.offer_id)
    except Exception as exc:
        raise _provider_error(exc) from exc
    latest = _unwrap_flight_data(latest_response)
    latest_price, latest_currency = _flight_price(latest)
    expiration = latest.get("expiration")
    if isinstance(expiration, str):
        try:
            expires_at = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(502, {"code": "FLIGHT_EXPIRATION_INVALID", "message": "The provider returned an invalid offer expiration. Verify the fare again before checkout."}) from exc
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(409, {"code": "FLIGHT_OFFER_EXPIRED", "message": "This provider offer has expired. Select and verify a current offer before checkout."})
    if (latest_price, latest_currency) != (verification.get("price"), verification.get("currency")):
        verification_id = str(uuid4())
        FLIGHT_VERIFICATION_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = {"trip_id": trip_id, "offer_id": body.offer_id, "verification_id": verification_id, "verified_at": datetime.now(timezone.utc).isoformat(), "price": latest_price, "currency": latest_currency, "expiration": latest.get("expiration"), "response": latest_response}
        _flight_verification_path(trip_id, verification_id).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        raise HTTPException(409, {"code": "FLIGHT_PRICE_CHANGED", "message": "The provider fare changed. Review and confirm the new fare before continuing.", "verification_id": verification_id, "price": latest_price, "currency": latest_currency, "expiration": latest.get("expiration")})
    attempt_key = hashlib.sha256(f"{trip_id}:{body.offer_id}:{body.verification_id}".encode()).hexdigest()
    FLIGHT_PREBOOK_DIR.mkdir(parents=True, exist_ok=True)
    record_path = FLIGHT_PREBOOK_DIR / f"attempt_{attempt_key}.json"
    if record_path.exists():
        previous = _json(record_path, {})
        raise HTTPException(409, {"code": "FLIGHT_PREBOOK_ATTEMPT_EXISTS", "status": previous.get("status"), "message": "A prebook attempt already exists for this verified fare. Check its status before retrying."})
    client_reference = f"WF-FLT-SBX-{uuid4().hex[:12].upper()}"
    criteria = saved.get("criteria") if isinstance(saved.get("criteria"), dict) else {}
    itinerary = [
        {key: leg.get(key) for key in ("origin", "destination", "date", "direction")}
        for leg in criteria.get("legs", [])
        if isinstance(leg, dict)
    ]
    passenger_counts = {
        "adults": int(criteria.get("adults", 0)),
        "children": int(criteria.get("children", 0)),
        "infants": int(criteria.get("infants", 0)),
    }
    active_checkout_statuses = {
        "PREBOOK_PENDING",
        "PAYMENT_PENDING",
        "PAYMENT_CONFIRMED",
        "BOOKING_PENDING_CONFIRMATION",
        "REQUIRES_RECONCILIATION",
    }
    existing_attempts = FLIGHT_PREBOOK_DIR.glob("attempt_*.json")
    for existing_path in existing_attempts:
        existing = _json(existing_path, {})
        if (
            existing.get("trip_id") == trip_id
            and existing.get("offer_id") == body.offer_id
            and existing.get("status") in active_checkout_statuses
        ):
            raise HTTPException(409, {
                "code": "FLIGHT_PREBOOK_ATTEMPT_EXISTS",
                "status": existing.get("status"),
                "prebook_id": existing.get("prebook_id"),
                "message": "A checkout for this exact flight offer is already in progress or requires reconciliation. Do not create another prebook; resume or reconcile the existing provider checkout.",
            })
    attempt = {"trip_id": trip_id, "offer_id": body.offer_id, "verification_id": body.verification_id, "client_reference": client_reference, "status": "PREBOOK_PENDING", "created_at": datetime.now(timezone.utc).isoformat(), "currency": latest_currency, "price": latest_price, "itinerary": itinerary, "segments": [segment for segment in selected_journey.get("segments", []) if isinstance(segment, dict)], "baggage": selected_offer.get("baggage"), "fare_family": selected_offer.get("fareFamily") or selected_offer.get("fareFamilyName"), "passenger_counts": passenger_counts, "passenger_count": len(body.passengers)}
    try:
        with record_path.open("x", encoding="utf-8") as handle:
            json.dump(attempt, handle, indent=2)
    except FileExistsError:
        previous = _json(record_path, {})
        raise HTTPException(409, {"code": "FLIGHT_PREBOOK_ATTEMPT_EXISTS", "status": previous.get("status"), "message": "A prebook attempt already exists for this verified fare. Check its status before retrying."})
    payload = {
        "offerId": body.offer_id,
        "contact": body.contact,
        "passengers": body.passengers,
        "usePaymentSdk": True,
    }
    try:
        response = _flight_client().prebook(payload)
        data = _unwrap_flight_data(response)
        prebook_id = data.get("prebookId")
        if not isinstance(prebook_id, str) or not prebook_id.strip():
            attempt.update({"status": "REQUIRES_RECONCILIATION", "provider_prebook_id": prebook_id, "payment_mode": payment_mode})
            record_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
            raise HTTPException(502, {"code": "FLIGHT_PREBOOK_INCOMPLETE", "message": "Provider did not return a prebook ID. Check the provider dashboard before retrying."})
        transaction_id = data.get("transactionId")
        client_secret = data.get("secretKey")
        if not all(isinstance(value, str) and value.strip() for value in (transaction_id, client_secret)):
            attempt.update({"status": "REQUIRES_RECONCILIATION", "provider_prebook_id": prebook_id, "transaction_id": transaction_id})
            record_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
            raise HTTPException(502, {"code": "FLIGHT_PREBOOK_INCOMPLETE", "message": "Provider did not return the payment references required by the LiteAPI payment SDK. Check the provider dashboard before retrying."})
        checkout_status = "PAYMENT_PENDING"
        provider_publishable_key = data.get("publishableKey")
        prebook_price, prebook_currency = _flight_price(data)
        prebook_price = prebook_price if prebook_price is not None else latest_price
        prebook_currency = prebook_currency or latest_currency
        attempt.update({
            "status": checkout_status,
            "prebook_id": prebook_id,
            "transaction_id": transaction_id,
            "secret_key": client_secret,
            "provider_publishable_key": provider_publishable_key if isinstance(provider_publishable_key, str) else None,
            "payment_mode": payment_mode,
            "price": prebook_price,
            "currency": prebook_currency,
            "prebooked_at": datetime.now(timezone.utc).isoformat(),
        })
        record_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
        return {
            "prebook_id": prebook_id,
            "transaction_id": transaction_id,
            "client_reference": client_reference,
            "secret_key": client_secret,
            "payment_mode": payment_mode,
            "payment_required": True,
            "provider_publishable_key_provided": isinstance(provider_publishable_key, str) and bool(provider_publishable_key),
            "price": prebook_price,
            "currency": prebook_currency,
            "services_attachable": data.get("servicesAttachable"),
            "status": checkout_status,
        }
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        attempt.update({"status": "REQUIRES_RECONCILIATION", "error_type": type(exc).__name__})
        record_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
        raise _provider_error(exc) from exc


@app.post("/api/flights/payment-return")
def flight_payment_return(body: FlightPaymentReturn):
    """Record the documented successful-payment redirect against its saved checkout."""
    prebook_files = list(FLIGHT_PREBOOK_DIR.glob("attempt_*.json")) if FLIGHT_PREBOOK_DIR.exists() else []
    matching_path = None
    checkout: dict[str, Any] = {}
    for path in prebook_files:
        candidate = _json(path, {})
        if (
            isinstance(candidate, dict)
            and candidate.get("prebook_id") == body.prebook_id
            and candidate.get("transaction_id") == body.transaction_id
        ):
            matching_path = path
            checkout = candidate
            break
    if matching_path is None:
        raise HTTPException(404, "The payment return does not match a saved Wayfarer flight checkout.")
    user = _current_user.get()
    trip_id = checkout.get("trip_id")
    if user is None or not isinstance(trip_id, str) or not auth_store.owns_trip(user["user_id"], trip_id):
        raise HTTPException(404, "The payment return does not match a checkout owned by this account.")
    if checkout.get("payment_mode") != "nuitee_sdk":
        raise HTTPException(409, "The payment return does not match the configured LiteAPI payment flow.")
    if checkout.get("status") == "PAYMENT_PENDING":
        checkout.update({
            "status": "PAYMENT_CONFIRMED",
            "payment_returned_at": datetime.now(timezone.utc).isoformat(),
        })
        checkout.pop("secret_key", None)
        matching_path.write_text(json.dumps(checkout, indent=2), encoding="utf-8")
    elif checkout.get("status") not in (
        "PAYMENT_CONFIRMED",
        "BOOKING_PENDING_CONFIRMATION",
        "CONFIRMED_SANDBOX",
        "REQUIRES_RECONCILIATION",
    ):
        raise HTTPException(409, {
            "code": "FLIGHT_PAYMENT_RETURN_NOT_APPLICABLE",
            "status": checkout.get("status"),
            "message": "This flight checkout is no longer awaiting payment. Do not submit it again; check its current booking status.",
        })
    booking_attempt = None
    if isinstance(checkout.get("booking_attempt_id"), str):
        booking_attempt = _json(
            FLIGHT_BOOKING_ATTEMPT_DIR / f"{checkout['booking_attempt_id']}.json"
        )
    if isinstance(booking_attempt, dict):
        return {
            "trip_id": checkout.get("trip_id"),
            "offer_id": checkout.get("offer_id"),
            "prebook_id": checkout.get("prebook_id"),
            "client_reference": checkout.get("client_reference"),
            "status": booking_attempt.get("status"),
            "booking_id": booking_attempt.get("booking_id"),
            "provider_booking_id": booking_attempt.get("provider_booking_id"),
            "confirmation_reference": booking_attempt.get("confirmation_reference"),
            "provider_status": booking_attempt.get("provider_status"),
            "price": booking_attempt.get("price", checkout.get("price")),
            "currency": booking_attempt.get("currency", checkout.get("currency")),
            "itinerary": checkout.get("itinerary", []),
            "segments": booking_attempt.get("segments", checkout.get("segments", [])),
            "baggage": booking_attempt.get("baggage", checkout.get("baggage")),
            "fare_family": booking_attempt.get("fare_family", checkout.get("fare_family")),
            "passenger_counts": checkout.get("passenger_counts", {}),
            "booking_attempt": booking_attempt,
        }
    return {
        "trip_id": checkout.get("trip_id"),
        "offer_id": checkout.get("offer_id"),
        "prebook_id": checkout.get("prebook_id"),
        "client_reference": checkout.get("client_reference"),
        "status": checkout.get("status"),
        "price": checkout.get("price"),
        "currency": checkout.get("currency"),
        "itinerary": checkout.get("itinerary", []),
        "segments": checkout.get("segments", []),
        "baggage": checkout.get("baggage"),
        "fare_family": checkout.get("fare_family"),
        "passenger_counts": checkout.get("passenger_counts", {}),
        "message": "The LiteAPI payment SDK returned successfully. Review the checkout and explicitly confirm provider booking.",
    }


@app.get("/api/trips/{trip_id}/flights/prebooks/{prebook_id}")
def flight_prebook_status(trip_id: str, prebook_id: str):
    _trip_or_404(trip_id)
    prebook_files = list(FLIGHT_PREBOOK_DIR.glob("attempt_*.json")) if FLIGHT_PREBOOK_DIR.exists() else []
    prebook_path = next((path for path in prebook_files if _json(path, {}).get("prebook_id") == prebook_id), None)
    if prebook_path is None:
        raise HTTPException(404, "Flight prebook not found.")
    checkout = _json(prebook_path, {})
    if checkout.get("trip_id") != trip_id:
        raise HTTPException(404, "Flight prebook not found for this trip.")
    attempt = None
    attempt_id = checkout.get("booking_attempt_id")
    if isinstance(attempt_id, str):
        attempt = _json(FLIGHT_BOOKING_ATTEMPT_DIR / f"{attempt_id}.json")
    return {
        "trip_id": checkout.get("trip_id"),
        "offer_id": checkout.get("offer_id"),
        "prebook_id": checkout.get("prebook_id"),
        "client_reference": checkout.get("client_reference"),
        "status": checkout.get("status"),
        "price": checkout.get("price"),
        "currency": checkout.get("currency"),
        "itinerary": checkout.get("itinerary", []),
        "segments": checkout.get("segments", []),
        "baggage": checkout.get("baggage"),
        "fare_family": checkout.get("fare_family"),
        "booking_attempt": attempt,
    }


@app.post("/api/trips/{trip_id}/flights/booking")
def flight_booking(trip_id: str, body: FlightBookingRequest):
    _trip_or_404(trip_id)
    payment_mode = _flight_payment_mode()
    if os.getenv("ALLOW_FLIGHT_BOOKING", "false").lower() != "true":
        raise HTTPException(403, "Flight booking submission is disabled. Configure a reviewed payment flow before enabling it.")
    if payment_mode != "nuitee_sdk":
        raise HTTPException(403, "Flight booking is disabled because the LiteAPI payment SDK flow is not configured.")
    _require_sandbox_booking()
    if body.confirm_booking is not True:
        raise HTTPException(422, "Explicit flight booking confirmation is required before submitting the provider booking.")
    prebook_files = list(FLIGHT_PREBOOK_DIR.glob("attempt_*.json")) if FLIGHT_PREBOOK_DIR.exists() else []
    prebook_path = next((path for path in prebook_files if _json(path, {}).get("prebook_id") == body.prebook_id), None)
    if prebook_path is None:
        raise HTTPException(404, "The prebook does not belong to a saved Wayfarer checkout.")
    checkout = _json(prebook_path, {})
    if checkout.get("trip_id") != trip_id or checkout.get("payment_mode") != payment_mode:
        raise HTTPException(422, "Prebook or payment mode does not match this trip checkout.")
    if checkout.get("status") != "PAYMENT_CONFIRMED":
        raise HTTPException(409, {"code": "FLIGHT_CHECKOUT_NOT_PAYABLE", "status": checkout.get("status"), "message": "The LiteAPI payment SDK has not returned a successful payment confirmation for this checkout."})
    transaction_id = checkout.get("transaction_id")
    if not isinstance(transaction_id, str) or not transaction_id.strip():
        raise HTTPException(409, "The saved LiteAPI transaction reference is unavailable. Do not submit this booking; contact support.")
    # One persistent guard per prebook; do not blindly retry uncertain outcomes.
    FLIGHT_BOOKING_ATTEMPT_DIR.mkdir(parents=True, exist_ok=True)
    attempt_id = str(uuid4())
    record_path = FLIGHT_BOOKING_ATTEMPT_DIR / f"{attempt_id}.json"
    if checkout.get("booking_attempt_id"):
        prior_path = FLIGHT_BOOKING_ATTEMPT_DIR / f"{checkout['booking_attempt_id']}.json"
        raise HTTPException(409, {"code": "FLIGHT_BOOKING_ATTEMPT_EXISTS", "status": _json(prior_path, {}).get("status"), "message": "A booking attempt already exists for this paid prebook. Check its provider status before taking further action."})
    claim_key = hashlib.sha256(body.prebook_id.encode("utf-8")).hexdigest()
    claim_path = FLIGHT_BOOKING_ATTEMPT_DIR / f"claim_{claim_key}.json"
    claim = {"trip_id": trip_id, "prebook_id": body.prebook_id, "booking_id": attempt_id, "status": "BOOKING_SUBMISSION_CLAIMED", "claimed_at": datetime.now(timezone.utc).isoformat()}
    try:
        with claim_path.open("x", encoding="utf-8") as handle:
            json.dump(claim, handle, indent=2)
    except FileExistsError:
        previous = _json(claim_path, {})
        prior_path = FLIGHT_BOOKING_ATTEMPT_DIR / f"{previous.get('booking_id', '')}.json"
        raise HTTPException(409, {"code": "FLIGHT_BOOKING_ATTEMPT_EXISTS", "status": _json(prior_path, {}).get("status", previous.get("status")), "message": "A booking submission was already claimed for this prebook. Reconcile its provider status before taking further action."})
    attempt = {"booking_id": attempt_id, "trip_id": trip_id, "provider": "LiteAPI Flights", "is_sandbox": True, "status": "BOOKING_PENDING_CONFIRMATION", "payment_mode": payment_mode, "prebook_id": body.prebook_id, "offer_id": checkout.get("offer_id"), "transaction_id": transaction_id, "client_reference": checkout.get("client_reference"), "attempted_at": datetime.now(timezone.utc).isoformat(), "currency": checkout.get("currency"), "price": checkout.get("price"), "itinerary": checkout.get("itinerary", []), "segments": checkout.get("segments", []), "baggage": checkout.get("baggage"), "fare_family": checkout.get("fare_family"), "passenger_counts": checkout.get("passenger_counts", {}), "passenger_count": checkout.get("passenger_count")}
    record_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
    checkout.update({"status": "BOOKING_PENDING_CONFIRMATION", "booking_attempt_id": attempt_id})
    prebook_path.write_text(json.dumps(checkout, indent=2), encoding="utf-8")
    try:
        result = _flight_client().book({"prebookId": body.prebook_id, "payment": {"method": "TRANSACTION_ID", "transactionId": transaction_id}})
        data = _unwrap_flight_data(result)
        provider_booking_id = data.get("bookingId")
        if not provider_booking_id:
            attempt.update({"status": "REQUIRES_RECONCILIATION"})
        else:
            # Persist the provider ID before lookup so an uncertain GET can be reconciled
            # without submitting a duplicate booking.
            attempt.update({"provider_booking_id": provider_booking_id, "status": "BOOKING_PENDING_VERIFICATION"})
            record_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
            verified_response = _flight_client().get_booking(str(provider_booking_id))
            verified = _unwrap_flight_data(verified_response)
            provider_status = str(verified.get("status", "")).upper()
            status = "CONFIRMED_SANDBOX" if provider_status == "CONFIRMED" else "REQUIRES_RECONCILIATION"
            verified_price, verified_currency = _flight_price(verified)
            result_price, result_currency = _flight_price(data)
            booking = verified.get("booking") if isinstance(verified.get("booking"), dict) else {}
            order = booking.get("order") if isinstance(booking.get("order"), dict) else {}
            reference = order.get("reference") if isinstance(order.get("reference"), dict) else {}
            attempt.update({"status": status, "provider_status": provider_status or "UNKNOWN", "provider_booking_id": provider_booking_id, "confirmation_reference": verified.get("bookingRef") or data.get("bookingRef") or reference.get("orderId"), "price": verified_price if verified_price is not None else result_price if result_price is not None else checkout.get("price"), "currency": verified_currency or result_currency or checkout.get("currency"), "verified_at": datetime.now(timezone.utc).isoformat(), "is_sandbox": True})
            checkout.update({"status": status, "provider_booking_id": provider_booking_id})
            prebook_path.write_text(json.dumps(checkout, indent=2), encoding="utf-8")
            if status == "CONFIRMED_SANDBOX":
                FLIGHT_BOOKING_DIR.mkdir(parents=True, exist_ok=True)
                (FLIGHT_BOOKING_DIR / f"{attempt_id}.json").write_text(json.dumps(attempt, indent=2), encoding="utf-8")
    except Exception as exc:
        diagnostic = _provider_error(exc)
        safe_detail = diagnostic.detail if isinstance(diagnostic.detail, dict) else {}
        attempt.update({"status": "REQUIRES_RECONCILIATION", "error_type": type(exc).__name__, "provider_status": safe_detail.get("provider_status"), "provider_code": safe_detail.get("provider_code")})
    record_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
    return attempt


@app.get("/api/trips/{trip_id}/flights/bookings")
def flight_bookings(trip_id: str):
    _trip_or_404(trip_id)
    if not FLIGHT_BOOKING_DIR.exists(): return []
    return [r for p in FLIGHT_BOOKING_DIR.glob("*.json") if not p.name.startswith("claim_") and (r := _json(p, {})).get("trip_id") == trip_id and r.get("status") == "CONFIRMED_SANDBOX" and r.get("provider_booking_id")]


@app.post("/api/trips/{trip_id}/flights/bookings/{booking_id}/verify")
def verify_flight_booking(trip_id: str, booking_id: str):
    """Explicitly reconcile a known provider booking ID without resubmitting it."""
    _trip_or_404(trip_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", booking_id):
        raise HTTPException(422, "Invalid local booking reference.")
    path = FLIGHT_BOOKING_DIR / f"{booking_id}.json"
    attempt = _json(path)
    if attempt is None:
        path = FLIGHT_BOOKING_ATTEMPT_DIR / f"{booking_id}.json"
        attempt = _json(path)
    if not isinstance(attempt, dict) or attempt.get("trip_id") != trip_id:
        raise HTTPException(404, "Flight booking record not found for this trip.")
    provider_booking_id = attempt.get("provider_booking_id")
    if not provider_booking_id:
        raise HTTPException(409, {"code": "PROVIDER_BOOKING_ID_UNAVAILABLE", "message": "The provider did not return a booking ID. Do not retry the booking; reconcile this attempt with Nuitee using its transaction and client references."})
    try:
        response = _flight_client().get_booking(str(provider_booking_id))
    except Exception as exc:
        diagnostic = _provider_error(exc)
        detail = diagnostic.detail if isinstance(diagnostic.detail, dict) else {}
        attempt.update({"status": "REQUIRES_RECONCILIATION", "provider_status": detail.get("provider_status"), "provider_code": detail.get("provider_code")})
        path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
        raise diagnostic from exc
    data = _unwrap_flight_data(response)
    provider_status = str(data.get("status", "")).upper()
    attempt.update({"status": "CONFIRMED_SANDBOX" if provider_status == "CONFIRMED" else "REQUIRES_RECONCILIATION", "provider_status": provider_status or "UNKNOWN", "confirmation_reference": data.get("bookingRef") or attempt.get("confirmation_reference"), "verified_at": datetime.now(timezone.utc).isoformat()})
    if attempt["status"] == "CONFIRMED_SANDBOX":
        FLIGHT_BOOKING_DIR.mkdir(parents=True, exist_ok=True)
        (FLIGHT_BOOKING_DIR / f"{booking_id}.json").write_text(json.dumps(attempt, indent=2), encoding="utf-8")
        prebook_files = list(FLIGHT_PREBOOK_DIR.glob("attempt_*.json")) if FLIGHT_PREBOOK_DIR.exists() else []
        prebook_path = next((item for item in prebook_files if _json(item, {}).get("booking_attempt_id") == booking_id), None)
        if prebook_path:
            checkout = _json(prebook_path, {})
            checkout.update({"status": "CONFIRMED_SANDBOX", "provider_booking_id": attempt.get("provider_booking_id")})
            prebook_path.write_text(json.dumps(checkout, indent=2), encoding="utf-8")
    path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")
    return attempt


@app.get("/api/trips/{trip_id}/bookings")
def trip_bookings(trip_id: str):
    _trip_or_404(trip_id)
    hotels = list_bookings_for_trip(trip_id)
    flights = flight_bookings(trip_id)
    return {"hotels": hotels, "flights": flights}


@app.get("/api/bookings/{booking_id}")
def get_booking(booking_id: str):
    for directory in (ROOT / "data" / "hotel_bookings", FLIGHT_BOOKING_DIR):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", booking_id): break
        path = directory / f"{booking_id}.json"
        record = _json(path)
        if record is not None:
            return {k: record.get(k) for k in ("booking_id", "trip_id", "hotel_name", "provider", "status", "is_sandbox", "provider_booking_id", "confirmation_reference", "hotel_confirmation_code", "remote_verification", "price", "currency", "created_at", "attempted_at") if k in record}
    raise HTTPException(404, "Booking not found.")


def _chat_pending_field(
    history: list[ChatTurn],
    missing_fields: set[str] | None = None,
    preferred_field: str | None = None,
) -> tuple[str, str] | None:
    candidates = (
        ("origin", r"\b(origin|travel from|which city.*from)\b"),
        ("destination", r"\b(destination|where.*visit|where.*go)\b"),
        ("departure_date", r"\b(departure date|when.*depart|what dates? (?:are you considering|do you have)|travel dates?|date range|valid departure and return date)\b"),
        ("return_date", r"\b(return date|when.*return|return-flight)\b"),
        ("nights", r"\b(nights|how long.*stay)\b"),
    )
    questions = {
        "origin": "Which city will you depart from?",
        "destination": "Which destination would you like to visit?",
        "departure_date": "What dates are you considering?",
        "return_date": "What is your return date?",
        "nights": "How many nights should I plan for?",
    }
    if preferred_field and preferred_field in questions:
        candidates = tuple(item for item in candidates if item[0] == preferred_field)
    for turn in reversed(history):
        if turn.role != "assistant":
            continue
        question = turn.content.lower()
        for field, pattern in candidates:
            if missing_fields is not None and field not in missing_fields:
                continue
            if re.search(pattern, question):
                return field, turn.content
    if preferred_field and preferred_field in questions:
        return preferred_field, questions[preferred_field]
    return None


def _message_answers_pending(
    message: str,
    field: str,
    request: TravelRequest,
) -> bool:
    text = message.strip()
    if not text or "?" in text:
        return False
    if field in {"departure_date", "return_date"}:
        if not re.search(
            r"\b\d{1,4}(?:st|nd|rd|th)?\b|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
            r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow|"
            r"next week|next month|this month|next weekend|this weekend|"
            r"not sure|don't know|do not know|undecided)\b",
            text,
            re.IGNORECASE,
        ):
            return False
        context_date = request.departure_date or request.return_date
        parsed, issue = extract_date_updates(
            text,
            pending_field=field,
            context_date=context_date,
        )
        return bool(parsed or issue)
    if field in {"origin", "destination"}:
        if len(text.split()) <= 4 and re.fullmatch(r"[\w\s,.'-]+", text):
            return True
        expected_markers = r"\b(from|depart(?:ing)?\s+from|origin(?:\s+is)?|to|destination(?:\s+is)?)\b"
        return bool(re.search(expected_markers, text, re.IGNORECASE))
    if field in {"nights", "adults", "children", "rooms"}:
        return bool(re.search(r"\b\d+\b|\b(one|two|three|four|five|six|seven|eight|nine|ten)\b", text, re.IGNORECASE))
    return False


def _is_tulip_season_question(message: str) -> bool:
    return bool(re.search(r"\btulip(?:s)?\b", message, re.IGNORECASE))


def _is_weather_question(message: str) -> bool:
    return bool(re.search(r"\b(weather|forecast|snow|rain|temperature|current conditions|rainfall|precipitation)\b", message, re.IGNORECASE))


def _is_planning_request(message: str, draft_plan: bool = False) -> bool:
    return draft_plan or bool(
        re.search(r"\b(plan|planning|itinerary|travel plan|trip plan)\b", message, re.IGNORECASE)
    )


def _validate_draft_date_updates(
    current: TravelRequest,
    updates: dict[str, Any],
) -> str | None:
    """Reject invalid date changes without discarding other extracted fields."""
    date_keys = {"departure_date", "return_date"}
    incoming = {"departure_date", "return_date", "trip_duration_days", "nights"}.intersection(updates)
    if not incoming:
        return None
    incoming_dates = date_keys.intersection(updates)

    def parse(value: Any) -> date | None:
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
        return None

    departure = parse(updates.get("departure_date", current.departure_date))
    return_date = parse(updates.get("return_date", current.return_date))
    issue = None
    today = local_today()
    if incoming_dates and departure and departure <= today:
        issue = f"Your departure date ({departure.isoformat()}) is not in the future."
    elif incoming_dates and return_date and return_date <= today:
        issue = f"Your return date ({return_date.isoformat()}) is not in the future."
    elif departure and return_date and return_date <= departure:
        issue = (
            f"Your return date ({return_date.isoformat()}) must be later than "
            f"your departure date ({departure.isoformat()})."
        )
    elif departure and return_date and updates.get("trip_duration_days", current.trip_duration_days):
        duration_days = updates.get("trip_duration_days", current.trip_duration_days)
        actual_days = (return_date - departure).days + 1
        if actual_days != duration_days:
            issue = (
                f"That date range covers {actual_days} calendar days, but you asked "
                f"for {duration_days} days. Would you like to change "
                "the dates or the trip duration?"
            )
    if not issue and departure and return_date and updates.get("nights", current.nights):
        nights = updates.get("nights", current.nights)
        actual_nights = (return_date - departure).days
        if actual_nights != nights:
            issue = (
                f"That date range is {actual_nights} nights, but you asked for "
                f"{nights} nights. Would you like to change the dates or "
                "the stay duration?"
            )

    if issue:
        for field in incoming:
            updates.pop(field, None)
        return issue

    if departure and return_date and current.hotel_required and not current.nights and "nights" not in updates:
        updates["nights"] = (return_date - departure).days
    return None


def _duration_conflict_options(
    current: TravelRequest,
    updates: dict[str, Any],
) -> dict[str, Any] | None:
    """Build explicit, date-validated alternatives for a return/duration mismatch."""
    if not {"departure_date", "return_date"}.intersection(updates):
        return None
    departure_value = updates.get("departure_date", current.departure_date)
    return_value = updates.get("return_date", current.return_date)
    if return_value is None:
        return None
    try:
        departure = (
            departure_value if isinstance(departure_value, date)
            else date.fromisoformat(str(departure_value))
        )
        proposed_return = (
            return_value if isinstance(return_value, date)
            else date.fromisoformat(str(return_value))
        )
    except (TypeError, ValueError):
        return None
    nights = updates.get("nights", current.nights)
    calendar_days = updates.get("trip_duration_days", current.trip_duration_days)
    if nights is None and calendar_days is None:
        return None
    if nights is None:
        nights = calendar_days - 1
    if calendar_days is None:
        calendar_days = nights + 1
    actual_nights = (proposed_return - departure).days
    actual_days = actual_nights + 1
    if actual_nights <= 0 or (actual_nights == nights and actual_days == calendar_days):
        return None
    kept_duration_return = departure + timedelta(days=int(nights))
    return {
        "kind": "resolve_duration_conflict",
        "departure_date": departure.isoformat(),
        "proposed_return_date": proposed_return.isoformat(),
        "option1": {
            "return_date": kept_duration_return.isoformat(),
            "nights": int(nights),
            "trip_duration_days": int(nights) + 1,
        },
        "option2": {
            "return_date": proposed_return.isoformat(),
            "nights": actual_nights,
            "trip_duration_days": actual_days,
        },
    }


def _duration_option_prompt(state: dict[str, Any]) -> str:
    departure = date.fromisoformat(state["departure_date"])
    proposed_return = date.fromisoformat(state["proposed_return_date"])
    option1 = state["option1"]
    option2 = state["option2"]
    first_return = date.fromisoformat(option1["return_date"])
    return (
        f"Your dates conflict with the requested duration. "
        f"Option 1 – Keep {option1['nights']} nights "
        f"({option1['trip_duration_days']} calendar days): "
        f"{departure.strftime('%B')} {departure.day}–{first_return.strftime('%B')} "
        f"{first_return.day}, {first_return.year}. "
        f"Option 2 – Keep the {proposed_return.strftime('%B')} {proposed_return.day} "
        f"return date: {option2['nights']} nights "
        f"({option2['trip_duration_days']} calendar days). "
        "Reply Option 1 or Option 2."
    )


def _select_duration_option(message: str, state: Any) -> int | None:
    if not isinstance(state, dict) or state.get("kind") != "resolve_duration_conflict":
        return None
    text = message.casefold()
    if re.search(r"\b(?:option\s*(?:1|one)|first\s+option)\b", text):
        return 1
    if re.search(r"\b(?:option\s*(?:2|two)|second\s+option)\b", text):
        return 2
    keep_nights = re.search(r"\bkeep\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+nights?\b", text)
    keep_days = re.search(r"\bkeep\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:calendar\s+)?days?\b", text)
    number_words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    }
    for match, field in ((keep_nights, "nights"), (keep_days, "trip_duration_days")):
        if match:
            chosen = int(match.group(1)) if match.group(1).isdigit() else number_words[match.group(1)]
            matches = [
                index for index in (1, 2)
                if state.get(f"option{index}", {}).get(field) == chosen
            ]
            if len(matches) == 1:
                return matches[0]
    return None


def _saved_trip_summary(request: TravelRequest) -> str:
    parts = []
    if request.origin or request.destination:
        parts.append(f"{request.origin or 'Origin'} → {request.destination or 'Destination'}")
    if request.departure_date and request.return_date:
        start, end = request.departure_date, request.return_date
        if start.year == end.year and start.month == end.month:
            date_range = f"{start.strftime('%B')} {start.day}–{end.day}, {end.year}"
        else:
            start_month = start.strftime('%b' if start.year != end.year else '%B')
            end_month = end.strftime('%b' if start.year != end.year else '%B')
            date_range = f"{start_month} {start.day}, {start.year}–{end_month} {end.day}, {end.year}"
        parts.append(date_range)
    elif request.departure_date:
        parts.append(f"Departing {request.departure_date.strftime('%B')} {request.departure_date.day}, {request.departure_date.year}")
    parts.append(f"{request.adults} adult{'s' if request.adults != 1 else ''}")
    if request.children:
        parts.append(f"{request.children} child{'ren' if request.children != 1 else ''}")
    if request.nights:
        parts.append(f"{request.nights} night{'s' if request.nights != 1 else ''}")
    if request.trip_duration_days:
        parts.append(f"{request.trip_duration_days} day{'s' if request.trip_duration_days != 1 else ''}")
    if request.rooms:
        parts.append(f"{request.rooms} hotel room{'s' if request.rooms != 1 else ''}")
    if request.hotel_budget_per_night is not None:
        parts.append(f"Hotel budget: {request.currency} {request.hotel_budget_per_night:g} per night combined across all rooms")
    if request.total_trip_budget is not None:
        parts.append(f"Total trip budget: {request.budget_currency or request.currency} {request.total_trip_budget:g}")
    if request.hotel_preferences:
        parts.append(f"Hotel preferences: {', '.join(request.hotel_preferences)}")
    if request.flight_preferences:
        parts.append(f"Flight preferences: {', '.join(request.flight_preferences)}")
    if request.transport_preferences:
        parts.append(f"Ground transport: {', '.join(request.transport_preferences)}")
    if request.preferred_transport_mode:
        parts.append(f"Preferred transport mode: {request.preferred_transport_mode.title()}")
    if request.accommodation_type:
        parts.append(f"Accommodation type: {request.accommodation_type.title()}")
    if request.interests:
        parts.append(f"Interests: {', '.join(request.interests)}")
    return " · ".join(parts)


def _draft_update_summary(updates: dict[str, Any], request: TravelRequest) -> str:
    details: list[str] = []
    if request.origin or request.destination:
        details.append(f"Route: {request.origin or 'Origin'} → {request.destination or 'Destination'}")
    if request.departure_date and request.return_date:
        start, end = request.departure_date, request.return_date
        start_text = f"{start.strftime('%B')} {start.day}"
        end_text = f"{end.strftime('%B')} {end.day}"
        date_text = (
            f"{start_text} – {end_text}, {end.year}"
            if start.year == end.year
            else f"{start_text}, {start.year} – {end_text}, {end.year}"
        )
        details.append(f"Dates: {date_text}")
    details.append(f"Travelers: {request.adults} adult{'s' if request.adults != 1 else ''}")
    if request.children:
        details[-1] += f", {request.children} child{'ren' if request.children != 1 else ''}"
    if request.nights is not None:
        duration = (
            f" ({request.trip_duration_days} calendar days)"
            if request.trip_duration_days is not None
            else ""
        )
        details.append(f"Duration: {request.nights} nights{duration}")
    if request.total_trip_budget is not None:
        budget_amount = f"{request.total_trip_budget:,.2f}".rstrip("0").rstrip(".")
        details.append(
            f"Total budget: {request.budget_currency or request.currency} {budget_amount}"
        )
    if request.preferred_transport_mode:
        details.append(f"Transport preference: {request.preferred_transport_mode.title()}")
    if request.accommodation_type:
        details.append(f"Accommodation: {request.accommodation_type.title()}")
    if request.interests:
        details.append("Interests: All major sightseeing experiences")
    if request.hotel_budget_per_night is not None:
        details.append(
            f"Hotel budget: {request.currency} {request.hotel_budget_per_night:g} per night"
        )
    if request.hotel_preferences:
        details.append(f"Hotel preferences: {', '.join(request.hotel_preferences)}")
    if request.flight_preferences:
        details.append(f"Flight preferences: {', '.join(request.flight_preferences)}")
    if request.transport_preferences:
        details.append(f"Ground transport requested: {', '.join(request.transport_preferences)} (not booked)")
    if not updates:
        return ""
    summary = "Updated your unsaved trip draft.\n" + "\n".join(f"- {detail}" for detail in details)
    return (
        summary
        + "\nNo hotel, flight, or transfer was booked."
        + "\nYour draft is still unsaved. You can ask me to create the itinerary, "
        "search available options, or save the draft to My Trips."
    )


def _mandarmoni_route_note(request: TravelRequest) -> str:
    origin = re.sub(r"[^a-z]", "", (request.origin or "").casefold())
    destination = re.sub(r"[^a-z]", "", (request.destination or "").casefold())
    if origin != "kolkata" or "mandarmoni" not in destination:
        return ""
    note = (
        "Kolkata–Mandarmani is generally road-accessible, so a ground transfer is the practical "
        "route to consider; any flight preference remains conditional. No flight search, transfer "
        "booking, or live price check was made."
    )
    if (
        request.total_trip_budget is not None
        and request.hotel_budget_per_night is not None
        and request.nights
        and (request.budget_currency or request.currency) == request.currency
    ):
        lodging_cap = request.hotel_budget_per_night * request.nights
        remaining = request.total_trip_budget - lodging_cap
        if remaining >= 0:
            budget_note = (
                f"and leave {request.currency} {remaining:g} for other costs"
            )
        else:
            budget_note = (
                f"and exceed the total-trip budget by {request.currency} "
                f"{abs(remaining):g}, before other costs"
            )
        note += (
            f" At the full nightly hotel cap, {request.nights} nights would allow up to "
            f"{request.currency} {lodging_cap:g} for lodging {budget_note}; "
            "this is budget arithmetic, not a verified price."
        )
    return note


def _safe_general_chat(message: str, history: list[ChatTurn], current: TravelRequest | None) -> str:
    from src.travel_agent.llm.router import generate

    recent = "\n".join(f"{turn.role}: {redact_personal_details(turn.content)}" for turn in history[-8:])
    context = current.model_dump_json(exclude={"status"}) if current else "No trip selected."
    prompt = f"""You are Wayfarer, a conversational travel assistant. Respond naturally and concisely.
Never claim that trip details were saved unless the application says they were. Never claim live searches, availability, prices, or bookings unless a provider service returned them. Use trip data only as context and never change saved data.

Saved trip context: {context}
Recent conversation:
{recent}
User: {redact_personal_details(message)}
Assistant:"""
    return generate(prompt).text.strip()


@app.post("/api/chat", response_model=ChatResponse)
def chat(body: ChatRequest):
    from src.travel_agent.workflows.clarification import get_next_field, get_next_question

    existing = _trip_or_404(body.trip_id) if body.trip_id else None
    text = body.message.strip()
    safe_text = redact_personal_details(text)
    lowered = text.lower().strip(" .,!?")
    conversation_id = body.conversation_id or str(uuid4())
    greeting = bool(re.fullmatch(r"(hi|hello|hey|good morning|good afternoon|good evening|how are you|what can you do|what do you do)", lowered))
    if greeting:
        return ChatResponse(
            reply="Hi! I can help you plan a trip, answer travel questions, and guide searches and bookings. Planning details stay in an unsaved draft until you ask me to save them.",
            trip_id=body.trip_id,
            travel_request=existing.model_dump(mode="json") if existing else None,
            conversation_id=conversation_id,
        )

    save_requested = bool(re.search(r"\b(save|add)\b.*\b(trip|draft|my trips)\b", lowered))
    if save_requested:
        if not body.draft_id:
            return ChatResponse(
                reply="There is no active planning draft to save. Start with “Plan a trip to …” and I’ll keep it unsaved until you ask to save.",
                conversation_id=conversation_id,
            )
        draft = _draft_or_404(body.draft_id, conversation_id)
        try:
            saved = save_planning_draft(body.draft_id)
        except HTTPException as exc:
            if exc.status_code == 422 and isinstance(exc.detail, dict):
                missing = exc.detail.get("missing", [])
                question = get_next_question(TravelRequest.model_validate(draft["travel_request"]))
                return ChatResponse(
                    reply=f"I haven’t saved the draft yet. {question or 'Please fill in the missing trip information first.'}",
                    draft_id=body.draft_id,
                    pending_field=draft.get("pending_field"),
                    conversation_id=conversation_id,
                    travel_request=draft["travel_request"],
                    missing=missing,
                    has_planning_draft=True,
                )
            raise
        return ChatResponse(
            reply="Saved the planning draft to My Trips.",
            trip_id=saved["trip_id"],
            trip_saved=True,
            travel_request=saved["travel_request"],
            status_message="Trip saved successfully",
            trip_summary=_saved_trip_summary(TravelRequest.model_validate(saved["travel_request"])),
            draft_id=body.draft_id,
            conversation_id=conversation_id,
            has_planning_draft=False,
        )

    explicit_saved_update = bool(
        re.search(r"\b(update|change|edit|modify)\b.*\b(saved|existing|selected)\s+trip\b", lowered)
        or re.search(r"\b(change|update|edit)\s+my\s+saved\s+trip\b", lowered)
    )
    new_plan_requested = bool(
        _is_planning_request(text)
        and not explicit_saved_update
        and (not body.draft_id or re.search(r"\b(new|another|different)\b", lowered))
    )
    active_draft: dict[str, Any] | None = None
    draft_request: TravelRequest | None = None
    if body.draft_id and not new_plan_requested:
        active_draft = _draft_or_404(body.draft_id, conversation_id)
        draft_request = TravelRequest.model_validate(active_draft.get("travel_request", {}))

    clarification_state = active_draft.get("clarification_state") if active_draft else None
    selected_duration_option = _select_duration_option(text, clarification_state)
    if (
        active_draft
        and active_draft.get("pending_field") == "resolve_duration_conflict"
        and selected_duration_option is None
        and re.fullmatch(r"\s*(?:yes|yeah|yep|correct|ok|okay|sure)[.! ]*\s*", lowered)
    ):
        return ChatResponse(
            reply=_duration_option_prompt(clarification_state),
            travel_request=draft_request.model_dump(mode="json") if draft_request else None,
            draft_id=body.draft_id,
            pending_field="resolve_duration_conflict",
            conversation_id=conversation_id,
            has_planning_draft=True,
        )

    context_request = draft_request or existing
    if _is_weather_question(text):
        location_match = re.search(
            r"\b(?:weather(?:\s+like)?|forecast|snow|rain|temperature|conditions)\s+(?:in|for|at)\s+(.+)",
            text,
            re.IGNORECASE,
        )
        location = location_match.group(1).strip(" .,!?") if location_match else ""
        location = re.split(
            r"\b(?:during my trip|for my trip|the saved trip|my trip|next week|this week|tomorrow|today|right now|currently)\b",
            location,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" .,!?")
        if location.lower() in {"there", "the trip", "my trip", "this trip"}:
            location = ""
        if not location and context_request:
            location = context_request.destination or ""
        if not location:
            return ChatResponse(
                reply="Which city or region should I check the current weather for?",
                trip_id=body.trip_id,
                travel_request=context_request.model_dump(mode="json") if context_request else None,
                draft_id=body.draft_id if active_draft else None,
                pending_field=active_draft.get("pending_field") if active_draft else None,
                conversation_id=conversation_id,
                has_planning_draft=bool(active_draft),
            )

        forecast_request = bool(re.search(r"\b(forecast|during my trip|for my trip|trip dates)\b", lowered))
        trip_date = context_request.departure_date if context_request else None
        requested_dates, _ = extract_date_updates(text)
        requested_date = None
        if requested_dates.get("departure_date"):
            requested_date = date.fromisoformat(requested_dates["departure_date"])
        requested_year_match = re.search(r"\b(20\d{2})\b", text)
        beyond_horizon = bool(
            (trip_date and trip_date > local_today() + timedelta(days=16))
            or (requested_date and requested_date > local_today() + timedelta(days=16))
            or (
                requested_year_match
                and int(requested_year_match.group(1)) > local_today().year
            )
            or re.search(r"\bnext year\b", lowered)
        )
        if forecast_request and beyond_horizon:
            forecast_date = requested_date or trip_date
            forecast_label = forecast_date.isoformat() if forecast_date else (
                requested_year_match.group(1) if requested_year_match else "that future date"
            )
            return ChatResponse(
                reply=(
                    f"I can’t provide a reliable forecast for {forecast_label}; "
                    "the supported forecast horizon is about 16 days. I have not changed your trip draft."
                ),
                trip_id=body.trip_id,
                travel_request=context_request.model_dump(mode="json") if context_request else None,
                draft_id=body.draft_id if active_draft else None,
                pending_field=active_draft.get("pending_field") if active_draft else None,
                conversation_id=conversation_id,
                has_planning_draft=bool(active_draft),
            )
        try:
            weather_result = get_current_weather(location)
            place = weather_result["location"]["name"]
            representative_note = weather_result["location"].get("representative_note")
            details = [f"{weather_result['conditions']} at {weather_result['observed_at']} ({weather_result['timezone']})."]
            details.append(
                f"Temperature {weather_result['temperature']}{weather_result['temperature_unit']} "
                f"(feels like {weather_result['feels_like']}{weather_result['temperature_unit']})."
                if weather_result.get("feels_like") is not None
                else f"Temperature {weather_result['temperature']}{weather_result['temperature_unit']}."
            )
            if weather_result.get("humidity") is not None:
                details.append(f"Humidity {weather_result['humidity']}%.")
            if weather_result.get("wind_speed") is not None:
                details.append(f"Wind {weather_result['wind_speed']} {weather_result['wind_speed_unit']}.")
            if weather_result.get("precipitation") is not None:
                details.append(
                    f"Precipitation {weather_result['precipitation']} {weather_result['precipitation_unit']}."
                )
            reply = f"Current weather for {place} from Open-Meteo: " + " ".join(details)
            if representative_note:
                reply += f" {representative_note}"
            return ChatResponse(
                reply=reply,
                trip_id=body.trip_id,
                travel_request=context_request.model_dump(mode="json") if context_request else None,
                draft_id=body.draft_id if active_draft else None,
                pending_field=active_draft.get("pending_field") if active_draft else None,
                conversation_id=conversation_id,
                has_planning_draft=bool(active_draft),
                weather_result=weather_result,
            )
        except WeatherLocationError as exc:
            return ChatResponse(
                reply=str(exc),
                trip_id=body.trip_id,
                travel_request=context_request.model_dump(mode="json") if context_request else None,
                draft_id=body.draft_id if active_draft else None,
                pending_field=active_draft.get("pending_field") if active_draft else None,
                conversation_id=conversation_id,
                has_planning_draft=bool(active_draft),
            )
        except WeatherProviderError as exc:
            logger.warning("Current weather provider failed: exception=%s", type(exc).__name__)
            return ChatResponse(
                reply="I couldn’t retrieve a live current observation from the weather provider. No trip data was changed; please try again later or specify a city.",
                trip_id=body.trip_id,
                travel_request=context_request.model_dump(mode="json") if context_request else None,
                draft_id=body.draft_id if active_draft else None,
                pending_field=active_draft.get("pending_field") if active_draft else None,
                conversation_id=conversation_id,
                has_planning_draft=bool(active_draft),
            )

    if _is_tulip_season_question(text):
        reply = (
            "Srinagar’s Indira Gandhi Memorial Tulip Garden generally blooms from late March to mid-April. "
            "The opening date and peak bloom vary with weather and the official annual schedule, so exact dates "
            "for a future year should be checked against the garden’s official announcement."
        )
        if context_request and context_request.departure_date and context_request.departure_date.month == 7:
            reply += " July is generally outside the tulip-blooming season."
        return ChatResponse(
            reply=reply,
            trip_id=body.trip_id,
            travel_request=context_request.model_dump(mode="json") if context_request else None,
            draft_id=body.draft_id if active_draft else None,
            pending_field=active_draft.get("pending_field") if active_draft else None,
            conversation_id=conversation_id,
            has_planning_draft=bool(active_draft),
        )

    if body.draft_plan and not body.draft_id:
        if existing is None:
            return ChatResponse(
                reply="Select a saved trip or start an unsaved plan before asking for an itinerary.",
                conversation_id=conversation_id,
            )
        missing_field = get_next_field(existing)
        if missing_field:
            question = get_next_question(existing) or "What trip detail should we fill in next?"
            return ChatResponse(
                reply=f"I can draft an itinerary after we fill in the trip details. {question}",
                trip_id=body.trip_id,
                travel_request=existing.model_dump(mode="json"),
                missing=[missing_field],
                conversation_id=conversation_id,
            )
        try:
            from src.travel_agent.workflows.travel_planning import create_travel_planning_crew
            crew = create_travel_planning_crew(existing, safe_text)
            result = crew.kickoff(inputs={
                "travel_request": safe_text + "\nAI planning draft only. No provider search, booking, or payment was run.",
                "validated_request": existing.model_dump_json(indent=2),
            })
            return ChatResponse(
                reply="Here is an AI-generated itinerary draft. It is not live availability or a booking confirmation.",
                trip_id=body.trip_id,
                travel_request=existing.model_dump(mode="json"),
                planning_brief=str(result),
                conversation_id=conversation_id,
            )
        except Exception as exc:
            logger.warning("Itinerary generation failed: exception=%s", type(exc).__name__)
            return ChatResponse(
                reply="I couldn’t generate an itinerary because the configured planning model is unavailable. Your saved trip is unchanged.",
                trip_id=body.trip_id,
                travel_request=existing.model_dump(mode="json"),
                conversation_id=conversation_id,
            )

    booking_service_intent = bool(
        re.search(r"\b(cancel|cancellation|refund|amend|change|modify|reissue|status|confirmed|confirmation|booked)\b", lowered)
        and re.search(r"\b(booking|reservation|flight|hotel|itinerary)\b", lowered)
    )
    if booking_service_intent:
        is_flight = bool(re.search(r"\b(flight|airline)\b", lowered))
        is_hotel = bool(re.search(r"\b(hotel|stay|accommodation)\b", lowered))
        is_status_check = bool(re.search(r"\b(status|confirmed|confirmation|booked)\b", lowered))
        if is_status_check:
            reply = "I can’t verify provider booking status in chat. Open My Bookings and use the booking’s status/reconciliation control; no provider request was made from this message."
        elif is_flight:
            reply = "Wayfarer does not submit flight cancellations, refunds, changes, or reissues. Those are handled through Nuitee’s flight support process; no flight servicing request was made."
        elif is_hotel:
            reply = "No hotel change was made from chat. In My Bookings, you can review current provider terms before separately confirming a cancellation. The available holder amendment covers only holder name and email, not dates, rooms, or occupancy."
        else:
            reply = "I can’t identify which booking you mean, and no provider action was made. Please specify the hotel or flight booking; flight servicing is handled through Nuitee support."
        return ChatResponse(
            reply=reply,
            trip_id=body.trip_id,
            travel_request=(draft_request or existing).model_dump(mode="json") if (draft_request or existing) else None,
            draft_id=body.draft_id if active_draft else None,
            pending_field=active_draft.get("pending_field") if active_draft else None,
            conversation_id=conversation_id,
            has_planning_draft=bool(active_draft),
        )

    weather_intent = bool(re.search(r"\b(weather|forecast|snow|rain|temperature)\b", lowered))
    if weather_intent:
        context_request = draft_request or existing
        location_match = re.search(
            r"\b(?:weather(?:\s+like)?|forecast|snow|rain|temperature)\s+(?:in|for|at)\s+(.+)",
            lowered,
        )
        location = location_match.group(1) if location_match else ""
        location = re.split(
            r"\b(?:during my trip|for my trip|the saved trip|my trip|next week|this week|tomorrow|today)\b",
            location,
            maxsplit=1,
        )[0].strip(" .,!?")
        location = re.sub(r"\s+like$", "", location, flags=re.IGNORECASE).strip()
        refers_to_trip = bool(re.search(r"\b(there|my trip|this trip|trip dates|saved destination)\b", lowered))
        if re.fullmatch(r"(?:the )?(?:(?:saved|my) )?trip|there", location):
            location = ""
        if not location and (refers_to_trip or context_request is not None):
            location = context_request.destination if context_request else ""
        if not location:
            return ChatResponse(
                reply="Which city or region should I check the weather for?",
                draft_id=body.draft_id if active_draft else None,
                pending_field=active_draft.get("pending_field") if active_draft else None,
                conversation_id=conversation_id,
                has_planning_draft=bool(active_draft),
            )
        today = date.today()
        start_day = today
        end_day = today
        if "during my trip" in lowered or "for my trip" in lowered or "trip dates" in lowered:
            if context_request and context_request.departure_date:
                start_day = context_request.departure_date
                end_day = context_request.return_date or start_day
        elif "tomorrow" in lowered:
            start_day = end_day = today + timedelta(days=1)
        elif "next week" in lowered:
            start_day = today + timedelta(days=7)
            end_day = today + timedelta(days=13)
        elif "this week" in lowered:
            end_day = today + timedelta(days=6)
        try:
            weather_result = get_weather(location, start_day, min(end_day, start_day + timedelta(days=15)))
            summaries = []
            for forecast in weather_result["forecasts"]:
                details = []
                if forecast.get("conditions"):
                    details.append(str(forecast["conditions"]))
                low, high = forecast.get("temperature_min"), forecast.get("temperature_max")
                if isinstance(low, (int, float)) and isinstance(high, (int, float)):
                    unit = "°F" if weather_result["units"] == "imperial" else "°C"
                    details.append(f"{low:g}–{high:g}{unit}")
                if isinstance(forecast.get("precipitation_probability"), (int, float)):
                    details.append(f"{forecast['precipitation_probability'] * 100:.0f}% precipitation chance")
                if details:
                    summaries.append(f"- {forecast['date']}: {', '.join(details)}")
            horizon_note = f"\n\n{weather_result['accuracy_note']}" if weather_result.get("accuracy_note") else ""
            reply = (
                f"LiteAPI weather for {weather_result['location']['name']} "
                f"({weather_result['start_date']} to {weather_result['end_date']}):\n"
                + ("\n".join(summaries) if summaries else "The provider returned no daily summary fields.")
                + horizon_note
            )
            return ChatResponse(
                reply=reply,
                trip_id=body.trip_id,
                travel_request=context_request.model_dump(mode="json") if context_request else None,
                draft_id=body.draft_id if active_draft else None,
                pending_field=active_draft.get("pending_field") if active_draft else None,
                conversation_id=conversation_id,
                has_planning_draft=bool(active_draft),
                weather_result=weather_result,
            )
        except WeatherLocationError as exc:
            return ChatResponse(
                reply=str(exc),
                draft_id=body.draft_id if active_draft else None,
                conversation_id=conversation_id,
                has_planning_draft=bool(active_draft),
            )
        except (LiteAPIError, ValueError) as exc:
            logger.warning("Weather request failed: exception=%s", type(exc).__name__)
            return ChatResponse(
                reply="I couldn’t retrieve provider weather for that location or date range. No trip data was changed; please try a specific city and a period of up to 16 days.",
                draft_id=body.draft_id if active_draft else None,
                conversation_id=conversation_id,
                has_planning_draft=bool(active_draft),
            )

    hotel_search_intent = bool(
        re.search(r"\b(find|search|show|look for|check)\b", lowered)
        and re.search(r"\b(hotels?|stays?|accommodation)\b", lowered)
        and not re.search(r"\b(cancel|modify|change|policy|refund)\b", lowered)
    )
    if hotel_search_intent:
        if active_draft:
            return ChatResponse(
                reply="I have not searched yet. Save this unsaved plan to My Trips first; live hotel search currently requires a saved trip and its explicit combined nightly hotel budget.",
                draft_id=body.draft_id,
                pending_field=active_draft.get("pending_field"),
                conversation_id=conversation_id,
                travel_request=draft_request.model_dump(mode="json") if draft_request else None,
                has_planning_draft=True,
            )
        if not existing or not body.trip_id:
            return ChatResponse(
                reply="Select or save the trip you want to search, then ask me to find hotels. No provider search has run.",
                conversation_id=conversation_id,
            )
        if not existing.hotel_budget_per_night:
            return ChatResponse(
                reply="Set the combined hotel budget per night for all rooms in the selected trip before searching. A total trip budget is not used as a hotel nightly limit.",
                trip_id=body.trip_id,
                travel_request=existing.model_dump(mode="json"),
                conversation_id=conversation_id,
            )
        try:
            saved_results = hotel_search(
                body.trip_id,
                HotelSearch(country_code="IN", guest_nationality="IN"),
            )
            offers = saved_results.get("offers", []) if isinstance(saved_results, dict) else []
            safe_offers = [
                {
                    key: offer.get(key)
                    for key in ("hotel_id", "name", "city", "address", "offer_id", "total_stay_price", "currency", "within_budget", "room_details")
                    if key in offer
                }
                for offer in offers[:10]
                if isinstance(offer, dict)
            ]
            summary = (
                f"Live provider hotel search returned {len(offers)} offers for "
                f"{existing.destination}, {existing.departure_date} to {existing.return_date}. "
                "Review/select an exact offer in Hotel Search to continue."
                if offers
                else "The live provider hotel search returned no offers for this trip. If the destination is a broad region, open Hotel Search, choose a specific city or area suggestion, and search there. No booking was made."
            )
            return ChatResponse(
                reply=summary,
                trip_id=body.trip_id,
                travel_request=existing.model_dump(mode="json"),
                conversation_id=conversation_id,
                hotel_results=safe_offers,
            )
        except HTTPException as exc:
            detail = exc.detail
            message = detail.get("message") if isinstance(detail, dict) else detail
            return ChatResponse(
                reply=f"Hotel search did not complete: {message or 'provider access or trip requirements need attention.'}",
                trip_id=body.trip_id,
                travel_request=existing.model_dump(mode="json"),
                conversation_id=conversation_id,
            )
        except Exception as exc:
            logger.warning("Chat hotel search failed: exception=%s", type(exc).__name__)
            return ChatResponse(
                reply="Live hotel search failed. No booking was made; check the selected trip and provider access, then try again.",
                trip_id=body.trip_id,
                travel_request=existing.model_dump(mode="json"),
                conversation_id=conversation_id,
            )

    if explicit_saved_update:
        if existing is None:
            return ChatResponse(
                reply="Select the exact saved trip you want to update, then tell me which details to change. I haven’t modified any trip.",
                conversation_id=conversation_id,
            )
        try:
            updates, date_issue = extract_travel_updates_with_date_issue(safe_text)
            date_issue = _validate_draft_date_updates(existing, updates) or date_issue
            if date_issue:
                return ChatResponse(
                    reply=f"{date_issue} The selected saved trip was not changed.",
                    trip_id=body.trip_id,
                    travel_request=existing.model_dump(mode="json"),
                    conversation_id=conversation_id,
                )
            if not updates:
                raise ValueError("No explicit trip fields were supplied.")
            updated_request = merge_trip_state(existing, updates)
            update_trip(body.trip_id, updated_request)
        except Exception as exc:
            logger.warning("Saved trip update rejected: exception=%s", type(exc).__name__)
            return ChatResponse(
                reply="I couldn’t safely update the selected trip. Please state the exact fields to change; no trip was modified.",
                trip_id=body.trip_id,
                travel_request=existing.model_dump(mode="json"),
                conversation_id=conversation_id,
            )
        return ChatResponse(
            reply="Updated only the selected saved trip with the details you explicitly changed.",
            trip_id=body.trip_id,
            travel_request=updated_request.model_dump(mode="json"),
            conversation_id=conversation_id,
            draft_id="",
        )

    missing_fields = set(active_draft.get("missing_information", [])) if active_draft else set()
    pending_clarification = (
        _chat_pending_field(
            body.history,
            missing_fields,
            active_draft.get("pending_field"),
        )
        if active_draft else None
    )
    if pending_clarification and not _message_answers_pending(
        text,
        pending_clarification[0],
        draft_request or TravelRequest(),
    ):
        pending_clarification = None
    pending_draft_answer = active_draft and pending_clarification is not None
    pending_action = active_draft.get("pending_action") if active_draft else None
    resume_itinerary_requested = bool(
        active_draft
        and (
            _is_planning_request(text, body.draft_plan)
            or pending_action == "generate_itinerary"
        )
    )
    draft_followup_intent = bool(
        re.search(
            r"\b(origin|depart|destination|date|travel date|departure|return|night|day|adult|children|room|budget|prefer|preference|cheap|rail|train|one[- ]way|round[- ]trip|actually|instead|change|update|clear|accommodation|accomodation|accomadation|guesthouse|gueshouse|lodging|stay|sightseeing|experiences?|attractions?|interests?)\b|travel\s*mode",
            lowered,
        )
        or pending_draft_answer
        or resume_itinerary_requested
        or selected_duration_option is not None
    )
    if new_plan_requested or (active_draft and (draft_followup_intent or body.draft_plan)):
        base = TravelRequest() if new_plan_requested or draft_request is None else draft_request
        try:
            plan_destination = re.search(
                r"\b(?:to|visit)\s+([A-Za-z][A-Za-z .'-]*?)(?=\s+(?:with|for|from|on|and|date|budget|adults?|children|prefer|economy|business)\b|[?.!,]|$)",
                text,
                re.IGNORECASE,
            ) if resume_itinerary_requested else None
            has_other_planning_details = bool(re.search(
                r"\b(?:from|departure|return|date|origin|destination|budget|adult|child|night|prefer|economy|business)\b",
                lowered,
            ))
            if selected_duration_option is not None:
                selected_option = clarification_state[f"option{selected_duration_option}"]
                updates = dict(selected_option)
                date_issue = None
            elif (
                resume_itinerary_requested
                and not pending_draft_answer
                and plan_destination
                and not has_other_planning_details
            ):
                destination = plan_destination.group(1).strip()
                updates = (
                    {"destination": normalize_destination(destination)}
                    if not draft_request or destination.casefold() != (draft_request.destination or "").casefold()
                    else {}
                )
                date_issue = None
            elif resume_itinerary_requested and not pending_draft_answer and not re.search(
                r"\b(?:to|from|departure|return|date|origin|destination|budget|adult|child|night|prefer|economy|business)\b",
                lowered,
            ):
                updates, date_issue = {}, None
            elif pending_clarification and pending_clarification[0] in {"origin", "destination"} and len(text.split()) <= 4:
                place = re.sub(r"^(?:from|to|origin|destination)\s+", "", text.strip(), flags=re.IGNORECASE).strip(" .,!?")
                if place and pending_clarification[0] == "destination":
                    place = normalize_destination(place)
                updates, date_issue = ({pending_clarification[0]: place}, None) if place else ({}, None)
            else:
                updates, date_issue = extract_travel_updates_with_date_issue(
                    safe_text,
                    pending_field=pending_clarification[0] if pending_clarification else None,
                    context_date=(draft_request.departure_date or draft_request.return_date) if draft_request else None,
                )
        except Exception as exc:
            logger.warning("Planning detail extraction failed: exception=%s", type(exc).__name__)
            return ChatResponse(
                reply="I couldn’t reliably extract those details, so I haven’t changed the draft or saved a trip. Please rephrase the destination, dates, travelers, or budget.",
                draft_id=body.draft_id if active_draft else None,
                pending_field=active_draft.get("pending_field") if active_draft else None,
                conversation_id=conversation_id,
                travel_request=base.model_dump(mode="json"),
                has_planning_draft=bool(active_draft),
            )
        duration_conflict = _duration_conflict_options(base, updates)
        date_issue = _validate_draft_date_updates(base, updates) or date_issue
        if duration_conflict:
            date_issue = _duration_option_prompt(duration_conflict)
        if not updates and not active_draft and not date_issue:
            return ChatResponse(
                reply="Where would you like to go, and where will you depart from? I’ll start an unsaved planning draft.",
                conversation_id=conversation_id,
            )
        if (
            not updates
            and active_draft
            and not body.draft_plan
            and not resume_itinerary_requested
            and not date_issue
        ):
            try:
                reply = _safe_general_chat(text, body.history, draft_request)
            except Exception as exc:
                logger.warning("Chat LLM unavailable: exception=%s", type(exc).__name__)
                reply = "My conversational model is unavailable. The planning draft is unchanged."
            return ChatResponse(
                reply=reply,
                draft_id=body.draft_id,
                pending_field=active_draft.get("pending_field"),
                conversation_id=conversation_id,
                travel_request=draft_request.model_dump(mode="json") if draft_request else None,
                has_planning_draft=True,
            )
        try:
            current = merge_trip_state(base, updates) if updates else base
            missing = []
            if not current.origin:
                missing.append("origin")
            if not current.destination:
                missing.append("destination")
            if not current.departure_date:
                missing.append("departure_date")
            if current.flight_required and current.journey_type == "round_trip" and not current.return_date:
                missing.append("return_date")
            if current.hotel_required and not current.nights:
                missing.append("nights")
            if active_draft:
                next_pending_field = (
                    "resolve_duration_conflict"
                    if duration_conflict
                    else pending_clarification[0]
                    if date_issue and pending_clarification
                    else missing[0] if missing else None
                )
                draft = planning_draft_store.update_draft(
                    body.draft_id,
                    current,
                    missing=missing,
                    pending_field=next_pending_field,
                    pending_action=(
                        "generate_itinerary"
                        if resume_itinerary_requested or body.draft_plan
                        else active_draft.get("pending_action")
                    ),
                    clarification_state=(
                        duration_conflict
                        if duration_conflict
                        else None
                        if selected_duration_option is not None
                        else active_draft.get("clarification_state")
                    ),
                )
            else:
                user = _current_user.get()
                if user is None:
                    raise HTTPException(401, "Sign in to continue.")
                draft = planning_draft_store.create_draft(
                    conversation_id,
                    current,
                    owner_id=user["user_id"],
                    missing=missing,
                    pending_field=missing[0] if missing else None,
                    pending_action=(
                        "generate_itinerary"
                        if resume_itinerary_requested or body.draft_plan
                        else None
                    ),
                )
        except (OSError, ValueError, ValidationError) as exc:
            logger.warning("Planning draft could not be updated: exception=%s", type(exc).__name__)
            return ChatResponse(
                reply="I couldn’t update the planning draft because one or more details are invalid. It has not been saved as a trip.",
                draft_id=body.draft_id if active_draft else None,
                pending_field=active_draft.get("pending_field") if active_draft else None,
                conversation_id=conversation_id,
                travel_request=base.model_dump(mode="json"),
                has_planning_draft=bool(active_draft),
            )

        planning_brief = None
        if (body.draft_plan or resume_itinerary_requested) and not missing and not date_issue:
            try:
                from src.travel_agent.workflows.travel_planning import create_travel_planning_crew
                crew = create_travel_planning_crew(current, safe_text)
                result = crew.kickoff(inputs={
                    "travel_request": safe_text + "\nAI planning draft only. No provider search, booking, or payment was run.",
                    "validated_request": current.model_dump_json(indent=2),
                })
                planning_brief = str(result).strip() or None
                if planning_brief:
                    draft = planning_draft_store.update_draft(
                        draft["draft_id"],
                        current,
                        missing=missing,
                        pending_field=missing[0] if missing else None,
                        pending_action=None,
                        clarification_state=None,
                        itinerary=planning_brief,
                    )
            except Exception as exc:
                logger.warning("Itinerary generation failed: exception=%s", type(exc).__name__)

        update_confirmation = (
            _draft_update_summary(updates, current)
            if active_draft and updates
            else ""
        )
        route_note = (
            _mandarmoni_route_note(current)
            if active_draft and updates
            else ""
        )
        if date_issue:
            context = _saved_trip_summary(current)
            if duration_conflict:
                reply = (
                    f"{date_issue} The unsaved draft remains unchanged: {context}."
                    if context else date_issue
                )
            elif "would you like to change" in date_issue.lower():
                reply = (
                    f"{date_issue} Your unsaved draft still includes {context}."
                    if context else date_issue
                )
            else:
                explanation = re.sub(
                    r"\s*Please (?:provide|include)\b.*$",
                    "",
                    date_issue,
                    flags=re.IGNORECASE,
                ).rstrip()
                if pending_clarification and pending_clarification[0] == "return_date":
                    follow_up = " Please provide a valid return date, including its month and year."
                elif pending_clarification and pending_clarification[0] == "departure_date":
                    follow_up = " Please provide a valid departure date, including its month and year."
                else:
                    follow_up = " Please provide valid departure and return dates."
                reply = (
                    f"{explanation} Your unsaved draft still includes {context}.{follow_up}"
                    if context else
                    f"{explanation}{follow_up}"
                )
            if update_confirmation:
                reply = f"{update_confirmation} {reply}"
        elif planning_brief:
            reply = "Here is an AI-generated itinerary draft. It is not live availability, a provider quote, or a booking confirmation."
        elif missing:
            question_by_field = {
                "origin": "Which city will you depart from?",
                "destination": "Which destination would you like to visit?",
                "departure_date": "What dates are you considering?",
                "return_date": "What is your return date?",
                "nights": "How many nights should I plan for?",
            }
            next_question = question_by_field[missing[0]]
            if "origin" in missing and "destination" in missing:
                next_question = "Where will you depart from, and which destination would you like to visit?"
            question = (
                f"I’m continuing your unsaved planning draft. {next_question}"
                if active_draft else
                f"I’ve started an unsaved planning draft. {next_question}"
            )
            reply = " ".join(part for part in (update_confirmation, route_note, question) if part)
        elif update_confirmation:
            reply = " ".join(
                part for part in (
                    update_confirmation,
                    route_note,
                )
                if part
            )
        else:
            reply = "I’ve updated the unsaved planning draft. You can ask for an AI itinerary, weather, hotel or flight search, or explicitly ask me to save it to My Trips."
        return ChatResponse(
            reply=reply,
            trip_id=None,
            trip_saved=False,
            travel_request=current.model_dump(mode="json"),
            missing=missing,
            planning_brief=planning_brief,
            draft_id=draft["draft_id"],
            pending_field=draft.get("pending_field"),
            conversation_id=conversation_id,
            has_planning_draft=True,
        )

    if re.search(r"\b(hotels?|stays?)\b", lowered) and re.search(r"\b(available|availability|options|find|search|show)\b", lowered):
        return ChatResponse(
            reply="Hotel search from chat is not yet connected to the live search workflow. Select a saved trip and use Hotel Search; I have not searched or changed any trip.",
            trip_id=body.trip_id,
            travel_request=existing.model_dump(mode="json") if existing else None,
            conversation_id=conversation_id,
        )

    if ("?" in text or not re.search(r"\b(trip|travel|budget|destination|flight|hotel)\b", lowered)):
        try:
            reply = _safe_general_chat(text, body.history, draft_request or existing)
        except Exception as exc:
            logger.warning("Chat LLM unavailable: exception=%s", type(exc).__name__)
            reply = "My conversational model is unavailable right now. I haven’t changed or saved any trip."
        return ChatResponse(
            reply=reply or "What would you like help with?",
            trip_id=body.trip_id,
            travel_request=context_request.model_dump(mode="json") if context_request else None,
            draft_id=body.draft_id if active_draft else None,
            pending_field=active_draft.get("pending_field") if active_draft else None,
            conversation_id=conversation_id,
            has_planning_draft=bool(active_draft),
        )

    return ChatResponse(
        reply="I haven’t changed any trip. To start a new plan, say “Plan a trip to …”. To change a saved trip, explicitly ask to update it and keep that trip selected.",
        trip_id=body.trip_id,
        travel_request=context_request.model_dump(mode="json") if context_request else None,
        draft_id=body.draft_id if active_draft else None,
        pending_field=active_draft.get("pending_field") if active_draft else None,
        conversation_id=conversation_id,
        has_planning_draft=bool(active_draft),
    )
