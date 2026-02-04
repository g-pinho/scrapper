from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Literal, Optional


@dataclass
class Price:
    """Price with amount (major units, e.g. dollars) and currency code.

    Currency is inferred from common symbols when parsing (e.g. $ -> USD, € -> EUR);
    otherwise defaults to USD.
    """

    amount: Decimal
    currency: str


@dataclass
class Result:
    current_price: Literal["low", "typical", "high"]
    flights: List[Flight]


@dataclass
class Flight:
    is_best: bool
    name: str
    departure_datetime: Optional[datetime]
    departure_display: str
    arrival_datetime: Optional[datetime]
    arrival_display: str
    arrival_time_ahead: str
    duration: timedelta
    duration_display: str
    stops: int
    delay: Optional[timedelta]
    delay_display: Optional[str]
    price: Price
    flight_number: str
    segments: List[FlightSegment]


@dataclass
class FlightSegment:
    flight_number: str
    airline: str
    departure_airport: str
    departure_airport_name: str
    arrival_airport: str
    arrival_airport_name: str
    departure_datetime: Optional[datetime]
    departure_display: str
    arrival_datetime: Optional[datetime]
    arrival_display: str
    duration: timedelta
    duration_display: str
    aircraft: Optional[str]
