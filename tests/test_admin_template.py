from pathlib import Path


def test_admin_drag_selection_uses_fullcalendar_wall_clock_strings():
    template = Path("templates/admin.html").read_text()
    assert "openBlockModal(info);" in template
    assert "const start = info.startStr;" in template
    assert "const end = info.endStr;" in template
    assert "'/api/admin/create_block'" in template
