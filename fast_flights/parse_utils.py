"""Parsing helpers for HTML-scraped flight strings into typed values."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

from .schema import Price

# Currency symbol -> ISO 4217 code
_CURRENCY_SYMBOLS = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
}


def parse_price(raw: str) -> Price:
    """Parse a price string like '$399' or '399' into Price(amount, currency).

    Strips common symbols and infers currency when possible; otherwise defaults to USD.
    Empty or invalid amount yields Price(Decimal('0'), 'USD').
    """
    if not raw or not raw.strip():
        return Price(amount=Decimal("0"), currency="USD")
    s = raw.strip().replace(",", "")
    currency = "USD"
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in s:
            currency = code
            s = s.replace(symbol, "").strip()
            break
    # Remove any remaining non-digit/non-dot chars (e.g. other symbols)
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return Price(amount=Decimal("0"), currency=currency)
    try:
        amount = Decimal(s)
    except InvalidOperation:
        return Price(amount=Decimal("0"), currency=currency)
    return Price(amount=amount, currency=currency)


def parse_duration(raw: str) -> Tuple[timedelta, str]:
    """Parse duration string like '12 hr' or '1 hr 30 min' into (timedelta, display_string).

    On failure returns (timedelta(0), raw) so callers always get a valid timedelta.
    """
    display = raw.strip() if raw else ""
    if not display:
        return timedelta(0), display
    # Match "N hr" or "N hr M min" (optional "s" for hrs/mins)
    m = re.match(
        r"^(?P<h>\d+)\s*hrs?\s*(?:(?P<min>\d+)\s*mins?)?\s*$",
        display,
        re.IGNORECASE,
    )
    if m:
        h = int(m.group("h"))
        min_val = int(m.group("min") or 0)
        return timedelta(hours=h, minutes=min_val), display
    # Try "N min" only
    m = re.match(r"^(?P<min>\d+)\s*mins?\s*$", display, re.IGNORECASE)
    if m:
        min_val = int(m.group("min"))
        return timedelta(minutes=min_val), display
    return timedelta(0), display


def parse_delay(raw: Optional[str]) -> Tuple[Optional[timedelta], Optional[str]]:
    """Parse delay string like '15 min' into (timedelta, None).

    When unparseable returns (None, raw) so delay_display can keep the raw string.
    """
    if not raw or not raw.strip():
        return None, None
    s = raw.strip()
    m = re.match(r"^(?P<min>\d+)\s*mins?\s*$", s, re.IGNORECASE)
    if m:
        return timedelta(minutes=int(m.group("min"))), None
    m = re.match(r"^(?P<h>\d+)\s*hrs?\s*(?:(?P<min>\d+)\s*mins?)?\s*$", s, re.IGNORECASE)
    if m:
        h = int(m.group("h"))
        min_val = int(m.group("min") or 0)
        return timedelta(hours=h, minutes=min_val), None
    return None, s


def parse_departure_arrival(
    raw: str, first_leg_date: Optional[date]
) -> Tuple[Optional[datetime], str]:
    """Parse strings like '12:35 PM on Fri, May 1' into (datetime, display_string).

    Year is taken from first_leg_date when provided; otherwise returns (None, raw).
    On parse failure returns (None, raw).
    """
    display = " ".join(raw.split()) if raw else ""
    if not display or first_leg_date is None:
        return None, display
    # Format: "12:35 PM on Fri, May 1" or similar
    try:
        # Try "H:MM AM/PM on Day, Mon D"
        m = re.search(
            r"(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s+on\s+(?P<date>\w+,\s+\w+\s+\d{1,2})",
            display,
            re.IGNORECASE,
        )
        if not m:
            return None, display
        time_str = m.group("time").strip()
        date_str = m.group("date").strip()
        # Parse time (e.g. "12:35 PM")
        t = datetime.strptime(time_str, "%I:%M %p").time()
        # Parse month/day from "Fri, May 1" - we only need month and day; year from first_leg_date
        try:
            parsed = datetime.strptime(
                date_str + f" {first_leg_date.year}", "%a, %b %d %Y"
            )
        except ValueError:
            parsed = datetime.strptime(
                date_str + f" {first_leg_date.year}", "%A, %B %d %Y"
            )
        dt = datetime.combine(parsed.date(), t)
        return dt, display
    except (ValueError, AttributeError):
        return None, display
