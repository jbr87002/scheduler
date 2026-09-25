import os
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("ADMIN_API_TOKEN", "test-admin-token")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/scheduler-test.sqlite")

from app import app, db, TimeSlot
from icloud_availability import ICloudUnavailable


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
