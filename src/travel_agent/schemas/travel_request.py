
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TravelRequest(BaseModel):
    """Validated travel requirements extracted from a user's message."""

    model_config = ConfigDict(extra="forbid")

    origin: str | None = None
    destination: str | None = None

    departure_date: date | None = None
    return_date: date | None = None
    journey_type: Literal["one_way", "round_trip"] = "round_trip"
    nights: int | None = Field(default=None, ge=1, le=365)
    trip_duration_days: int | None = Field(default=None, ge=1, le=366)

    adults: int = Field(default=1, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    rooms: int = Field(default=1, ge=1, le=20)

    hotel_budget_per_night: float | None = Field(
        default=None,
        gt=0,
        description="Maximum hotel budget per night combined across all requested rooms.",
    )
    total_trip_budget: float | None = Field(
        default=None,
        gt=0,
        description="Total budget for the entire trip, not a nightly accommodation budget.",
    )
    budget_currency: str | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
    )
    currency: str = Field(default="INR", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")

    hotel_required: bool = True
    flight_required: bool = True

    hotel_preferences: list[str] = Field(default_factory=list)
    flight_preferences: list[str] = Field(default_factory=list)
    transport_preferences: list[str] = Field(default_factory=list)
    preferred_transport_mode: Literal["train", "flight", "bus", "car", "other"] | None = None
    accommodation_type: str | None = Field(default=None, max_length=80)
    interests: list[str] = Field(default_factory=list, max_length=30)

    status: Literal["incomplete", "ready_to_search"] = "incomplete"

    @model_validator(mode="after")
    def validate_dates(self):
        if (
            self.departure_date
            and self.return_date
            and self.return_date <= self.departure_date
        ):
            raise ValueError("Return date must be after departure date.")

        if self.departure_date and self.return_date and self.nights:
            actual_nights = (self.return_date - self.departure_date).days
            if actual_nights != self.nights:
                raise ValueError(
                    "The number of nights does not match the travel dates."
                )

        if self.departure_date and self.return_date and self.trip_duration_days:
            actual_days = (self.return_date - self.departure_date).days + 1
            if actual_days != self.trip_duration_days:
                raise ValueError(
                    "The number of trip days does not match the travel dates."
                )

        return self
