
import json
import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.travel_agent.llm.router import generate
from src.travel_agent.schemas.travel_request import TravelRequest


_DATE_TOKEN = re.compile(
    r"\b(?:"
    r"\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{4}"
    r"|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+\d{4}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|"
    r"apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[,]?\s+\d{4}"
    r")\b",
    re.IGNORECASE,
)
_MONTH_PATTERN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)"
)
_DATE_RANGE = re.compile(
    rf"\b(?:(?P<month1>{_MONTH_PATTERN})\s+)?"
    r"(?P<day1>\d{1,2})(?:st|nd|rd|th)?\s*"
    r"(?:to|through|until|[-–—])\s*"
    rf"(?:(?P<month2_before>{_MONTH_PATTERN})\s+)?"
    r"(?P<day2>\d{1,2})(?:st|nd|rd|th)?"
    rf"(?:\s+(?P<month2_after>{_MONTH_PATTERN}))?"
    r"(?:\s*,?\s*(?P<year>\d{4}))?\b",
    re.IGNORECASE,
)
_RELATIVE_DAY_RANGE = re.compile(
    r"\b(?P<day1>\d{1,2})(?:st|nd|rd|th)?\s*"
    r"(?:to|through|until|[-–—])\s*"
    r"(?P<day2>\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(?:of\s+)?(?P<period>this month|next month)\b",
    re.IGNORECASE,
)
_RELATIVE_DAY = re.compile(
    r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
    r"(?P<period>this month|next month)\b",
    re.IGNORECASE,
)
_MONTH_DAY_WITHOUT_YEAR = re.compile(
    rf"\b(?:(?P<month1>{_MONTH_PATTERN})\s+(?P<day1>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"|(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month2>{_MONTH_PATTERN}))\b",
    re.IGNORECASE,
)
_WEEKDAY = re.compile(
    r"\b(?:(?P<modifier>next|this)\s+)?"
    r"(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_PARTIAL_DAY = re.compile(r"^\s*(\d{1,2})(?:st|nd|rd|th)?[.!]?\s*$", re.IGNORECASE)


def local_today() -> date:
    configured_timezone = os.getenv("WAYFARER_TIMEZONE")
    if configured_timezone:
        return datetime.now(ZoneInfo(configured_timezone)).date()
    return datetime.now().astimezone().date()


def _month_year(reference: date, period: str) -> tuple[int, int]:
    if period.casefold() == "this month":
        return reference.year, reference.month
    if reference.month == 12:
        return reference.year + 1, 1
    return reference.year, reference.month + 1


def _date_from_day(year: int, month: int, day: str) -> date:
    return date(year, month, int(day))


def _date_candidates(token: str) -> list[date]:
    normalized = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", token.strip(), flags=re.IGNORECASE)
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", normalized):
        try:
            return [date.fromisoformat(normalized)]
        except ValueError:
            return []
    numeric = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", normalized)
    if numeric:
        first, second, year = map(int, numeric.groups())
        candidates: set[date] = set()
        if first > 12 and second <= 12:
            orders = ((year, second, first),)
        elif second > 12 and first <= 12:
            orders = ((year, first, second),)
        elif first == second:
            orders = ((year, first, second),)
        else:
            orders = ((year, second, first), (year, first, second))
        for parts in orders:
            try:
                candidates.add(date(*parts))
            except ValueError:
                continue
        return sorted(candidates)

    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                "%d %B %Y", "%d %b %Y"):
        try:
            return [datetime.strptime(normalized, fmt).date()]
        except ValueError:
            continue
    return []


def extract_date_updates(
    message: str,
    *,
    pending_field: str | None = None,
    context_date: date | None = None,
    reference_date: date | None = None,
) -> tuple[dict[str, str], str | None]:
    """Parse dates without guessing an ambiguous numeric locale."""
    today = reference_date or local_today()
    relative_range = _RELATIVE_DAY_RANGE.search(message)
    if relative_range:
        year, month = _month_year(today, relative_range.group("period"))
        try:
            departure = _date_from_day(year, month, relative_range.group("day1"))
            return_date = _date_from_day(year, month, relative_range.group("day2"))
        except ValueError:
            return {}, "One of those dates is not a valid calendar date. Please correct the date range."
        if return_date <= departure:
            problem = "earlier than" if return_date < departure else "the same as"
            return {}, f"Your return date is {problem} your departure date. Please provide a valid date range."
        return {
            "departure_date": departure.isoformat(),
            "return_date": return_date.isoformat(),
        }, None

    date_range = _DATE_RANGE.search(message)
    range_month2 = (
        date_range.group("month2_before") or date_range.group("month2_after")
        if date_range else None
    )
    if date_range and (date_range.group("month1") or range_month2 or not _DATE_TOKEN.search(message)):
        first_month_text = date_range.group("month1")
        second_month_text = range_month2
        month_text = first_month_text or second_month_text
        if not month_text:
            return {}, "Please include the month for the departure and return dates."
        shared_month = datetime.strptime(month_text[:3].title(), "%b").month
        first_month = datetime.strptime(first_month_text[:3].title(), "%b").month if first_month_text else shared_month
        second_month = datetime.strptime(second_month_text[:3].title(), "%b").month if second_month_text else shared_month
        year = int(date_range.group("year")) if date_range.group("year") else today.year
        try:
            departure = date(year, first_month, int(date_range.group("day1")))
            return_date = date(year, second_month, int(date_range.group("day2")))
        except ValueError:
            return {}, "One of those dates is not a valid calendar date. Please correct the date range."
        if return_date < departure:
            return {}, "Your return date is earlier than your departure date. Please provide a valid date range."
        if return_date == departure:
            return {}, "Your return date is the same as your departure date. Please provide a later return date."
        return {
            "departure_date": departure.isoformat(),
            "return_date": return_date.isoformat(),
        }, None

    tokens = list(_DATE_TOKEN.finditer(message))
    if not tokens:
        relative_day = _RELATIVE_DAY.search(message)
        if relative_day:
            year, month = _month_year(today, relative_day.group("period"))
            try:
                parsed = _date_from_day(year, month, relative_day.group("day"))
            except ValueError:
                return {}, "That day is not a valid calendar date for the requested month."
            field = pending_field if pending_field in {"departure_date", "return_date"} else "departure_date"
            return {field: parsed.isoformat()}, None

        weekday_match = _WEEKDAY.search(message)
        if weekday_match:
            weekdays = {
                "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6,
            }
            weekday = weekdays[weekday_match.group("weekday").casefold()]
            days_ahead = (weekday - today.weekday()) % 7
            if weekday_match.group("modifier") == "next" and days_ahead == 0:
                days_ahead = 7
            parsed = date.fromordinal(today.toordinal() + days_ahead)
            field = pending_field if pending_field in {"departure_date", "return_date"} else "departure_date"
            return {field: parsed.isoformat()}, None

        month_day = _MONTH_DAY_WITHOUT_YEAR.search(message)
        if month_day:
            month_text = month_day.group("month1") or month_day.group("month2")
            day_text = month_day.group("day1") or month_day.group("day2")
            month = datetime.strptime(month_text[:3].title(), "%b").month
            try:
                parsed = _date_from_day(today.year, month, day_text)
            except ValueError:
                return {}, "That is not a valid calendar date. Please correct the date."
            field = pending_field if pending_field in {"departure_date", "return_date"} else "departure_date"
            return {field: parsed.isoformat()}, None

        partial_day = _PARTIAL_DAY.fullmatch(message)
        if partial_day and pending_field in {"departure_date", "return_date"}:
            if context_date is None:
                return {}, "Please include the month and year with that day."
            try:
                parsed = context_date.replace(day=int(partial_day.group(1)))
            except ValueError:
                return {}, f"That day is not valid for {context_date.strftime('%B %Y')}. Please give a full date."
            return {pending_field: parsed.isoformat()}, None
        if pending_field in {"departure_date", "return_date"}:
            return {}, "I couldn’t identify a complete date in that answer. Please include the month and year."
        return {}, None
    if len(tokens) > 2:
        return {}, "I found more than two dates, so I can’t tell which are departure and return. Please provide one departure and one return date in YYYY-MM-DD format."

    candidates = [_date_candidates(match.group(0)) for match in tokens]
    if any(not values for values in candidates):
        return {}, "One of those dates is not a valid calendar date. Please provide both dates in YYYY-MM-DD format."

    if len(tokens) == 1:
        if len(candidates[0]) > 1:
            return {}, "That numeric date is ambiguous without a known date format. Please provide it in YYYY-MM-DD format."
        field = pending_field if pending_field in {"departure_date", "return_date"} else "departure_date"
        return {field: candidates[0][0].isoformat()}, None

    pairs = [(start, end) for start in candidates[0] for end in candidates[1]]
    if all(end < start for start, end in pairs):
        return {}, "Your return date is earlier than your departure date under the date formats I can interpret. Please provide a valid departure and return date in YYYY-MM-DD format."
    if all(end == start for start, end in pairs):
        return {}, "Your return date is the same as your departure date. Please provide a later return date in YYYY-MM-DD format."
    if len(pairs) > 1:
        return {}, "Those numeric dates are ambiguous without a known date format. Please provide the departure and return dates in YYYY-MM-DD format."
    departure, return_date = pairs[0]
    return {
        "departure_date": departure.isoformat(),
        "return_date": return_date.isoformat(),
    }, None


def _explicit_preference_updates(message: str) -> dict:
    """Extract common preference and budget statements without an LLM."""
    updates: dict = {}
    text = message.lower()

    currency_codes = r"(?:INR|USD|EUR|GBP|CAD|AUD|JPY|RS\.?)"
    amount_pattern = r"([\d][\d,]*(?:\.\d{1,2})?)"
    currency_label = re.search(
        rf"\bbudget\s+currency\s*[-:=]\s*({currency_codes}|₹)(?![A-Za-z])",
        text,
        re.IGNORECASE,
    )

    def labeled_budget(pattern: str) -> tuple[float, str | None] | None:
        match = re.search(
            rf"{pattern}\s*[-:=]\s*(?:(?P<currency>{currency_codes}|₹)\s*)?{amount_pattern}",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        currency = match.group("currency")
        if currency and (currency.upper().startswith("RS") or currency == "₹"):
            normalized_currency = "INR"
        else:
            normalized_currency = currency.upper() if currency else None
        return float(match.group(2).replace(",", "")), normalized_currency

    nightly_budget = labeled_budget(
        r"\b(?:hotel\s+budget(?:\s+per\s+night)?|nightly\s+hotel\s+budget)\b"
    )
    total_budget = labeled_budget(r"\b(?:total\s+trip\s+budget|trip\s+budget\s+total)\b")
    if nightly_budget:
        updates["hotel_budget_per_night"] = nightly_budget[0]
        updates["currency"] = nightly_budget[1] or "INR"
    if total_budget:
        updates["total_trip_budget"] = total_budget[0]
        updates["budget_currency"] = total_budget[1] or "INR"
    if currency_label:
        currency = currency_label.group(1).upper()
        currency = "INR" if currency.startswith("RS") or currency == "₹" else currency
        updates["currency"] = currency
        if total_budget or re.search(r"\btotal\s+trip\s+budget\b", text):
            updates["budget_currency"] = currency

    if not nightly_budget and not total_budget:
        budget_label = re.search(r"\bbudget\b", text)
        if budget_label:
            tail = text[budget_label.start():]
            amount_match = re.search(
                rf"(?:\b{currency_codes}\b|₹)\s*{amount_pattern}"
                rf"|{amount_pattern}\s*(?:\b{currency_codes}\b|₹)",
                tail,
                re.IGNORECASE,
            )
            if not amount_match:
                amount_match = re.search(amount_pattern, tail)
            if amount_match:
                amount_text = next(group for group in amount_match.groups() if group and re.fullmatch(amount_pattern, group))
                amount = float(amount_text.replace(",", ""))
                currency_match = re.search(
                    rf"\b(INR|USD|EUR|GBP|CAD|AUD|JPY)\b|\bRS\.?\b|₹",
                    text,
                    re.IGNORECASE,
                )
                currency = currency_match.group(1).upper() if currency_match and currency_match.group(1) else "INR"
                currency = "INR" if currency == "RS" else currency
                if re.search(r"\b(per night|nightly|each night|a night)\b", tail):
                    updates["hotel_budget_per_night"] = amount
                    updates["currency"] = currency
                else:
                    updates["total_trip_budget"] = amount
                    updates["budget_currency"] = currency

    duration = re.search(
        r"\bfor\s+(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+days?\b",
        text,
    )
    if duration:
        numbers = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }
        updates["trip_duration_days"] = numbers.get(
            duration.group(1), int(duration.group(1)) if duration.group(1).isdigit() else 0
        )

    transport_mode = re.search(
        r"\b(?:travel\s*mode|mode\s+of\s+travel|transport\s+mode)\s*"
        r"(?:(?:will\s+be|is|:|=|-)\s*)?(train|rail|flight|plane|bus|coach|car|road)\b",
        text,
        re.IGNORECASE,
    )
    if transport_mode:
        mode = transport_mode.group(1).casefold()
        updates["preferred_transport_mode"] = {
            "rail": "train",
            "plane": "flight",
            "coach": "bus",
            "road": "car",
        }.get(mode, mode)

    accommodation = re.search(
        r"\b(?:accommodation|accomodation|accomadation|accom[oa]dation|lodging|stay)\s*"
        r"(?:(?:will\s+be|is|:|=|-)\s*)?(?:a\s+|an\s+)?"
        r"(guest\s*house|gueshouse|homestay|hostel|hotel|resort|camping)\b",
        text,
        re.IGNORECASE,
    )
    if accommodation:
        accommodation_type = re.sub(r"\s+", "", accommodation.group(1).casefold())
        if accommodation_type == "gueshouse":
            accommodation_type = "guesthouse"
        updates["accommodation_type"] = accommodation_type

    all_key_experiences = bool(
        re.search(
            r"\b(?:all\s+(?:the\s+)?key\s+experiences?|"
            r"key\s+experiences?\s*(?:[-:=]\s*)?all)\b",
            text,
            re.IGNORECASE,
        )
    )
    if all_key_experiences:
        updates["interests"] = [
            "Srinagar and Dal Lake",
            "Gulmarg",
            "Pahalgam",
            "Sonamarg",
            "Local gardens and scenic viewpoints",
            "Suitable seasonal activities",
        ]

    hotel_preference = re.search(
        r"\bhotel\s+preferences?\s*[-:=]\s*([^.;\n]+)",
        message,
        re.IGNORECASE,
    )
    if hotel_preference and hotel_preference.group(1).strip():
        updates["hotel_preferences"] = [hotel_preference.group(1).strip(" \t,")]

    flight_preference = re.search(
        r"\bflight\s+preferences?\s*[-:=]\s*([^.;\n]+)",
        message,
        re.IGNORECASE,
    )
    if flight_preference:
        value = flight_preference.group(1).strip(" \t,")
        preferences: list[str] = []
        if re.search(r"\bpremium\s+economy\b", value, re.IGNORECASE):
            preferences.append("Premium economy cabin")
        elif re.search(r"\beconomy(?:\s+class)?\b", value, re.IGNORECASE):
            preferences.append("Economy class")
        if re.search(r"\bwindow(?:\s+seat)?\b", value, re.IGNORECASE):
            preferences.append("Window seat requested")
        if re.search(r"\baisle(?:\s+seat)?\b", value, re.IGNORECASE):
            preferences.append("Aisle seat requested")
        if not preferences and value:
            preferences.append(value)
        if preferences:
            updates["flight_preferences"] = preferences
    elif re.search(r"\beconomy(?:\s+class)?\b", text):
        updates["flight_preferences"] = ["Economy class"]

    transport_preference = re.search(
        r"\b(?:ground[- ]transport|transport)\s+preferences?\s*[-:=]\s*([^.;\n]+)",
        message,
        re.IGNORECASE,
    )
    if transport_preference and transport_preference.group(1).strip():
        updates["transport_preferences"] = [
            transport_preference.group(1).strip(" \t,")
        ]

    no_preference_pattern = r"\b(no (?:particular|special|specific) preference|any suitable option|anything suitable|no preference|preferences? (?:are )?flexible)\b"
    clauses = re.split(r"[.;\n]+", text)
    no_preference = bool(re.search(no_preference_pattern, text))
    mentions_hotel = bool(re.search(r"\b(hotel|accommodation|stay)\b", text))
    mentions_flight = bool(re.search(r"\b(flights?|airlines?|fares?|cabin)\b", text))
    hotel_no_preference = any(re.search(no_preference_pattern, clause) and re.search(r"\b(hotel|accommodation|stay)\b", clause) for clause in clauses)
    flight_no_preference = any(re.search(no_preference_pattern, clause) and re.search(r"\b(flights?|airlines?|fares?|cabin)\b", clause) for clause in clauses)
    if no_preference and (hotel_no_preference or (not mentions_flight and mentions_hotel) or not mentions_flight):
        updates["hotel_preferences"] = ["No specific preference"]
    elif "hotel_preferences" not in updates and re.search(r"\b(hotel|accommodation|stay) preferences?\b", text):
        value = re.sub(r"\b(my|hotel|accommodation|stay|preferences?|are|is|i|prefer|flexible)\b", " ", text)
        value = re.sub(r"\b(no particular|no special|no specific|any suitable option)\b", " ", value)
        value = re.sub(r"\b(?:budget|inr|rs)\b|₹|\d[\d,.]*", " ", value)
        items = [part.strip(" .,:;-") for part in re.split(r",|\band\b", value) if part.strip(" .,:;-")]
        if items:
            updates["hotel_preferences"] = items

    cheap_flight = bool(re.search(r"\b(cheap|cheapest|budget[- ]friendly|low[- ]cost|lower fares?)\b", text) and (mentions_flight or ("anything cheap" in text and not mentions_hotel)))
    if cheap_flight:
        updates["flight_preferences"] = ["Prioritize budget-friendly fares"]
    elif flight_no_preference or (no_preference and mentions_flight and not mentions_hotel):
        updates["flight_preferences"] = ["No specific preference"]
    if (
        not transport_mode
        and re.search(r"\b(train|trains|rail|railway)\b", text)
        and re.search(r"\b(prefer|travell?ing|travel|transport|take|by)\b", text)
    ):
        updates["transport_preferences"] = ["Train"]

    return updates


def normalize_destination(value: str) -> str:
    """Correct a small set of unambiguous spelling variants without geocoding."""
    match = re.fullmatch(r"\s*si(?:k|kk)im(?:\s*,?\s*india)?\s*", value, re.IGNORECASE)
    if not match:
        return value.strip()
    return "Sikkim, India" if re.search(r"\bindia\b", value, re.IGNORECASE) else "Sikkim"


def redact_personal_details(message: str) -> str:
    """Remove contact and payment details before travel text reaches an LLM."""
    dates: list[str] = []
    def protect_date(match: re.Match) -> str:
        dates.append(match.group(0))
        return f"TRAVELDATETOKEN{len(dates)}END"
    safe = _DATE_TOKEN.sub(protect_date, message)
    safe = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[redacted email]", safe, flags=re.IGNORECASE)
    safe = re.sub(r"\b(?:\+?\d[\d ()-]{7,}\d)\b", "[redacted phone]", safe)
    safe = re.sub(r"\b(?:\d[ -]*?){13,19}\b", "[redacted payment number]", safe)
    # Contact and billing clauses have no role in trip preference extraction.
    safe = re.sub(r"(?:[,;.]?\s*)(?:my\s+)?(?:contact|billing|payment|phone|telephone|email|card|credit card)(?:\s+(?:and|or)\s+(?:billing|payment|contact))?(?:\s+details?)?\s+(?:are|is|:|for)\s+[^.;\n]+", " ", safe, flags=re.IGNORECASE)
    for index, original in enumerate(dates):
        safe = safe.replace(f"TRAVELDATETOKEN{index + 1}END", original)
    return safe.strip()


def _validated_partial_updates(
    data: dict,
    explicit: dict,
    date_updates: dict[str, str],
    date_issue: str | None,
    message: str,
) -> tuple[dict, str | None]:
    values = dict(data)
    if "total_trip_budget" in explicit and "hotel_budget_per_night" not in explicit:
        values.pop("hotel_budget_per_night", None)
        values.pop("currency", None)
    values.pop("departure_date", None)
    values.pop("return_date", None)
    if explicit.get("trip_duration_days") and not re.search(r"\bnight", message, re.IGNORECASE):
        values.pop("nights", None)
    updates = {**values, **explicit, **date_updates}
    if isinstance(updates.get("destination"), str):
        updates["destination"] = normalize_destination(updates["destination"])
    if "departure_date" in updates and "return_date" in updates:
        departure = date.fromisoformat(updates["departure_date"])
        return_date = date.fromisoformat(updates["return_date"])
        duration_days = updates.get("trip_duration_days")
        nights = updates.get("nights")
        if duration_days and (return_date - departure).days + 1 != duration_days:
            date_issue = (
                f"That date range covers {(return_date - departure).days + 1} calendar days, "
                f"but you asked for {duration_days} days. Would you like to change the dates or the trip duration?"
            )
        if not date_issue and nights and (return_date - departure).days != nights:
            date_issue = (
                f"That date range is {(return_date - departure).days} nights, "
                f"but you asked for {nights} nights. Would you like to change the dates or the stay duration?"
            )
        if date_issue:
            updates.pop("departure_date", None)
            updates.pop("return_date", None)
    validated = TravelRequest.model_validate(updates)
    return (
        validated.model_dump(
            include=set(updates),
            exclude={"status"},
            mode="json",
            exclude_unset=True,
            exclude_none=True,
        ),
        date_issue,
    )


def extract_travel_updates_with_date_issue(
    message: str,
    *,
    pending_field: str | None = None,
    context_date: date | None = None,
    reference_date: date | None = None,
) -> tuple[dict, str | None]:
    """Extract only travel fields explicitly present in this message.

    Unlike ``extract_travel_request``, this returns a partial mapping and
    never materializes schema defaults. It is intended for merging a chat
    correction into an existing trip.
    """
    if not message.strip():
        raise ValueError("Travel update cannot be empty.")

    explicit = _explicit_preference_updates(message)
    date_updates, date_issue = extract_date_updates(
        message,
        pending_field=pending_field,
        context_date=context_date,
        reference_date=reference_date,
    )
    safe_message = redact_personal_details(message)
    # Preference-only and budget-only messages are deterministic. Avoid both
    # unnecessary LLM calls and model-generated defaults for these updates.
    other_trip_signal = re.search(r"\bfrom\s+[A-Za-z][A-Za-z .'-]+\s+to\s+[A-Za-z]|\b(departure|return|travel)\s+date\b|\b(adults?|children|passengers?|travelers?|room count)\b|\b(one[- ]way|round[- ]trip)\b|\b20\d{2}-\d{2}-\d{2}\b|\b(plan|planning|trip to|visit|go to|destination)\b|\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", safe_message, re.IGNORECASE)
    if explicit and not other_trip_signal:
        return _validated_partial_updates({}, explicit, date_updates, date_issue, message)
    if date_issue and not other_trip_signal:
        return _validated_partial_updates({}, explicit, date_updates, date_issue, message)
    date_only_text = _DATE_RANGE.sub(" ", message)
    date_only_text = _DATE_TOKEN.sub(" ", date_only_text)
    has_other_details = re.search(
        r"\b(from|origin|destination|adults?|children|people|travelers?|budget|"
        r"room|nights?|days?|economy|business|train|rail|prefer|preference)\b",
        date_only_text,
        re.IGNORECASE,
    )
    if date_updates and not has_other_details:
        return _validated_partial_updates({}, explicit, date_updates, date_issue, message)

    prompt = f"""
Extract only travel information the traveler explicitly states in this message.
Return one JSON object containing only explicitly stated fields from this list:
origin, destination, departure_date, return_date, journey_type, nights,
trip_duration_days,
adults, children, rooms, hotel_budget_per_night, total_trip_budget,
budget_currency, currency, hotel_required,
flight_required, hotel_preferences, flight_preferences, transport_preferences,
preferred_transport_mode, accommodation_type, interests.

Rules:
- Omit every field that the message does not explicitly provide or change.
- Do not include defaults, assumptions, current-trip values, or unrelated values.
- Do not infer that a trip should be saved from a greeting or general question.
- Dates must use YYYY-MM-DD. Return null only for a field explicitly being cleared.
- Use JSON numbers for counts and budgets, and arrays for preferences.
- A general or total-trip budget updates total_trip_budget and budget_currency.
- Only an explicitly stated hotel amount per night/nightly updates hotel_budget_per_night and currency.
- Never interpret a general budget as a nightly hotel budget.
- Flight preferences describe fare, airline, cabin, or timing priorities; never treat “anything cheap” as an airport, route, or numeric budget.
- `trip_duration_days` is inclusive calendar days; `nights` is hotel nights and is not interchangeable with days.
- “No preference”, “flexible”, or “anything suitable” is an explicit no-constraint preference, not a missing trip value.
- Train or rail preferences belong in transport_preferences.
- Extract explicit travel modes into preferred_transport_mode.
- Extract an explicitly requested accommodation type into accommodation_type.
- Treat requests for all key experiences as broad interests, not confirmed itinerary inclusions.
- Do not interpret contact, phone, email, billing, or payment data as trip fields.
- Return JSON only, with no markdown or explanation.

Traveler message:
{safe_message}
"""
    try:
        raw = generate(prompt).text.strip()
    except Exception:
        if explicit:
            return _validated_partial_updates({}, explicit, date_updates, date_issue, message)
        raise
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(line for line in lines if not line.strip().startswith("```"))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        if explicit:
            return _validated_partial_updates({}, explicit, date_updates, date_issue, message)
        raise ValueError("The assistant could not parse the trip details. Please rephrase them.") from exc
    if not isinstance(data, dict):
        raise ValueError("The extracted trip details must be a JSON object.")
    allowed = set(TravelRequest.model_fields) - {"status"}
    if set(data) - allowed:
        raise ValueError("The assistant returned unsupported trip fields.")
    return _validated_partial_updates(data, explicit, date_updates, date_issue, message)


def extract_travel_updates(message: str) -> dict:
    """Compatibility wrapper returning valid partial updates."""
    updates, _ = extract_travel_updates_with_date_issue(message)
    return updates


def extract_travel_request(message: str) -> TravelRequest:
    """Extract, normalize, and validate the initial travel request."""

    if not message.strip():
        raise ValueError("Travel request cannot be empty.")

    safe_message = redact_personal_details(message)

    prompt = f"""
Extract the traveler's trip information from the message.

Return ONLY one valid JSON object with these fields:
- origin
- destination
- departure_date
- return_date
- nights
- trip_duration_days
- adults
- children
- rooms
- hotel_budget_per_night
- total_trip_budget
- budget_currency
- currency
- hotel_required
- flight_required
- hotel_preferences
- flight_preferences
- transport_preferences
- preferred_transport_mode
- accommodation_type
- interests

Rules:
1. Use null for unknown scalar values.
2. Use [] for unknown preference lists.
3. Dates must use YYYY-MM-DD format.
4. Numbers must be JSON numbers, not strings.
5. Do not invent missing information.
6. Use INR when the currency is not specified and the budget is
   expressed in Indian rupees.
7. adults defaults to 1, children to 0, rooms to 1.
8. hotel_required and flight_required default to true.
9. Do not include a status field.
10. Do not include markdown or explanations.
11. hotel_budget_per_night means the maximum combined budget per
    night for ALL requested rooms together, not a per-room budget.
12. A general or total-trip budget belongs in total_trip_budget and
    budget_currency; only an explicit nightly hotel amount belongs in
    hotel_budget_per_night and currency.
13. Never infer a hotel nightly budget from a general trip budget.
14. `trip_duration_days` counts inclusive calendar days; `nights` is the number
    of hotel nights and must not be inferred as the same number.
15. Extract explicit travel and accommodation preferences when stated.
16. Broad sightseeing requests belong in interests and do not guarantee that every
    attraction is feasible or included.

Traveler's message:
{safe_message}
"""

    result = generate(prompt)
    raw = result.text.strip()

    # Remove optional Markdown JSON fences.
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "The LLM did not return valid JSON for the travel request."
        ) from exc

    if not isinstance(data, dict):
        raise ValueError("The extracted travel request must be a JSON object.")

    explicit = _explicit_preference_updates(message)
    date_updates, date_issue = extract_date_updates(message)
    if date_issue:
        raise ValueError(date_issue)
    data.update(explicit)
    data.update(date_updates)
    if isinstance(data.get("destination"), str):
        data["destination"] = normalize_destination(data["destination"])
    if explicit.get("total_trip_budget") and "hotel_budget_per_night" not in explicit:
        data.pop("hotel_budget_per_night", None)
        data.pop("currency", None)
    if explicit.get("trip_duration_days") and not re.search(r"\bnight", message, re.IGNORECASE):
        data.pop("nights", None)

    # Status is calculated by this function, not trusted from the LLM.
    data.pop("status", None)

    # Remove null values for fields that have schema defaults.
    # Pydantic will apply the defaults when these keys are absent.
    defaulted_fields = {
        "adults",
        "children",
        "rooms",
        "currency",
        "hotel_required",
        "flight_required",
        "hotel_preferences",
        "flight_preferences",
        "transport_preferences",
    }

    for field in defaulted_fields:
        if data.get(field) is None:
            data.pop(field, None)

    # If both dates are known but nights are missing, calculate the
    # number of nights from the dates.
    departure_value = data.get("departure_date")
    return_value = data.get("return_date")

    if (
        data.get("nights") is None
        and isinstance(departure_value, str)
        and isinstance(return_value, str)
    ):
        try:
            departure = date.fromisoformat(departure_value)
            return_date = date.fromisoformat(return_value)
            calculated_nights = (return_date - departure).days

            if calculated_nights > 0:
                data["nights"] = calculated_nights
        except ValueError:
            # Pydantic will report invalid date values below.
            pass

    # Validate all extracted fields against the Pydantic schema.
    request = TravelRequest.model_validate(data)

    # Determine readiness from validated trip data.
    required_missing = (
        not request.origin
        or not request.destination
        or not request.departure_date
    )

    if request.flight_required and not request.return_date:
        required_missing = True

    if (
        request.hotel_required
        and not request.nights
        and not (
            request.departure_date and request.return_date
        )
    ):
        required_missing = True

    request.status = (
        "incomplete" if required_missing else "ready_to_search"
    )

    return request
