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

from app import app, db, TimeSlot


@pytest.fixture(autouse=True)
def clean_database(monkeypatch):
    monkeypatch.setattr("app.send_confirmation_email", lambda *args, **kwargs: True)
    app.config["TESTING"] = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


def _admin_headers():
    return {"Authorization": "Bearer test-admin-token"}


def test_repeating_signup_rejects_future_booked_conflict(monkeypatch):
    monkeypatch.setenv("END_OF_TERM", "2026-02-28")
    with app.app_context():
        db.session.add_all(
            [
                TimeSlot(
                    start_time=datetime(2026, 2, 2, 14),
                    end_time=datetime(2026, 2, 2, 15),
                    is_available=True,
                    location="A",
                ),
                TimeSlot(
                    start_time=datetime(2026, 2, 9, 14),
                    end_time=datetime(2026, 2, 9, 15),
                    is_available=False,
                    name="existing",
                    location="A",
                ),
            ]
        )
        db.session.commit()

    response = app.test_client().post(
        "/api/signup",
        json={"id": 1, "name": "new", "repeat": True},
        base_url="https://localhost",
    )

    assert response.status_code == 409
    assert response.json["success"] is False
    with app.app_context():
        slots = TimeSlot.query.order_by(TimeSlot.start_time, TimeSlot.id).all()
        assert [(s.is_available, s.name) for s in slots] == [
            (True, None),
            (False, "existing"),
        ]


def test_admin_save_does_not_delete_rows_omitted_from_stale_calendar_snapshot():
    with app.app_context():
        db.session.add_all(
            [
                TimeSlot(
                    start_time=datetime(2026, 2, 2, 14),
                    end_time=datetime(2026, 2, 2, 15),
                    is_available=True,
                    location="A",
                ),
                TimeSlot(
                    start_time=datetime(2026, 2, 3, 14),
                    end_time=datetime(2026, 2, 3, 15),
                    is_available=False,
                    name="late booking",
                    location="B",
                ),
            ]
        )
        db.session.commit()

    response = app.test_client().post(
        "/api/admin/set_timeslots",
        json=[
            {
                "id": "1",
                "start_time": "2026-02-02T14:00:00",
                "end_time": "2026-02-02T15:00:00",
                "is_available": True,
                "location": "A",
                "name": None,
            }
        ],
        headers=_admin_headers(),
        base_url="https://localhost",
    )

    assert response.status_code == 200
    with app.app_context():
        slots = TimeSlot.query.order_by(TimeSlot.id).all()
        assert [(s.id, s.name, s.location) for s in slots] == [
            (1, None, "A"),
            (2, "late booking", "B"),
        ]
