import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from icalendar import Event

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("ADMIN_API_TOKEN", "test-admin-token")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/scheduler-test.sqlite")

from app import app, db, TimeSlot
from icloud_availability import ICloudUnavailable
import icloud_availability


def setup_function():
    app.config["TESTING"] = True
    with app.app_context():
        db.drop_all()
        db.create_all()


def teardown_function():
    with app.app_context():
        db.session.remove()
        db.drop_all()


def test_block_skips_icloud_busy_hour_and_existing_slot(monkeypatch):
    london = ZoneInfo("Europe/London")
    monkeypatch.setattr(
        "app.busy_intervals",
        lambda start, end: [(datetime(2099, 6, 1, 10, 30, tzinfo=london),
                             datetime(2099, 6, 1, 11, 30, tzinfo=london))],
    )
    with app.app_context():
        db.session.add(TimeSlot(
            start_time=datetime(2099, 6, 1, 12),
            end_time=datetime(2099, 6, 1, 13),
            is_available=False,
            name="Already booked",
            location="Room A",
        ))
        db.session.commit()

    response = app.test_client().post(
        "/api/admin/create_block",
        json={"start": "2099-06-01T09:00:00+01:00", "end": "2099-06-01T13:30:00+01:00", "location": "Room B"},
        headers={"Authorization": "Bearer test-admin-token"},
        base_url="https://localhost",
    )
    assert response.status_code == 200
    assert response.json["created"] == 1
    assert response.json["skipped_icloud"] == 2
    assert response.json["skipped_existing"] == 1
    assert response.json["ignored_minutes"] == 30
    with app.app_context():
        slots = TimeSlot.query.order_by(TimeSlot.start_time).all()
        assert [(s.start_time.hour, s.end_time.hour, s.location) for s in slots] == [
            (9, 10, "Room B"), (12, 13, "Room A")
        ]


def test_block_fails_closed_when_icloud_is_unavailable(monkeypatch):
    def unavailable(start, end):
        raise ICloudUnavailable("Could not read iCloud availability. No slots were created.")

    monkeypatch.setattr("app.busy_intervals", unavailable)
    response = app.test_client().post(
        "/api/admin/create_block",
        json={"start": "2099-06-01T09:00:00", "end": "2099-06-01T11:00:00", "location": "Room B"},
        headers={"Authorization": "Bearer test-admin-token"},
        base_url="https://localhost",
    )
    assert response.status_code == 503
    with app.app_context():
        assert TimeSlot.query.count() == 0


def test_block_requires_admin():
    response = app.test_client().post(
        "/api/admin/create_block",
        json={"start": "2099-06-01T09:00:00", "end": "2099-06-01T11:00:00", "location": "Room B"},
        base_url="https://localhost",
    )
    assert response.status_code == 401


def test_icloud_feed_is_admin_only_and_never_appears_on_public_timetable(monkeypatch):
    sample = {
        "title": "Private meeting", "start": "2099-06-01T09:00:00+01:00",
        "end": "2099-06-01T10:00:00+01:00", "allDay": False,
        "calendar": "Work", "location": "Office", "free": False,
    }
    monkeypatch.setattr("app.cached_calendar_events", lambda start, end: [sample])
    client = app.test_client()
    url = "/api/admin/icloud/events?start=2099-06-01&end=2099-06-08"
    assert client.get(url, base_url="https://localhost").status_code == 401
    response = client.get(
        url, headers={"Authorization": "Bearer test-admin-token"},
        base_url="https://localhost",
    )
    assert response.status_code == 200
    assert response.json == [sample]
    assert response.headers["Cache-Control"] == "private, no-store"
    public = client.get("/api/get_timeslots", base_url="https://localhost")
    assert public.json == []


def test_icloud_events_include_all_day_and_free_events_but_not_cancellations(monkeypatch):
    class FakeEvent:
        def __init__(self, component):
            self.component = component

        def get_icalendar_component(self):
            return self.component

    class FakeCalendar:
        def __init__(self, name, events):
            self.name = name
            self.events = events

        def get_display_name(self):
            return self.name

        def search(self, **kwargs):
            return [FakeEvent(event) for event in self.events]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def principal(self):
            return self

        def calendars(self):
            return calendars

    timed = Event()
    timed.add("summary", "Morning meeting")
    timed.add("dtstart", datetime(2099, 6, 1, 8, tzinfo=timezone.utc))
    timed.add("dtend", datetime(2099, 6, 1, 9, tzinfo=timezone.utc))
    all_day = Event()
    all_day.add("summary", "Away")
    all_day.add("dtstart", date(2099, 6, 2))
    all_day.add("dtend", date(2099, 6, 3))
    transparent = Event()
    transparent.add("summary", "Optional")
    transparent.add("dtstart", datetime(2099, 6, 1, 12, tzinfo=timezone.utc))
    transparent.add("dtend", datetime(2099, 6, 1, 13, tzinfo=timezone.utc))
    transparent.add("transp", "TRANSPARENT")
    cancelled = Event()
    cancelled.add("summary", "Cancelled")
    cancelled.add("dtstart", date(2099, 6, 4))
    cancelled.add("dtend", date(2099, 6, 5))
    cancelled.add("status", "CANCELLED")
    calendars = [
        FakeCalendar("Work", [timed, cancelled]),
        FakeCalendar("Benji work", [all_day]),
        FakeCalendar("Our leisure", [transparent]),
    ]
    monkeypatch.setattr(icloud_availability, "_client", FakeClient)
    start, end = datetime(2099, 6, 1), datetime(2099, 6, 8)
    events = icloud_availability.calendar_events(start, end)
    assert len(events) == 3
    assert events[0]["start"] == "2099-06-01T09:00:00+01:00"
    assert events[1]["allDay"] is True
    assert events[1]["end"] == "2099-06-03"
    assert events[2]["free"] is True
    assert len(icloud_availability.busy_intervals(start, end)) == 2


def test_admin_display_cache_checks_tokens_before_refetching(monkeypatch):
    search_count = [0]

    class FakeCalendar:
        def __init__(self, name):
            self.name = name
            self.token = "first"

        def get_display_name(self):
            return self.name

        def get_property(self, property):
            return self.token

        def search(self, **kwargs):
            search_count[0] += 1
            return []

    calendars = [FakeCalendar(name) for name in icloud_availability.CALENDAR_NAMES]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def principal(self):
            return self

        def calendars(self):
            return calendars

    monkeypatch.setattr(icloud_availability, "_client", FakeClient)
    icloud_availability._event_cache.clear()
    start, end = datetime(2099, 6, 1), datetime(2099, 6, 8)
    assert icloud_availability.cached_calendar_events(start, end) == []
    assert search_count[0] == 3
    assert icloud_availability.cached_calendar_events(start, end) == []
    assert search_count[0] == 3
    calendars[0].token = "changed"
    assert icloud_availability.cached_calendar_events(start, end) == []
    assert search_count[0] == 6
    icloud_availability._event_cache.clear()
