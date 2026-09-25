"""Read busy periods from the three iCloud calendars used for scheduling."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
import os


LONDON = ZoneInfo("Europe/London")
CALENDAR_NAMES = ("Work", "Benji work", "Our leisure")


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


def _event_interval(event):
    component = event.get_icalendar_component()
    if str(component.get("STATUS", "")).upper() == "CANCELLED":
        return None
    if str(component.get("TRANSP", "")).upper() == "TRANSPARENT":
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
                    interval = _event_interval(event)
                    if interval and interval[0] < window_end and interval[1] > window_start:
                        intervals.append(interval)
    except ICloudUnavailable:
        raise
    except Exception as exc:
        raise ICloudUnavailable("Could not read iCloud availability. No slots were created.") from exc
    return intervals


def overlaps(start, end, intervals):
    aware_start = start.replace(tzinfo=LONDON)
    aware_end = end.replace(tzinfo=LONDON)
    return any(aware_start < busy_end and aware_end > busy_start for busy_start, busy_end in intervals)
