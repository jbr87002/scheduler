from pathlib import Path


def test_admin_page_saves_each_change_without_a_save_button():
    template = Path("templates/admin.html").read_text()
    assert "openBlockModal(info);" in template
    assert "const start = info.startStr;" in template
    assert "const end = info.endStr;" in template
    assert "'/api/admin/slots/block'" in template
    assert "eventDrop: saveMove" in template
    assert "set_timeslots" not in template
    assert "saveTimeSlots" not in template
    assert "'/api/admin/icloud/events'" not in Path("templates/index.html").read_text()
