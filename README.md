# Scheduler

Flask app for managing supervision time slots (available + booked) and exporting booked slots as an iCal feed.

## Environment

Required:
- `SECRET_KEY`
- `ADMIN_PASSWORD`

Optional:
- `DATABASE_URL` (defaults to `sqlite:///timeslots.db`)
- `ADMIN_API_TOKEN` (enables bearer-token auth for admin API endpoints)
- `END_OF_TERM` (used for repeating signups)
- `SUPERVISION_START_DATE` (used for initial calendar view)

## Running Locally

```bash
source .venv/bin/activate
python app.py
```

Or via gunicorn:

```bash
source .venv/bin/activate
gunicorn app:app
```

## API

Public:
- `GET /api/get_timeslots`
- `GET /api/export/<calendar_id>`
- `GET /api/health`

Admin (requires either admin session cookie OR `Authorization: Bearer $ADMIN_API_TOKEN`):
- `GET /api/admin/slots?start=&end=` — slots in a range, each with a `version`
- `POST /api/admin/slots/block` — add one-hour available slots across a block, skipping iCloud clashes (`/api/admin/create_block` still works)
- `PATCH /api/admin/slots/<id>` — `{version, start_time?, end_time?, location?, scope?}`
- `DELETE /api/admin/slots/<id>?version=N&scope=this|following`
- `POST /api/admin/book_supervision`
- Legacy, no version check: `DELETE /api/admin/delete_timeslot/<id>`, `POST /api/admin/change_location/<id>`

Every admin change is saved individually. `PATCH` and `DELETE` must send the
`version` the client last saw; if the slot has changed since (for example a
student booked it), the server returns `409` and writes nothing.
`scope=following` applies to this slot and later slots of the same repeating
booking (`series_id`). The old bulk `POST /api/admin/set_timeslots` returns `410`.

Only timed iCloud events that aren't marked free block new slots; all-day
events never do. Discovered calendar URLs are cached for 6 hours.

New columns (`series_id`, `version`) are added automatically at startup by
`upgrade_schema()`, which also groups existing repeating bookings into series.

### Book Supervision

`POST /api/admin/book_supervision`

```json
{
  "start_time": "2026-02-16T14:00:00+00:00",
  "end_time": "2026-02-16T15:00:00+00:00",
  "students": ["al2126", "hc612"],
  "location": "CMS, Room XYZ"
}
```

Notes:
- Datetimes are stored as naive values interpreted as `Europe/London` local time.
- If `location` is omitted, the server will try to infer it from overlapping available slots (otherwise it returns `400`).
- If the requested time overlaps any booked slot, the server returns `409`.
