"""Read busy periods from the three iCloud calendars used for scheduling."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from collections import OrderedDict
import os
from threading import Lock
import time as clock


LONDON = ZoneInfo("Europe/London")
CALENDAR_NAMES = ("Work", "Benji work", "Our leisure")
_event_cache = OrderedDict()
_event_cache_lock = Lock()
_CACHE_MAX_AGE_SECONDS = 3600
_CACHE_FALLBACK_SECONDS = 300
_CACHE_MAX_RANGES = 12


class ICloudUnavailable(Exception):
    """The calendar connection cannot currently be trusted for scheduling."""


def configured():
    return bool(os.getenv("ICLOUD_APPLE_ID") and os.getenv("ICLOUD_APP_PASSWORD"))


def _client():
    if not configured():
        raise ICloudUnavailable("iCloud is not connected yet.")
    from caldav import DAVClient

    return DAVClient(
        url="https://caldav.icloud.com/",
        username=os.environ["ICLOUD_APPLE_ID"],
        password=os.environ["ICLOUD_APP_PASSWORD"],
        timeout=15,
    )


def _selected_calendars(client):
    calendars = client.principal().calendars()
    found = {}
    for calendar in calendars:
        name = str(calendar.get_display_name())
        if name in CALENDAR_NAMES:
            if name in found:
                raise ICloudUnavailable(f"More than one iCloud calendar is named {name}.")
            found[name] = calendar
    missing = [name for name in CALENDAR_NAMES if name not in found]
    if missing:
        raise ICloudUnavailable("Missing iCloud calendar(s): " + ", ".join(missing))
    return found


def check_connection():
    try:
        with _client() as client:
            _selected_calendars(client)
    except ICloudUnavailable:
        raise
    except Exception as exc:
        if type(exc).__name__ == "AuthorizationError":
            raise ICloudUnavailable(
                "Apple rejected the sign-in. Check that the email is your Apple Account "
                "and that you entered a current app-specific password."
            ) from exc
        raise ICloudUnavailable(
            f"Could not discover iCloud calendars ({type(exc).__name__})."
        ) from exc
    return CALENDAR_NAMES


def _local(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=LONDON)
        return value.astimezone(LONDON)
    if isinstance(value, date):
        return datetime.combine(value, time.min, LONDON)
    raise ValueError("Unsupported iCloud event date")


def _event_interval(component, include_transparent=False):
    if str(component.get("STATUS", "")).upper() == "CANCELLED":
        return None
    if not include_transparent and str(component.get("TRANSP", "")).upper() == "TRANSPARENT":
        return None
    start_field = component.get("DTSTART")
    if not start_field:
        return None
    start = _local(start_field.dt)
    end_field = component.get("DTEND")
    if end_field:
        end = _local(end_field.dt)
    elif component.get("DURATION"):
        end = start + component.get("DURATION").dt
    elif isinstance(start_field.dt, date) and not isinstance(start_field.dt, datetime):
        end = start + timedelta(days=1)
    else:
        end = start
    return (start, end) if end > start else None


def busy_intervals(start, end):
    """Return private busy intervals only; event names never leave this module."""
    window_start = start.replace(tzinfo=LONDON)
    window_end = end.replace(tzinfo=LONDON)
    intervals = []
    try:
        with _client() as client:
            for calendar in _selected_calendars(client).values():
                for event in calendar.search(
                    start=window_start, end=window_end, event=True, expand=True
                ):
                    interval = _event_interval(event.get_icalendar_component())
                    if interval and interval[0] < window_end and interval[1] > window_start:
                        intervals.append(interval)
    except ICloudUnavailable:
        raise
    except Exception as exc:
        raise ICloudUnavailable("Could not read iCloud availability. No slots were created.") from exc
    return intervals


def _calendar_events_from(calendars, window_start, window_end):
    result = []
    for calendar_name, calendar in calendars.items():
        for event in calendar.search(
            start=window_start, end=window_end, event=True, expand=True
        ):
            component = event.get_icalendar_component()
            interval = _event_interval(component, include_transparent=True)
            if not interval or interval[0] >= window_end or interval[1] <= window_start:
                continue
            all_day = isinstance(component["DTSTART"].dt, date) and not isinstance(
                component["DTSTART"].dt, datetime
            )
            start_value, end_value = interval
            result.append({
                "title": str(component.get("SUMMARY") or "(Untitled event)"),
                "start": start_value.date().isoformat() if all_day else start_value.isoformat(),
                "end": end_value.date().isoformat() if all_day else end_value.isoformat(),
                "allDay": all_day,
                "calendar": calendar_name,
                "location": str(component.get("LOCATION") or ""),
                "free": str(component.get("TRANSP", "")).upper() == "TRANSPARENT",
            })
    return result


def calendar_events(start, end):
    """Read the selected calendars' events afresh for the authenticated admin view."""
    window_start = start.replace(tzinfo=LONDON)
    window_end = end.replace(tzinfo=LONDON)
    try:
        with _client() as client:
            return _calendar_events_from(_selected_calendars(client), window_start, window_end)
    except ICloudUnavailable:
        raise
    except Exception as exc:
        raise ICloudUnavailable("Could not read iCloud events.") from exc


def _calendar_sync_tokens(calendars):
    """A cheap per-calendar change check; None means no usable tokens."""
    from caldav.elements import dav

    tokens = []
    for name in CALENDAR_NAMES:
        try:
            token = calendars[name].get_property(dav.SyncToken())
        except Exception:
            return None
        if not token:
            return None
        tokens.append((name, str(token)))
    return tuple(tokens)


def cached_calendar_events(start, end):
    """Reuse event data until a sync token changes or the cache ages out.

    The cache is process-local. Booking and block creation keep their live
    iCloud checks and never use this display cache.
    """
    window_start = start.replace(tzinfo=LONDON)
    window_end = end.replace(tzinfo=LONDON)
    key = (window_start.isoformat(), window_end.isoformat())
    with _event_cache_lock:
        try:
            with _client() as client:
                calendars = _selected_calendars(client)
                tokens = _calendar_sync_tokens(calendars)
                entry = _event_cache.get(key)
                age = clock.monotonic() - entry["fetched_at"] if entry else float("inf")
                if entry and (
                    (tokens is not None and tokens == entry["tokens"] and age < _CACHE_MAX_AGE_SECONDS)
                    or (tokens is None and entry["tokens"] is None and age < _CACHE_FALLBACK_SECONDS)
                ):
                    _event_cache.move_to_end(key)
                    return [dict(event) for event in entry["events"]]
                events = _calendar_events_from(calendars, window_start, window_end)
                _event_cache[key] = {
                    "tokens": tokens,
                    "fetched_at": clock.monotonic(),
                    "events": events,
                }
                _event_cache.move_to_end(key)
                while len(_event_cache) > _CACHE_MAX_RANGES:
                    _event_cache.popitem(last=False)
                return [dict(event) for event in events]
        except ICloudUnavailable:
            raise
        except Exception as exc:
            raise ICloudUnavailable("Could not read iCloud events.") from exc


def overlaps(start, end, intervals):
    aware_start = start.replace(tzinfo=LONDON)
    aware_end = end.replace(tzinfo=LONDON)
    return any(aware_start < busy_end and aware_end > busy_start for busy_start, busy_end in intervals)
