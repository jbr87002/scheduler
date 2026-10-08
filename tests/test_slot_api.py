import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("ADMIN_API_TOKEN", "test-admin-token")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/scheduler-test.sqlite")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import text

from app import app, db, TimeSlot, upgrade_schema
import icloud_availability

ADMIN = {"Authorization": "Bearer test-admin-token"}
BASE = "https://localhost"


@pytest.fixture(autouse=True)
def clean_database(monkeypatch):
    monkeypatch.setattr("app.send_confirmation_email", lambda *args, **kwargs: True)
    monkeypatch.delenv("ICLOUD_APPLE_ID", raising=False)
    app.config["TESTING"] = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client():
    return app.test_client()


def _add(**fields):
    defaults = dict(is_available=True, location="Room A")
    slot = TimeSlot(**{**defaults, **fields})
    db.session.add(slot)
    db.session.commit()
    return slot.id, slot.version


def _get(slot_id):
    db.session.expire_all()
    return db.session.get(TimeSlot, slot_id)


def test_list_slots_filters_by_range_and_includes_version(client):
    _add(start_time=datetime(2099, 6, 1, 9), end_time=datetime(2099, 6, 1, 10))
    _add(start_time=datetime(2099, 6, 9, 9), end_time=datetime(2099, 6, 9, 10))
    response = client.get(
        "/api/admin/slots?start=2099-06-01T00:00:00%2B01:00&end=2099-06-08T00:00:00%2B01:00",
        headers=ADMIN, base_url=BASE,
    )
    assert response.status_code == 200
    assert [s["start_time"] for s in response.json] == ["2099-06-01T09:00:00"]
    assert response.json[0]["version"] == 1
    assert client.get("/api/admin/slots", base_url=BASE).status_code == 401


def test_patch_moves_slot_and_bumps_version(client):
    slot_id, version = _add(start_time=datetime(2099, 6, 1, 9), end_time=datetime(2099, 6, 1, 10))
    response = client.patch(
        f"/api/admin/slots/{slot_id}",
        json={"version": version, "start_time": "2099-06-01T16:30:00", "end_time": "2099-06-01T17:30:00"},
        headers=ADMIN, base_url=BASE,
    )
    assert response.status_code == 200
    assert response.json["slot"]["version"] == version + 1
    slot = _get(slot_id)
    assert (slot.start_time.hour, slot.start_time.minute) == (16, 30)


def test_patch_from_stale_page_cannot_undo_a_student_booking(client):
    """The old Save button's failure mode: admin page loaded, student books, admin edits."""
    slot_id, version_seen_by_admin = _add(start_time=datetime(2099, 6, 1, 9), end_time=datetime(2099, 6, 1, 10))
    booked = client.post("/api/signup", json={"id": slot_id, "name": "student", "repeat": False}, base_url=BASE)
    assert booked.status_code == 200

    response = client.patch(
        f"/api/admin/slots/{slot_id}",
        json={"version": version_seen_by_admin, "location": "Room B"},
        headers=ADMIN, base_url=BASE,
    )
    assert response.status_code == 409
    slot = _get(slot_id)
    assert (slot.is_available, slot.name, slot.location) == (False, "student", "Room A")


def test_patch_rejects_overlap_with_another_slot(client):
    slot_id, version = _add(start_time=datetime(2099, 6, 1, 9), end_time=datetime(2099, 6, 1, 10))
    _add(start_time=datetime(2099, 6, 1, 11), end_time=datetime(2099, 6, 1, 12), is_available=False, name="x")
    response = client.patch(
        f"/api/admin/slots/{slot_id}",
        json={"version": version, "start_time": "2099-06-01T10:30:00", "end_time": "2099-06-01T11:30:00"},
        headers=ADMIN, base_url=BASE,
    )
    assert response.status_code == 409
    assert _get(slot_id).start_time.hour == 9


def test_patch_rejects_move_into_calendar_event(client, monkeypatch):
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")
    monkeypatch.setenv("ICLOUD_APPLE_ID", "x")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "y")
    monkeypatch.setattr(
        "app.busy_intervals",
        lambda start, end: [(datetime(2099, 6, 1, 14, tzinfo=london), datetime(2099, 6, 1, 15, tzinfo=london))],
    )
    slot_id, version = _add(start_time=datetime(2099, 6, 1, 9), end_time=datetime(2099, 6, 1, 10))
    response = client.patch(
        f"/api/admin/slots/{slot_id}",
        json={"version": version, "start_time": "2099-06-01T14:00:00", "end_time": "2099-06-01T15:00:00"},
        headers=ADMIN, base_url=BASE,
    )
    assert response.status_code == 409


def test_repeat_signup_creates_series_and_following_scope_only_touches_later_slots(client, monkeypatch):
    monkeypatch.setenv("END_OF_TERM", "2099-06-22")
    first_id, _ = _add(start_time=datetime(2099, 6, 1, 9), end_time=datetime(2099, 6, 1, 10))
    assert client.post("/api/signup", json={"id": first_id, "name": "pair", "repeat": True}, base_url=BASE).status_code == 200

    series = TimeSlot.query.order_by(TimeSlot.start_time).all()
    assert len(series) == 4
    assert len({s.series_id for s in series}) == 1 and series[0].series_id

    second = series[1]
    response = client.patch(
        f"/api/admin/slots/{second.id}",
        json={"version": second.version, "location": "Room Z", "scope": "following"},
        headers=ADMIN, base_url=BASE,
    )
    assert response.status_code == 200
    assert response.json["updated"] == 3
    db.session.expire_all()
    assert [s.location for s in TimeSlot.query.order_by(TimeSlot.start_time)] == ["Room A", "Room Z", "Room Z", "Room Z"]

    third = TimeSlot.query.order_by(TimeSlot.start_time).all()[2]
    response = client.delete(
        f"/api/admin/slots/{third.id}?version={third.version}&scope=following",
        headers=ADMIN, base_url=BASE,
    )
    assert response.status_code == 200
    assert response.json["deleted"] == 2
    assert TimeSlot.query.count() == 2


def test_delete_requires_current_version(client):
    slot_id, version = _add(start_time=datetime(2099, 6, 1, 9), end_time=datetime(2099, 6, 1, 10))
    assert client.delete(f"/api/admin/slots/{slot_id}", headers=ADMIN, base_url=BASE).status_code == 400
    assert client.delete(f"/api/admin/slots/{slot_id}?version={version + 5}", headers=ADMIN, base_url=BASE).status_code == 409
    assert client.delete(f"/api/admin/slots/{slot_id}?version={version}", headers=ADMIN, base_url=BASE).status_code == 200
    assert client.delete(f"/api/admin/slots/{slot_id}?version={version}", headers=ADMIN, base_url=BASE).status_code == 404


def test_block_reports_which_hours_were_skipped(client, monkeypatch):
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")
    monkeypatch.setattr(
        "app.busy_intervals",
        lambda start, end: [(datetime(2099, 6, 1, 17, tzinfo=london), datetime(2099, 6, 1, 17, 15, tzinfo=london))],
    )
    response = client.post(
        "/api/admin/slots/block",
        json={"start": "2099-06-01T16:30:00", "end": "2099-06-01T18:30:00", "location": "Room B"},
        headers=ADMIN, base_url=BASE,
    )
    assert response.status_code == 200
    assert response.json["created"] == 1
    assert response.json["slots"][0]["start_time"] == "2099-06-01T17:30:00"
    assert response.json["skipped"] == [
        {"start_time": "2099-06-01T16:30:00", "end_time": "2099-06-01T17:30:00", "reason": "calendar"},
    ]


def test_upgrade_schema_adds_columns_and_groups_legacy_repeats():
    db.drop_all()
    with db.engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE time_slot (id INTEGER PRIMARY KEY, start_time DATETIME NOT NULL, "
            "end_time DATETIME NOT NULL, is_available BOOLEAN, name VARCHAR(100), "
            "location VARCHAR(200), is_repeated BOOLEAN)"
        ))
        rows = [
            (1, "2099-06-01 09:00:00", "2099-06-01 10:00:00", 0, "pair", 1),
            (2, "2099-06-08 09:00:00.000000", "2099-06-08 10:00:00.000000", 0, "pair", 1),
            (3, "2099-06-02 09:00:00.000000", "2099-06-02 10:00:00.000000", 0, "other", 1),
            (4, "2099-06-03 09:00:00.000000", "2099-06-03 10:00:00.000000", 1, None, 0),
        ]
        for row in rows:
            conn.execute(text(
                "INSERT INTO time_slot (id, start_time, end_time, is_available, name, location, is_repeated) "
                "VALUES (:id, :s, :e, :a, :n, 'Room A', :r)"
            ), dict(zip(["id", "s", "e", "a", "n", "r"], row)))

    upgrade_schema()
    upgrade_schema()  # idempotent

    slots = {s.id: s for s in TimeSlot.query.all()}
    assert slots[1].series_id and slots[1].series_id == slots[2].series_id
    assert slots[3].series_id and slots[3].series_id != slots[1].series_id
    assert slots[4].series_id is None
    assert all(s.version == 1 for s in slots.values())
    raw = db.session.execute(text("SELECT start_time FROM time_slot WHERE id = 1")).scalar()
    assert raw == "2099-06-01 09:00:00.000000"


def test_calendar_urls_are_remembered_between_checks(monkeypatch):
    discoveries = [0]

    class FakeCalendar:
        def __init__(self, name):
            self.name = name
            self.url = f"https://caldav.example/{name}"

        def get_display_name(self):
            return self.name

        def search(self, **kwargs):
            return []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def principal(self):
            discoveries[0] += 1
            return self

        def calendars(self):
            return [FakeCalendar(name) for name in icloud_availability.CALENDAR_NAMES]

        def calendar(self, url):
            return FakeCalendar(url.rsplit("/", 1)[1])

    monkeypatch.setattr(icloud_availability, "_client", FakeClient)
    icloud_availability.forget_calendars()
    start, end = datetime(2099, 6, 1), datetime(2099, 6, 2)
    icloud_availability.busy_intervals(start, end)
    icloud_availability.busy_intervals(start, end)
    icloud_availability.calendar_events(start, end)
    assert discoveries[0] == 1
    icloud_availability.forget_calendars()
