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

from app import app, db, TimeSlot, WorkSubmission

ADMIN = {"Authorization": "Bearer test-admin-token"}
BASE = "https://localhost"


@pytest.fixture(autouse=True)
def clean_database():
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


def _booked(name, day=17, hour=12):
    slot = TimeSlot(
        start_time=datetime(2026, 10, day, hour), end_time=datetime(2026, 10, day, hour + 1),
        is_available=False, name=name, location="Clare",
    )
    db.session.add(slot)
    db.session.commit()
    return slot.id


def test_week_lists_each_student_in_booked_sessions_only(client):
    _booked("sk2476 ir387, fk420")
    db.session.add(TimeSlot(start_time=datetime(2026, 10, 17, 9), end_time=datetime(2026, 10, 17, 10),
                            is_available=True, location="CMI"))
    _booked("later", day=26)
    db.session.commit()
    response = client.get("/api/admin/work?start=2026-10-12&end=2026-10-19", headers=ADMIN, base_url=BASE)
    assert response.status_code == 200
    assert len(response.json) == 1
    assert [s["crsid"] for s in response.json[0]["students"]] == ["sk2476", "ir387", "fk420"]
    assert all(s["format"] is None for s in response.json[0]["students"])
    assert client.get("/api/admin/work?start=2026-10-12&end=2026-10-19", base_url=BASE).status_code == 401


def test_tick_change_and_clear_work(client):
    slot_id = _booked("sk2476 ir387")
    url = f"/api/admin/work/{slot_id}/ir387"

    first = client.put(url, json={"format": "paper"}, headers=ADMIN, base_url=BASE)
    assert first.status_code == 200
    assert first.json["student"]["format"] == "paper"
    received_at = first.json["student"]["received_at"]

    changed = client.put(url, json={"format": "online"}, headers=ADMIN, base_url=BASE)
    assert changed.json["student"] == {
        "crsid": "ir387", "format": "online", "received_at": received_at, "marked": False, "source": "manual",
    }

    week = client.get("/api/admin/work?start=2026-10-12&end=2026-10-19", headers=ADMIN, base_url=BASE).json
    assert {s["crsid"]: s["format"] for s in week[0]["students"]} == {"sk2476": None, "ir387": "online"}

    cleared = client.delete(url, headers=ADMIN, base_url=BASE)
    assert cleared.status_code == 200
    assert WorkSubmission.query.count() == 0


def test_rejects_bad_format_and_students_not_in_group(client):
    slot_id = _booked("sk2476 ir387")
    assert client.put(f"/api/admin/work/{slot_id}/ir387", json={"format": "email"},
                      headers=ADMIN, base_url=BASE).status_code == 400
    assert client.put(f"/api/admin/work/{slot_id}/zz999", json={"format": "paper"},
                      headers=ADMIN, base_url=BASE).status_code == 404
    assert client.put(f"/api/admin/work/9999/ir387", json={"format": "paper"},
                      headers=ADMIN, base_url=BASE).status_code == 404


def test_deleting_a_supervision_removes_its_work_records(client):
    slot_id = _booked("sk2476 ir387")
    client.put(f"/api/admin/work/{slot_id}/sk2476", json={"format": "paper"}, headers=ADMIN, base_url=BASE)
    version = db.session.get(TimeSlot, slot_id).version
    assert client.delete(f"/api/admin/slots/{slot_id}?version={version}", headers=ADMIN, base_url=BASE).status_code == 200
    assert WorkSubmission.query.count() == 0


def test_work_page_requires_login(client):
    response = client.get("/admin/work", base_url=BASE)
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


def test_marking_requires_received_work_and_keeps_format(client):
    slot_id = _booked("sk2476 ir387")
    url = f"/api/admin/work/{slot_id}/sk2476"
    assert client.put(url, json={"marked": True}, headers=ADMIN, base_url=BASE).status_code == 400
    client.put(url, json={"format": "paper"}, headers=ADMIN, base_url=BASE)
    response = client.put(url, json={"marked": True}, headers=ADMIN, base_url=BASE)
    assert response.status_code == 200
    assert (response.json["student"]["format"], response.json["student"]["marked"]) == ("paper", True)
    assert client.put(url, json={"marked": "yes"}, headers=ADMIN, base_url=BASE).status_code == 400


def test_names_and_notes_appear_in_week_and_can_be_cleared(client):
    slot_id = _booked("sk2476 ir387")
    assert client.put("/api/admin/students/sk2476", json={"name": " Sofia Koval "},
                      headers=ADMIN, base_url=BASE).json["name"] == "Sofia Koval"
    note_url = f"/api/admin/work/{slot_id}/ir387/note"
    assert client.put(note_url, json={"text": "Handing in late (ill)"}, headers=ADMIN, base_url=BASE).status_code == 200

    week = client.get("/api/admin/work?start=2026-10-12&end=2026-10-19", headers=ADMIN, base_url=BASE).json
    students = {s["crsid"]: s for s in week[0]["students"]}
    assert (students["sk2476"]["name"], students["sk2476"]["note"]) == ("Sofia Koval", "")
    assert (students["ir387"]["name"], students["ir387"]["note"]) == (None, "Handing in late (ill)")

    client.put(note_url, json={"text": "  "}, headers=ADMIN, base_url=BASE)
    client.put("/api/admin/students/sk2476", json={"name": ""}, headers=ADMIN, base_url=BASE)
    week = client.get("/api/admin/work?start=2026-10-12&end=2026-10-19", headers=ADMIN, base_url=BASE).json
    assert [(s["name"], s["note"]) for s in week[0]["students"]] == [(None, ""), (None, "")]


def test_notes_and_names_are_validated(client):
    slot_id = _booked("sk2476 ir387")
    assert client.put(f"/api/admin/work/{slot_id}/zz999/note", json={"text": "x"},
                      headers=ADMIN, base_url=BASE).status_code == 404
    assert client.put(f"/api/admin/work/{slot_id}/ir387/note", json={"text": 5},
                      headers=ADMIN, base_url=BASE).status_code == 400
    assert client.put("/api/admin/students/Not-A-CRSid", json={"name": "x"},
                      headers=ADMIN, base_url=BASE).status_code == 400
    assert client.put("/api/admin/students/ir387", json={"name": "<b>"},
                      headers=ADMIN, base_url=BASE).status_code == 400
    assert client.put("/api/admin/students/ir387", json={"name": "x"}, base_url=BASE).status_code == 401
