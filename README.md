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
- `POST /api/admin/set_timeslots`
- `DELETE /api/admin/delete_timeslot/<id>`
- `POST /api/admin/change_location/<id>`
- `POST /api/admin/book_supervision`

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
