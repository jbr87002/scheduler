from pathlib import Path


def test_admin_create_slot_uses_fullcalendar_wall_clock_strings():
    template = Path("templates/admin.html").read_text()
    assert "openSlotModal({ mode: 'create', start: info.startStr, end: info.endStr });" in template
    assert "openSlotModal({ mode: 'create', start: snappedStart, end: snappedEnd });" not in template
