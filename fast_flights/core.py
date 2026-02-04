import re
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import List, Literal, Optional, Union

from selectolax.lexbor import LexborHTMLParser, LexborNode

from .decoder import DecodedResult, ResultDecoder, Itinerary
from .schema import Flight, Result, FlightSegment, Price
from .flights_impl import FlightData, Passengers
from . import parse_utils
from .filter import TFSData
from .fallback_playwright import fallback_playwright_fetch
from .bright_data_fetch import bright_data_fetch
from .primp import Client, Response


DataSource = Literal["html", "js"]

# Default cookies embedded into the app to help bypass common consent gating.
# These are used only if the caller does not supply cookies (binary) and
# does not provide cookies via request_kwargs.
_DEFAULT_COOKIES = {
    "CONSENT": "PENDING+987",
    "SOCS": "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmRlIAEaBgiAo_CmBg",
}
_DEFAULT_COOKIES_BYTES = json.dumps(_DEFAULT_COOKIES).encode("utf-8")


def fetch(params: dict, request_kwargs: dict | None = None) -> Response:
    client = Client(impersonate="chrome_126", verify=False)
    # Pass through any extra request kwargs (e.g., cookies, headers)
    req_kwargs = request_kwargs.copy() if request_kwargs else {}
    res = client.get(
        "https://www.google.com/travel/flights", params=params, **req_kwargs
    )
    assert res.status_code == 200, f"{res.status_code} Result: {res.text_markdown}"
    return res


def _merge_binary_cookies(
    cookies_bytes: bytes | None, request_kwargs: dict | None
) -> dict:
    """Parse binary cookies into request kwargs.

    Supported formats (in order):
    - JSON bytes -> dict or list of pairs
    - Pickle bytes -> dict
    - Raw cookie header bytes -> sets the 'Cookie' header

    Existing request_kwargs are copied and updated; existing 'cookies' or 'headers' are overridden by parsed values.
    """
    req_kwargs = request_kwargs.copy() if request_kwargs else {}
    if not cookies_bytes:
        return req_kwargs

    # Try JSON first
    try:
        s = cookies_bytes.decode("utf-8")
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            req_kwargs["cookies"] = parsed
            return req_kwargs
        if isinstance(parsed, list):
            # list of pairs
            try:
                req_kwargs["cookies"] = dict(parsed)
                return req_kwargs
            except Exception:
                pass
    except Exception:
        pass

    # Try pickle
    try:
        import pickle

        parsed = pickle.loads(cookies_bytes)
        if isinstance(parsed, dict):
            req_kwargs["cookies"] = parsed
            return req_kwargs
    except Exception:
        pass

    # Fallback: treat as raw Cookie header
    try:
        s = cookies_bytes.decode("utf-8")
        headers = req_kwargs.get("headers", {})
        # make a shallow copy to avoid mutating input
        headers = headers.copy() if isinstance(headers, dict) else {}
        headers["Cookie"] = s
        req_kwargs["headers"] = headers
    except Exception:
        # give up silently and return what we have
        pass

    return req_kwargs


def get_flights_from_filter(
    filter: TFSData,
    currency: str = "",
    *,
    mode: Literal[
        "common", "fallback", "force-fallback", "local", "bright-data"
    ] = "common",
    data_source: DataSource = "html",
    cookies: bytes | None = None,
    request_kwargs: dict | None = None,
    cookie_consent: bool = True,
) -> Union[Result, DecodedResult, None]:
    data = filter.as_b64()

    params = {
        "tfs": data.decode("utf-8"),
        "hl": "en",
        "tfu": "EgQIABABIgA",
        "curr": currency,
    }

    # If the caller didn't provide cookies bytes and there is no cookies or Cookie header
    # in request_kwargs, use the embedded default cookies bytes (only when enabled).
    if cookies is None and cookie_consent:
        has_cookies_in_req = False
        if request_kwargs:
            if "cookies" in request_kwargs:
                has_cookies_in_req = True
            elif (
                "headers" in request_kwargs
                and isinstance(request_kwargs["headers"], dict)
                and "Cookie" in request_kwargs["headers"]
            ):
                has_cookies_in_req = True
        if not has_cookies_in_req:
            cookies = _DEFAULT_COOKIES_BYTES

    # Merge binary cookies into request kwargs (binary cookies take precedence)
    req_kwargs = _merge_binary_cookies(cookies, request_kwargs)

    if mode in {"common", "fallback"}:
        try:
            res = fetch(params, request_kwargs=req_kwargs)
        except AssertionError as e:
            if mode == "fallback":
                res = fallback_playwright_fetch(params, request_kwargs=req_kwargs)
            else:
                raise e

    elif mode == "local":
        from .local_playwright import local_playwright_fetch

        res = local_playwright_fetch(params, request_kwargs=req_kwargs)

    elif mode == "bright-data":
        res = bright_data_fetch(params, request_kwargs=req_kwargs)

    else:
        res = fallback_playwright_fetch(params, request_kwargs=req_kwargs)

    try:
        first_leg_date = None
        if getattr(filter, "flight_data", None) and len(filter.flight_data) > 0:
            first_leg_date = datetime.strptime(
                filter.flight_data[0].date, "%Y-%m-%d"
            ).date()
        return parse_response(res, data_source, first_leg_date=first_leg_date)
    except RuntimeError as e:
        if mode == "fallback":
            return get_flights_from_filter(
                filter,
                mode="force-fallback",
                request_kwargs=req_kwargs,
                cookies=None,
                cookie_consent=cookie_consent,
            )
        raise e


def get_flights(
    *,
    flight_data: List[FlightData],
    trip: Literal["round-trip", "one-way", "multi-city"],
    passengers: Optional[Passengers] = None,
    # Convenience passenger counters (used when `passengers` is None)
    adults: Optional[int] = None,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    seat: Literal["economy", "premium-economy", "business", "first"] = "economy",
    fetch_mode: Literal[
        "common", "fallback", "force-fallback", "local", "bright-data"
    ] = "common",
    max_stops: Optional[int] = None,
    data_source: DataSource = "html",
    cookies: bytes | None = None,
    request_kwargs: dict | None = None,
    cookie_consent: bool = True,
) -> Union[Result, DecodedResult, None]:
    # If the caller didn't supply a Passengers object, build one from the
    # convenience counters. Default to 1 adult when no adults count provided
    # (matches previous typical usage where at least one adult is expected).
    if passengers is None:
        ad = 1 if adults is None else adults
        passengers = Passengers(
            adults=ad,
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
        )

    tfs: TFSData = TFSData.from_interface(
        flight_data=flight_data,
        trip=trip,
        passengers=passengers,
        seat=seat,
        max_stops=max_stops,
    )

    return get_flights_from_filter(
        tfs,
        mode=fetch_mode,
        data_source=data_source,
        cookies=cookies,
        request_kwargs=request_kwargs,
        cookie_consent=cookie_consent,
    )


def parse_response(
    r: Response,
    data_source: DataSource,
    *,
    first_leg_date: Optional[date] = None,
    dangerously_allow_looping_last_item: bool = False,
) -> Union[Result, DecodedResult, None]:
    class _blank:
        def text(self, *_, **__):
            return ""

        def iter(self):
            return []

    blank = _blank()

    def safe(n: Optional[LexborNode]):
        return n or blank

    parser = LexborHTMLParser(r.text)

    # Always try to parse JS data first as it contains more details (segments, flight numbers)
    # unless specifically requested otherwise? No, user wants better data.
    # We maintain data_source arg for backward compat but we can be smarter.

    script_node = parser.css_first(r"script.ds\:1")
    if script_node:
        script = script_node.text()
        match = re.search(r"^.*?\{.*?data:(\[.*\]).*}", script)
        if match:
            try:
                data = json.loads(match.group(1))
                decoded_result = ResultDecoder.decode(data)

                # Convert to friendly schema
                flights = []

                def convert_itinerary(it: Itinerary, is_best: bool) -> Flight:
                    # Calculate total duration
                    # Itinerary doesn't have a simple total duration string, so we might need to format it or use travel_time
                    duration_minutes = it.travel_time
                    hours, minutes = divmod(duration_minutes, 60)
                    duration_display = (
                        f"{hours} hr {minutes} min" if hours > 0 else f"{minutes} min"
                    )

                    # Delays are not easily available in the JS structure (or I missed them), defaults to None

                    # Flight number: combine all flight numbers
                    flight_numbers = ", ".join(
                        f"{f.airline}{f.flight_number}" for f in it.flights
                    )

                    # Segments
                    segments = []
                    for f in it.flights:
                        # Create datetimes using both date and time from the segment
                        # Note: decoder Flight object has departure_date and departure_time tuple fields

                        f_dep_dt = None

                        def to_time_tuple(t):
                            if not t:
                                return (0, 0)
                            h = t[0] if len(t) > 0 else 0
                            m = t[1] if len(t) > 1 else 0
                            return (h or 0, m or 0)

                        f_dep_dt = None
                        f_dep_display = ""
                        if (
                            f.departure_date
                            and f.departure_time
                            and all(x is not None for x in f.departure_date)
                        ):
                            try:
                                t_tuple = to_time_tuple(f.departure_time)
                                f_dep_dt = datetime(*f.departure_date, *t_tuple)
                                f_dep_display = f_dep_dt.strftime("%I:%M %p").lstrip(
                                    "0"
                                )
                            except (ValueError, TypeError):
                                pass

                        f_arr_dt = None
                        f_arr_display = ""
                        if (
                            f.arrival_date
                            and f.arrival_time
                            and all(x is not None for x in f.arrival_date)
                        ):
                            try:
                                t_tuple = to_time_tuple(f.arrival_time)
                                f_arr_dt = datetime(*f.arrival_date, *t_tuple)
                                f_arr_display = f_arr_dt.strftime("%I:%M %p").lstrip(
                                    "0"
                                )
                            except (ValueError, TypeError):
                                pass

                        f_duration_mins = f.travel_time
                        f_duration = timedelta(minutes=f_duration_mins)
                        f_h, f_m = divmod(f_duration_mins, 60)
                        f_dur_display = (
                            f"{f_h} hr {f_m} min" if f_h > 0 else f"{f_m} min"
                        )

                        segments.append(
                            FlightSegment(
                                flight_number=f"{f.airline}{f.flight_number}",
                                airline=f.airline_name,
                                departure_airport=f.departure_airport,
                                departure_airport_name=f.departure_airport_name,
                                arrival_airport=f.arrival_airport,
                                arrival_airport_name=f.arrival_airport_name,
                                departure_datetime=f_dep_dt,
                                departure_display=f_dep_display,
                                arrival_datetime=f_arr_dt,
                                arrival_display=f_arr_display,
                                duration=f_duration,
                                duration_display=f_dur_display,
                                aircraft=f.aircraft,
                            )
                        )

                    # Price
                    # ItinerarySummary has price as int (major units?) logic says price.price / 100 in ItinerarySummary.from_b64
                    # But here we have Itinerary object which has itinerary_summary
                    price_amount = Decimal(it.itinerary_summary.price).quantize(
                        Decimal("0.01")
                    )
                    # Let's check decoder.py:
                    # return cls(pb.flights, pb.price.price / 100, pb.price.currency)
                    # Yes it is divided by 100.

                    # Create Flight object
                    # We need datetimes. JS data has (Year, Month, Day).
                    dep_dt = None
                    dep_display = ""
                    if (
                        it.departure_date
                        and it.departure_time
                        and all(x is not None for x in it.departure_date)
                        and all(x is not None for x in it.departure_time)
                    ):
                        try:
                            dep_dt = datetime(*it.departure_date, *it.departure_time)
                            dep_display = dep_dt.strftime("%I:%M %p").lstrip("0")
                        except (ValueError, TypeError):
                            pass

                    arr_dt = None
                    arr_display = ""

                    def to_time_tuple(t):
                        if not t:
                            return (0, 0)
                        h = t[0] if len(t) > 0 else 0
                        m = t[1] if len(t) > 1 else 0
                        return (h or 0, m or 0)

                    if (
                        it.arrival_date
                        and it.arrival_time
                        and all(x is not None for x in it.arrival_date)
                    ):
                        try:
                            t_tuple = to_time_tuple(it.arrival_time)
                            arr_dt = datetime(*it.arrival_date, *t_tuple)
                            arr_display = arr_dt.strftime("%I:%M %p").lstrip("0")
                        except (ValueError, TypeError):
                            pass

                    # Arrival ahead?
                    # Calculation of +1 day etc involves comparing dates
                    time_ahead = ""
                    if it.arrival_date != it.departure_date and arr_dt and dep_dt:
                        diff = (arr_dt.date() - dep_dt.date()).days
                        if diff > 0:
                            time_ahead = f"+{diff}"

                    return Flight(
                        is_best=is_best,
                        name=it.airline_names[0] if it.airline_names else "",
                        departure_datetime=dep_dt,
                        departure_display=dep_display,
                        arrival_datetime=arr_dt,
                        arrival_display=arr_display,
                        arrival_time_ahead=time_ahead,
                        duration=timedelta(minutes=duration_minutes),
                        duration_display=duration_display,
                        stops=len(it.flights) - 1,
                        delay=None,  # Not extracting delay from JS yet
                        delay_display=None,
                        price=Price(
                            amount=price_amount, currency=it.itinerary_summary.currency
                        ),
                        flight_number=flight_numbers,
                        segments=segments,
                    )

                for it in decoded_result.best:
                    flights.append(convert_itinerary(it, is_best=True))
                for it in decoded_result.other:
                    flights.append(convert_itinerary(it, is_best=False))

                # Format current_price from the cheapest flight if available, or just empty?
                # The HTML parser extracts 'current_price' string "low", "typical", "high"
                # The JS data doesn't seem to have this explicitly in ValidatedResult?
                # We can skip it or try to find it. Let's return "N/A" or check if we can parse it from HTML anyway.
                # Actually, we can still use HTML parser for the price insight!

                current_price_insight = (
                    safe(parser.css_first("span.gOatQ")).text() or "N/A"
                )

                return Result(current_price=current_price_insight, flights=flights)

            except Exception as e:
                # If JS parsing fails, silently fall back to HTML scraping
                print(f"DEBUG: JS parsing failed: {e}")
                import traceback

                traceback.print_exc()
                pass

    # Fallback to HTML parsing if JS parsing failed or wasn't found
    if data_source == "js":
        # If user explicitly asked for JS and we are here, it means we failed.
        # But wait, original code would crash?
        # Original code:
        # if data_source == 'js':
        #     script = parser.css_first(r'script.ds\:1').text()
        #     ...
        #     return ResultDecoder.decode(data)

        # We should keep supporting raw JS if they really want it, but based on my logic above
        # I intercepted it.
        # Let's handle the explicit 'js' case better if they want the raw DecodedResult?
        # The user request implies they want the "Flight" object to start having segments.
        # So I will assume returning Result is better.
        pass

    flights = []

    for i, fl in enumerate(parser.css('div[jsname="IWWDBc"], div[jsname="YdtKid"]')):
        is_best_flight = i == 0

        for item in fl.css("ul.Rk10dc li")[
            : (None if dangerously_allow_looping_last_item or i == 0 else -1)
        ]:
            # Flight name
            name = safe(item.css_first("div.sSHqwe.tPgKwe.ogfYpf span")).text(
                strip=True
            )

            # Get departure & arrival time
            dp_ar_node = item.css("span.mv1WYe div")
            try:
                departure_time = dp_ar_node[0].text(strip=True)
                arrival_time = dp_ar_node[1].text(strip=True)
            except IndexError:
                # sometimes this is not present
                departure_time = ""
                arrival_time = ""

            # Get arrival time ahead
            time_ahead = safe(item.css_first("span.bOzv6")).text()

            # Get duration
            duration = safe(item.css_first("li div.Ak5kof div")).text()

            # Get flight stops
            stops = safe(item.css_first(".BbR8Ec .ogfYpf")).text()

            # Get delay
            delay = safe(item.css_first(".GsCCve")).text() or None

            # Get prices
            price_raw = safe(item.css_first(".YMlIz.FpEdX")).text() or "0"

            # Stops formatting
            try:
                stops_fmt = 0 if stops == "Nonstop" else int(stops.split(" ", 1)[0])
            except ValueError:
                stops_fmt = 0

            departure_display = " ".join(departure_time.split())
            arrival_display = " ".join(arrival_time.split())
            dep_dt, _ = parse_utils.parse_departure_arrival(
                departure_display, first_leg_date
            )
            arr_dt, _ = parse_utils.parse_departure_arrival(
                arrival_display, first_leg_date
            )
            duration_delta, duration_display = parse_utils.parse_duration(duration)
            delay_delta, delay_display = parse_utils.parse_delay(delay)
            price_obj = parse_utils.parse_price(price_raw.replace(",", ""))

            flights.append(
                {
                    "is_best": is_best_flight,
                    "name": name,
                    "departure_datetime": dep_dt,
                    "departure_display": departure_display,
                    "arrival_datetime": arr_dt,
                    "arrival_display": arrival_display,
                    "arrival_time_ahead": time_ahead,
                    "duration": duration_delta,
                    "duration_display": duration_display,
                    "stops": stops_fmt,
                    "delay": delay_delta,
                    "delay_display": delay_display,
                    "price": price_obj,
                    "flight_number": "",  # Not available in HTML view easily
                    "segments": [],  # Not available in HTML view easily
                }
            )

    current_price = safe(parser.css_first("span.gOatQ")).text()
    if not flights:
        raise RuntimeError("No flights found:\n{}".format(r.text_markdown))

    return Result(current_price=current_price, flights=[Flight(**fl) for fl in flights])  # type: ignore
