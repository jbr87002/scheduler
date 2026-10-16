from flask import Flask, request, jsonify, render_template, send_file, redirect, url_for, session
from flask_talisman import Talisman
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import icalendar
import os
import io
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
from dateutil.relativedelta import relativedelta
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from zoneinfo import ZoneInfo
from sqlalchemy import or_, inspect as sa_inspect
from sqlalchemy.orm.exc import StaleDataError
import uuid
from flask import send_file, make_response
import logging
from logging.handlers import RotatingFileHandler
import sys
import hashlib
from flask import Response
import time
from sqlalchemy.sql import text
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import hmac
from dateutil.parser import isoparse
from icloud_availability import (
    CALENDAR_NAMES, ICloudUnavailable, busy_intervals, cached_calendar_events, check_connection,
    configured as icloud_configured, overlaps as overlaps_icloud,
)

load_dotenv()  # This line loads the variables from .env

london_tz = ZoneInfo("Europe/London")

_BOOKING_NAME_RE = re.compile(r"^[a-zA-Z0-9\s'()\[\],&-]+$")

def _get_bearer_token(auth_header):
    if not auth_header:
        return None
    # RFC 6750 bearer token, case-insensitive scheme.
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        return token or None
    return None

def _admin_api_token_valid(provided_token) -> bool:
    expected = os.getenv("ADMIN_API_TOKEN")
    if not expected or not provided_token:
        return False
    # Avoid timing leaks (not a big deal here, but essentially free).
    return hmac.compare_digest(provided_token, expected)

def _is_admin_authenticated() -> bool:
    # Browser-session admin login.
    if session.get("admin_logged_in"):
        return True
    # Machine-to-machine admin auth.
    provided = _get_bearer_token(request.headers.get("Authorization"))
    return _admin_api_token_valid(provided)

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not _is_admin_authenticated():
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function

def send_confirmation_email(student_name, slot_start, slot_end, location):
    """Send confirmation email for supervision signup"""
    sender = 'jbr46@cam.ac.uk'
    recipient = 'jbr46@cam.ac.uk'
    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = recipient
    msg['Subject'] = f'New supervision signup: {student_name}'
    body = f"""New supervision signup received:
Students: {student_name}
Time: {slot_start.strftime('%A, %d %B %Y %I:%M %p')} - {slot_end.strftime('%I:%M %p')}
Location: {location}
"""
    msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP('smtp.cam.ac.uk', 25) as server:
            server.send_message(msg)
            app.logger.info(f"Email sent to {recipient} for new signup")
            return True
    except Exception as e:
        app.logger.error(f"Error sending email: {str(e)}")
        return False

def _parse_local_datetime(value, field_name: str) -> datetime:
    if value is None:
        raise ValueError(f"Missing field '{field_name}'")
    if not isinstance(value, str):
        raise ValueError(f"Field '{field_name}' must be a string")

    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(
            f"Field '{field_name}' must include a time (e.g. 2026-02-16T14:00:00)"
        )

    try:
        dt = isoparse(value)
    except Exception as e:
        raise ValueError(f"Field '{field_name}' must be an ISO datetime") from e

    # DB stores naive datetimes interpreted as Europe/London local time.
    if dt.tzinfo is not None:
        dt = dt.astimezone(london_tz).replace(tzinfo=None)
    return dt

def _parse_range_param(value, field_name: str) -> datetime:
    """Accept a bare date (midnight) or a datetime, as FullCalendar sends either."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return datetime.fromisoformat(value.strip())
    return _parse_local_datetime(value, field_name)

def _normalize_booking_name(payload: dict) -> str:
    students = payload.get("students")
    name = payload.get("name")

    if students is not None:
        if not isinstance(students, list) or not all(isinstance(s, str) for s in students):
            raise ValueError("Field 'students' must be a list of strings")
        cleaned = [s.strip() for s in students if s.strip()]
        if not cleaned:
            raise ValueError("Field 'students' must not be empty")
        normalized = ", ".join(cleaned)
    elif name is not None:
        if not isinstance(name, str):
            raise ValueError("Field 'name' must be a string")
        normalized = name.strip()
        if not normalized:
            raise ValueError("Field 'name' must not be empty")
    else:
        raise ValueError("Missing field 'students' (list) or 'name' (string)")

    if len(normalized) > 100:
        raise ValueError("Booking name too long (max 100 chars)")
    if not _BOOKING_NAME_RE.match(normalized):
        raise ValueError("Booking name contains invalid characters")
    return normalized

def _normalize_location(payload: dict):
    location = payload.get("location")
    if location is None:
        return None
    if not isinstance(location, str):
        raise ValueError("Field 'location' must be a string")
    location = location.strip()
    if not location:
        raise ValueError("Field 'location' must not be empty")
    if len(location) > 200:
        raise ValueError("Location too long (max 200 chars)")
    # Basic XSS hardening for API callers. The web UI should be treated as the
    # canonical way to set rich locations if ever needed.
    if "<" in location or ">" in location:
        raise ValueError("Location must not contain '<' or '>'")
    return location

def _normalize_optional_slot_name(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Field 'name' must be a string")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 100:
        raise ValueError("Name too long (max 100 chars)")
    if not _BOOKING_NAME_RE.match(normalized):
        raise ValueError("Name contains invalid characters")
    return normalized

def _parse_end_of_term(value: str) -> datetime:
    if not value:
        raise ValueError("END_OF_TERM must be configured to use repeat signup")
    try:
        dt = isoparse(value.strip())
    except Exception as e:
        raise ValueError("END_OF_TERM must be an ISO date or datetime") from e
    if dt.tzinfo is not None:
        dt = dt.astimezone(london_tz).replace(tzinfo=None)
    return dt

def _repeated_occurrences(slot_start: datetime, slot_duration: timedelta, end_of_term: datetime):
    current_date = slot_start + timedelta(weeks=1)
    while current_date.date() <= end_of_term.date():
        repeated_start = current_date.replace(
            hour=slot_start.hour,
            minute=slot_start.minute,
            second=slot_start.second,
            microsecond=slot_start.microsecond,
        )
        yield repeated_start, repeated_start + slot_duration
        current_date += timedelta(weeks=1)

app = Flask(__name__)
Talisman(app, content_security_policy=None)
@app.before_request
def before_request():
    if not request.is_secure and not app.debug:
        url = request.url.replace('http://', 'https://', 1)
        code = 301
        return redirect(url, code=code)

app.secret_key = os.getenv('SECRET_KEY')

# Update database configuration
database_url = os.getenv('DATABASE_URL')
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///timeslots.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

class TimeSlot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    is_available = db.Column(db.Boolean, default=True)
    name = db.Column(db.String(100))
    location = db.Column(db.String(200))
    is_repeated = db.Column(db.Boolean, default=False)
    # Links the weekly copies of one repeating booking.
    series_id = db.Column(db.String(36), index=True)
    # Optimistic locking: SQLAlchemy bumps this on every ORM update and refuses
    # to write over a row someone else changed in the meantime.
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    __mapper_args__ = {"version_id_col": version}

    # Deleting a slot deletes its work records (the FK also cascades in SQL).
    submissions = db.relationship('WorkSubmission', cascade='all, delete-orphan')

    def to_json(self):
        return {
            'id': self.id,
            'start_time': self.start_time.isoformat(),
            'end_time': self.end_time.isoformat(),
            'is_available': self.is_available,
            'name': self.name,
            'location': self.location,
            'is_repeated': self.is_repeated,
            'series_id': self.series_id,
            'version': self.version,
        }

class WorkSubmission(db.Model):
    """One student's work for one booked supervision, ticked off on /admin/work."""
    __table_args__ = (db.UniqueConstraint('slot_id', 'crsid'),)
    id = db.Column(db.Integer, primary_key=True)
    slot_id = db.Column(
        db.Integer, db.ForeignKey('time_slot.id', ondelete='CASCADE'), nullable=False, index=True,
    )
    crsid = db.Column(db.String(20), nullable=False)
    format = db.Column(db.String(10), nullable=False)  # 'paper' or 'online'
    received_at = db.Column(db.DateTime, nullable=False)
    marked = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    source = db.Column(db.String(10), nullable=False, default='manual', server_default='manual')  # or 'email'

    def to_json(self):
        return {
            'crsid': self.crsid,
            'format': self.format,
            'received_at': self.received_at.isoformat(),
            'marked': self.marked,
            'source': self.source,
        }

class ProcessedEmail(db.Model):
    """Emails already handled by sync_work_email.py, so each is counted once
    (and a tick cleared by hand is not re-added on the next run)."""
    message_id = db.Column(db.String(255), primary_key=True)
    processed_at = db.Column(db.DateTime, nullable=False)
    outcome = db.Column(db.String(200), nullable=False)

class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    calendar_id = db.Column(db.String(36), unique=True, nullable=False)

    def __init__(self, username, password):
        self.username = username
        self.password_hash = generate_password_hash(password)
        self.calendar_id = str(uuid.uuid4())

def ensure_admin_account():
    admin = Admin.query.first()
    if admin:
        return admin

    admin_password = os.getenv('ADMIN_PASSWORD')
    if not admin_password:
        raise RuntimeError("ADMIN_PASSWORD must be set to initialize admin account")

    app.logger.warning("No admin user found, creating default admin")
    admin = Admin(username='admin', password=admin_password)
    db.session.add(admin)
    try:
        db.session.commit()
        return admin
    except IntegrityError:
        db.session.rollback()
        admin = Admin.query.first()
        if admin:
            return admin
        raise

@app.route('/')
def index():
    app.logger.info('Accessing index page')
    supervision_start_date = os.getenv('SUPERVISION_START_DATE')
    return render_template('index.html', supervision_start_date=supervision_start_date)

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json(silent=True)
    app.logger.info("Signup attempt received")

    if not isinstance(data, dict):
        return jsonify({'success': False, 'message': 'Request body must be a JSON object'}), 400

    # Input validation
    if not all(key in data for key in ['id', 'name', 'repeat']):
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400

    # Validate and sanitize name
    name = data.get('name', '').strip()
    if not name or len(name) > 100 or not re.match(r'^[a-zA-Z0-9\s\-\'()\[\]]+$', name):
        return jsonify({'success': False, 'message': 'Invalid name'}), 400

    # Validate id
    try:
        slot_id = int(data['id'])
        if slot_id <= 0:
            raise ValueError
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid slot ID'}), 400

    # Validate repeat
    repeat = data.get('repeat')
    if not isinstance(repeat, bool):
        return jsonify({'success': False, 'message': 'Invalid repeat value'}), 400

    try:
        end_of_term = _parse_end_of_term(os.getenv('END_OF_TERM')) if repeat else None
    except ValueError as e:
        app.logger.error(str(e))
        return jsonify({'success': False, 'message': str(e)}), 500

    timeslot = db.session.get(TimeSlot, slot_id)
    if not timeslot:
        app.logger.warning(f"Failed to book slot {slot_id}: Slot not available")
        return jsonify({'success': False, 'message': 'Time slot not available or invalid'}), 400

    slot_start = timeslot.start_time
    slot_end = timeslot.end_time
    slot_location = timeslot.location
    slot_duration = slot_end - slot_start

    if repeat and end_of_term.date() < slot_start.date():
        # END_OF_TERM left over from a previous term: refuse rather than
        # silently booking a single week.
        app.logger.error(f"END_OF_TERM ({end_of_term.date()}) is before slot {slot_id} ({slot_start.date()})")
        return jsonify({
            'success': False,
            'message': 'Repeat bookings are not set up for this term yet. Please book a one-off slot or email jbr46.',
        }), 500

    repeat_occurrences = (
        list(_repeated_occurrences(slot_start, slot_duration, end_of_term))
        if repeat else []
    )
    if icloud_configured():
        try:
            all_occurrences = [(slot_start, slot_end), *repeat_occurrences]
            busy = busy_intervals(slot_start, max(end for _, end in all_occurrences))
        except ICloudUnavailable:
            app.logger.warning('Booking paused because iCloud availability could not be checked')
            return jsonify({'success': False, 'message': 'Calendar availability could not be checked. Please try again later.'}), 503
        if any(overlaps_icloud(start, end, busy) for start, end in all_occurrences):
            return jsonify({'success': False, 'message': 'This time conflicts with a calendar event. Please choose another slot.'}), 409

    series_id = str(uuid.uuid4()) if repeat else None
    try:
        # Atomic transition: only one request can book an available slot.
        update_result = db.session.execute(
            text(
                "UPDATE time_slot "
                "SET is_available = :is_available, name = :name, is_repeated = :is_repeated, "
                "series_id = :series_id, version = version + 1 "
                "WHERE id = :id AND is_available = :expected_available"
            ),
            {
                "is_available": False,
                "name": name,
                "is_repeated": repeat,
                "series_id": series_id,
                "id": slot_id,
                "expected_available": True,
            }
        )
        if update_result.rowcount != 1:
            db.session.rollback()
            app.logger.warning(f"Failed to book slot {slot_id}: Slot already booked")
            return jsonify({'success': False, 'message': 'Time slot not available or invalid'}), 409

        if repeat_occurrences:
            conflict_filters = [
                (TimeSlot.start_time < repeated_end) & (TimeSlot.end_time > repeated_start)
                for repeated_start, repeated_end in repeat_occurrences
            ]
            repeated_conflicts = (
                TimeSlot.query.filter(TimeSlot.is_available == False)
                .filter(or_(*conflict_filters))
                .order_by(TimeSlot.start_time)
                .all()
            )
            if repeated_conflicts:
                db.session.rollback()
                return (
                    jsonify({
                        'success': False,
                        'message': 'Repeating signup conflicts with an existing booked slot',
                        'conflicts': [
                            {
                                'id': conflict.id,
                                'start_time': conflict.start_time.isoformat(),
                                'end_time': conflict.end_time.isoformat(),
                                'name': conflict.name,
                                'location': conflict.location,
                            }
                            for conflict in repeated_conflicts
                        ],
                    }),
                    409,
                )

        if repeat:
            app.logger.info(f"Creating repeated slots for {name}")
            for repeated_start, repeated_end in repeat_occurrences:
                db.session.add(TimeSlot(
                    start_time=repeated_start,
                    end_time=repeated_end,
                    is_available=False,
                    name=name,
                    location=slot_location,
                    is_repeated=True,
                    series_id=series_id,
                ))
                app.logger.debug(f"Created repeated slot: {repeated_start} - {repeated_end}")

        db.session.commit()
        app.logger.info(f"Successfully booked slot(s) for {name}")

        send_confirmation_email(
            student_name=name,
            slot_start=slot_start,
            slot_end=slot_end,
            location=slot_location
        )

        return jsonify({'success': True})
    except SQLAlchemyError as e:
        db.session.rollback()
        app.logger.error(f"Database error: {str(e)}")
        return jsonify({'success': False, 'message': 'An error occurred while booking the slot'}), 500

@app.route('/api/admin/icloud/status', methods=['GET'])
@admin_required
def icloud_status():
    if not icloud_configured():
        return jsonify({
            'ready': False,
            'calendars': CALENDAR_NAMES,
            'message': 'iCloud is not connected yet.',
        })
    try:
        check_connection()
        return jsonify({
            'ready': True,
            'calendars': CALENDAR_NAMES,
            'message': 'iCloud calendars connected.',
        })
    except ICloudUnavailable as exc:
        app.logger.warning('iCloud connection check failed: %s', type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__)
        return jsonify({'ready': False, 'calendars': CALENDAR_NAMES, 'message': str(exc)})


@app.route('/api/admin/icloud/events', methods=['GET'])
@admin_required
def icloud_events():
    try:
        start = _parse_range_param(request.args.get('start'), 'start')
        end = _parse_range_param(request.args.get('end'), 'end')
        if end <= start or end - start > timedelta(days=62):
            raise ValueError('Select a date range of up to 62 days.')
    except ValueError as exc:
        return jsonify({'message': str(exc)}), 400
    try:
        events = cached_calendar_events(start, end)
    except ICloudUnavailable as exc:
        app.logger.warning('iCloud events could not be read: %s', type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__)
        return jsonify({'message': str(exc)}), 503
    response = jsonify(events)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Vary'] = 'Cookie, Authorization'
    return response


STALE_MESSAGE = 'This slot changed since you loaded the page (perhaps a student just booked it). The calendar has been refreshed.'


def _error(message, status, **extra):
    return jsonify({'success': False, 'message': message, **extra}), status


def _following_in_series(slot):
    """This slot plus later slots of the same repeating booking."""
    if not slot.series_id:
        return [slot]
    # Compare in Python: older SQLite rows store datetimes in a different text
    # format, which breaks >= comparisons in SQL.
    series = TimeSlot.query.filter(TimeSlot.series_id == slot.series_id).all()
    return sorted(
        (s for s in series if s.start_time >= slot.start_time),
        key=lambda s: s.start_time,
    )


def _parse_scope(value):
    scope = value or 'this'
    if scope not in ('this', 'following'):
        raise ValueError("Field 'scope' must be 'this' or 'following'")
    return scope


def _parse_version(value):
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Field 'version' (integer) is required")
    return value


def _commit_or_error(log_message):
    try:
        db.session.commit()
    except StaleDataError:
        db.session.rollback()
        return _error(STALE_MESSAGE, 409)
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception(log_message)
        return _error('Could not save the change. Nothing was modified.', 500)
    return None


@app.route('/api/admin/slots', methods=['GET'])
@admin_required
def list_slots():
    query = TimeSlot.query
    try:
        if request.args.get('start'):
            query = query.filter(TimeSlot.end_time > _parse_range_param(request.args['start'], 'start'))
        if request.args.get('end'):
            query = query.filter(TimeSlot.start_time < _parse_range_param(request.args['end'], 'end'))
    except ValueError as exc:
        return _error(str(exc), 400)
    response = jsonify([slot.to_json() for slot in query.order_by(TimeSlot.start_time).all()])
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@app.route('/api/admin/slots/block', methods=['POST'])
@app.route('/api/admin/create_block', methods=['POST'])
@admin_required
def create_block():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error('Request body must be an object.', 400)
    try:
        start = _parse_local_datetime(payload.get('start'), 'start')
        end = _parse_local_datetime(payload.get('end'), 'end')
        location = _normalize_location(payload)
        if not location:
            raise ValueError('Location is required.')
        if start.date() != end.date() or end <= start:
            raise ValueError('Select a block within one day.')
        if start.minute % 30 or end.minute % 30 or start.second or end.second:
            raise ValueError('Block times must use half-hour increments.')
        count = int((end - start) // timedelta(hours=1))
        if count < 1 or count > 12:
            raise ValueError('Select between 1 and 12 hours.')
        if start < datetime.now(london_tz).replace(tzinfo=None):
            raise ValueError('Select a future time.')
    except ValueError as exc:
        return _error(str(exc), 400)

    try:
        busy = busy_intervals(start, end)
    except ICloudUnavailable as exc:
        app.logger.warning('iCloud availability check failed: %s', type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__)
        return _error(str(exc), 503)

    candidates = [(start + timedelta(hours=i), start + timedelta(hours=i + 1)) for i in range(count)]
    existing = TimeSlot.query.filter(
        TimeSlot.start_time < candidates[-1][1],
        TimeSlot.end_time > start,
    ).all()
    created = []
    skipped = []
    for slot_start, slot_end in candidates:
        if overlaps_icloud(slot_start, slot_end, busy):
            reason = 'calendar'
        elif any(slot_start < slot.end_time and slot_end > slot.start_time for slot in existing):
            reason = 'existing'
        else:
            new_slot = TimeSlot(
                start_time=slot_start, end_time=slot_end,
                is_available=True, location=location,
            )
            db.session.add(new_slot)
            created.append(new_slot)
            continue
        skipped.append({
            'start_time': slot_start.isoformat(),
            'end_time': slot_end.isoformat(),
            'reason': reason,
        })
    error = _commit_or_error('Could not create availability block')
    if error:
        return error
    app.logger.info(f"Created {len(created)} slot(s) from block {start}–{end}; skipped {len(skipped)}")
    return jsonify({
        'success': True,
        'created': len(created),
        'slots': [slot.to_json() for slot in created],
        'skipped': skipped,
        'skipped_icloud': sum(1 for s in skipped if s['reason'] == 'calendar'),
        'skipped_existing': sum(1 for s in skipped if s['reason'] == 'existing'),
        'ignored_minutes': int(((end - start) % timedelta(hours=1)).total_seconds() // 60),
    })


@app.route('/api/admin/slots/<int:slot_id>', methods=['PATCH'])
@admin_required
def update_slot(slot_id):
    """Move, resize or relocate one slot (or relocate the rest of its series).

    The client sends the version it last saw; a mismatch means the slot
    changed underneath it (e.g. a student booked it) and nothing is written.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error('Request body must be an object.', 400)
    slot = db.session.get(TimeSlot, slot_id)
    if not slot:
        return _error('This slot no longer exists. The calendar has been refreshed.', 404)
    try:
        version = _parse_version(payload.get('version'))
        scope = _parse_scope(payload.get('scope'))
        start = _parse_local_datetime(payload['start_time'], 'start_time') if 'start_time' in payload else slot.start_time
        end = _parse_local_datetime(payload['end_time'], 'end_time') if 'end_time' in payload else slot.end_time
        location = _normalize_location(payload) if 'location' in payload else slot.location
        if end <= start:
            raise ValueError('End time must be after start time.')
        if start.date() != end.date():
            raise ValueError('A slot must start and end on the same day.')
        times_changed = (start, end) != (slot.start_time, slot.end_time)
        if scope == 'following' and times_changed:
            raise ValueError('Times can only be changed one slot at a time.')
    except ValueError as exc:
        return _error(str(exc), 400)

    if version != slot.version:
        return _error(STALE_MESSAGE, 409, slot=slot.to_json())

    if times_changed:
        clash = TimeSlot.query.filter(
            TimeSlot.id != slot.id,
            TimeSlot.start_time < end,
            TimeSlot.end_time > start,
        ).first()
        if clash:
            return _error('That time overlaps another slot.', 409)
        if slot.is_available and icloud_configured():
            try:
                busy = busy_intervals(start, end)
            except ICloudUnavailable as exc:
                return _error(str(exc), 503)
            if overlaps_icloud(start, end, busy):
                return _error('That time clashes with an event in your calendar.', 409)

    targets = _following_in_series(slot) if scope == 'following' else [slot]
    slot.start_time = start
    slot.end_time = end
    for target in targets:
        target.location = location
    error = _commit_or_error(f'Could not update slot {slot_id}')
    if error:
        return error
    app.logger.info(f"Updated slot {slot_id} ({len(targets)} slot(s) affected)")
    return jsonify({'success': True, 'slot': slot.to_json(), 'updated': len(targets)})


@app.route('/api/admin/slots/<int:slot_id>', methods=['DELETE'])
@admin_required
def delete_slot(slot_id):
    slot = db.session.get(TimeSlot, slot_id)
    if not slot:
        return _error('This slot no longer exists. The calendar has been refreshed.', 404)
    try:
        version = _parse_version(request.args.get('version'))
        scope = _parse_scope(request.args.get('scope'))
    except ValueError as exc:
        return _error(str(exc), 400)
    if version != slot.version:
        return _error(STALE_MESSAGE, 409, slot=slot.to_json())

    targets = _following_in_series(slot) if scope == 'following' else [slot]
    for target in targets:
        db.session.delete(target)
    error = _commit_or_error(f'Could not delete slot {slot_id}')
    if error:
        return error
    app.logger.info(f"Deleted slot {slot_id} ({len(targets)} slot(s) removed)")
    return jsonify({'success': True, 'deleted': len(targets)})


@app.route('/api/admin/set_timeslots', methods=['POST'])
@admin_required
def set_timeslots():
    # Retired: saving the whole calendar from a browser snapshot could
    # overwrite bookings made after the page was loaded.
    return _error('This endpoint has been removed. Changes now save individually via /api/admin/slots.', 410)

@app.route('/api/get_timeslots', methods=['GET'])
def get_timeslots():
    app.logger.info("Fetching all timeslots")
    timeslots = TimeSlot.query.order_by(TimeSlot.start_time).all()
    app.logger.debug(f"Found {len(timeslots)} timeslots")
    return jsonify([{
        'id': slot.id,
        'start_time': slot.start_time.isoformat(),
        'end_time': slot.end_time.isoformat(),
        'is_available': slot.is_available,
        'name': slot.name,
        'location': slot.location,
        'is_repeated': slot.is_repeated
    } for slot in timeslots])

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"ok": True})

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        app.logger.info("Admin login attempt")
        password = request.form.get('password')
        try:
            admin = ensure_admin_account()
        except RuntimeError as e:
            app.logger.error(str(e))
            return render_template('admin_login.html', error='Server configuration error'), 500
        if admin and check_password_hash(admin.password_hash, password):
            session['admin_logged_in'] = True
            session['admin_username'] = admin.username
            app.logger.info(f"Admin login successful: {admin.username}")
            return redirect(url_for('admin'))
        else:
            app.logger.warning("Admin login failed: Invalid password")
            return render_template('admin_login.html', error='Invalid password')
    return render_template('admin_login.html')

@app.route('/admin')
def admin():
    if not session.get('admin_logged_in'):
        app.logger.warning("Unauthorized access attempt to admin page")
        return redirect(url_for('admin_login'))
    
    app.logger.info("Accessing admin page")
    try:
        admin = ensure_admin_account()
    except RuntimeError as e:
        app.logger.error(str(e))
        return "Server configuration error", 500
    
    supervision_start_date = os.getenv('SUPERVISION_START_DATE')
    calendar_url = url_for('export_calendar', calendar_id=admin.calendar_id, _external=True)
    webcal_url = calendar_url.replace('http://', 'webcal://').replace('https://', 'webcal://')
    return render_template('admin.html', supervision_start_date=supervision_start_date, calendar_url=webcal_url)

WORK_FORMATS = ('paper', 'online')


def _not_received(crsid):
    return {'crsid': crsid, 'format': None, 'received_at': None, 'marked': False, 'source': None}


def _group_crsids(name):
    """CRSids in a booking name, which students type as e.g. 'abc12, def34' or 'abc12 def34'."""
    return [crsid for crsid in re.split(r'[\s,]+', name or '') if crsid]


@app.route('/admin/work')
def admin_work():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    return render_template('admin_work.html')


@app.route('/api/admin/work', methods=['GET'])
@admin_required
def list_work():
    try:
        start = _parse_range_param(request.args.get('start'), 'start')
        end = _parse_range_param(request.args.get('end'), 'end')
        if end <= start or end - start > timedelta(days=62):
            raise ValueError('Select a date range of up to 62 days.')
    except ValueError as exc:
        return _error(str(exc), 400)
    slots = (
        TimeSlot.query.filter(
            TimeSlot.is_available == False,  # noqa: E712
            TimeSlot.start_time >= start,
            TimeSlot.start_time < end,
        )
        .order_by(TimeSlot.start_time)
        .all()
    )
    received = {
        (sub.slot_id, sub.crsid): sub
        for sub in WorkSubmission.query.filter(WorkSubmission.slot_id.in_([s.id for s in slots])).all()
    } if slots else {}
    response = jsonify([
        {
            'slot': slot.to_json(),
            'students': [
                received[(slot.id, crsid)].to_json() if (slot.id, crsid) in received
                else _not_received(crsid)
                for crsid in _group_crsids(slot.name)
            ],
        }
        for slot in slots
    ])
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@app.route('/api/admin/work/<int:slot_id>/<crsid>', methods=['PUT', 'DELETE'])
@admin_required
def set_work(slot_id, crsid):
    slot = db.session.get(TimeSlot, slot_id)
    if not slot or slot.is_available:
        return _error('This supervision no longer exists. The page has been refreshed.', 404)
    if crsid not in _group_crsids(slot.name):
        return _error(f'{crsid} is no longer in this group. The page has been refreshed.', 404)
    submission = WorkSubmission.query.filter_by(slot_id=slot_id, crsid=crsid).first()

    if request.method == 'DELETE':
        if submission:
            db.session.delete(submission)
        error = _commit_or_error(f'Could not clear work for {crsid} in slot {slot_id}')
        if error:
            return error
        return jsonify({'success': True, 'student': _not_received(crsid)})

    # PUT: set how the work arrived and/or whether it has been marked.
    payload = request.get_json(silent=True) or {}
    work_format = payload.get('format')
    marked = payload.get('marked')
    if work_format is not None and work_format not in WORK_FORMATS:
        return _error("Field 'format' must be 'paper' or 'online'.", 400)
    if marked is not None and not isinstance(marked, bool):
        return _error("Field 'marked' must be true or false.", 400)
    if work_format is None and marked is None:
        return _error("Send 'format' and/or 'marked'.", 400)
    if not submission and work_format is None:
        return _error('Record the work as received before marking it.', 400)

    def apply(target):
        if work_format is not None:
            target.format = work_format
        if marked is not None:
            target.marked = marked

    if submission:
        apply(submission)
    else:
        submission = WorkSubmission(
            slot_id=slot_id, crsid=crsid, format=work_format, marked=False, source='manual',
            received_at=datetime.now(london_tz).replace(tzinfo=None, microsecond=0),
        )
        apply(submission)
        db.session.add(submission)
    try:
        db.session.commit()
    except IntegrityError:
        # Ticked twice at once; the other request already recorded it.
        db.session.rollback()
        submission = WorkSubmission.query.filter_by(slot_id=slot_id, crsid=crsid).first()
        apply(submission)
        error = _commit_or_error(f'Could not record work for {crsid} in slot {slot_id}')
        if error:
            return error
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception(f'Could not record work for {crsid} in slot {slot_id}')
        return _error('Could not save the change. Nothing was modified.', 500)
    return jsonify({'success': True, 'student': submission.to_json()})


@app.route('/admin/logout')
def admin_logout():
    app.logger.info(f"Admin logout: {session.get('admin_username')}")
    session.pop('admin_logged_in', None)
    return redirect(url_for('index'))

@app.route('/api/admin/delete_timeslot/<int:id>', methods=['DELETE'])
@admin_required
def delete_timeslot(id):
    """Legacy API (no version check); the admin page uses DELETE /api/admin/slots/<id>."""
    slot = db.session.get(TimeSlot, id)
    if not slot:
        return _error('Time slot not found', 404)
    delete_subsequent = request.args.get('delete_subsequent', 'false').lower() == 'true'
    targets = _following_in_series(slot) if delete_subsequent else [slot]
    for target in targets:
        db.session.delete(target)
    error = _commit_or_error(f'Could not delete slot {id}')
    if error:
        return error
    app.logger.info(f"Deleted slot {id} ({len(targets)} slot(s) removed)")
    return jsonify({'success': True, 'deleted': len(targets)})

@app.route('/api/admin/change_location/<int:id>', methods=['POST'])
@admin_required
def change_location(id):
    """Legacy API (no version check); the admin page uses PATCH /api/admin/slots/<id>."""
    slot = db.session.get(TimeSlot, id)
    if not slot:
        return _error('Time slot not found', 404)
    data = request.get_json(silent=True) or {}
    try:
        new_location = _normalize_location({"location": data.get('location')})
    except ValueError as e:
        return _error(str(e), 400)
    if new_location is None:
        return _error('New location not provided', 400)
    update_subsequent = bool(data.get('update_subsequent', False))
    targets = _following_in_series(slot) if update_subsequent else [slot]
    for target in targets:
        target.location = new_location
    error = _commit_or_error(f'Could not update location for slot {id}')
    if error:
        return error
    app.logger.info(f"Updated location for slot {id} ({len(targets)} slot(s) affected)")
    return jsonify({'success': True, 'updated': len(targets)})

@app.route('/api/admin/book_supervision', methods=['POST'])
@admin_required
def book_supervision():
    """
    Create or update a booked supervision slot.

    Request JSON:
      - start_time: ISO datetime (required)
      - end_time: ISO datetime (required)
      - students: [str, ...] OR name: str (required)
      - location: str (optional; inferred from overlapping available slots if omitted)

    Semantics:
      - If an existing booked slot exactly matches (start_time, end_time), update its name/location.
      - If the requested time overlaps any other booked slot, return 409.
      - If it overlaps available slots, those are split/shrunk/deleted to avoid overlaps.
      - If it exactly matches an available slot, that slot is converted to booked (preserving ID/UID).
    """
    payload = request.get_json(silent=True) or {}
    try:
        start_time = _parse_local_datetime(payload.get("start_time"), "start_time")
        end_time = _parse_local_datetime(payload.get("end_time"), "end_time")
        if end_time <= start_time:
            return jsonify({"success": False, "message": "end_time must be after start_time"}), 400

        booking_name = _normalize_booking_name(payload)
        requested_location = _normalize_location(payload)  # may be None
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400

    try:
        overlapping = (
            TimeSlot.query.filter(TimeSlot.start_time < end_time, TimeSlot.end_time > start_time)
            .order_by(TimeSlot.start_time)
            .all()
        )

        # If there's an exact matching booked slot, treat this as an update.
        for slot in overlapping:
            if (
                slot.is_available is False
                and slot.start_time == start_time
                and slot.end_time == end_time
            ):
                slot.name = booking_name
                if requested_location is not None:
                    slot.location = requested_location
                db.session.commit()
                return jsonify(
                    {
                        "success": True,
                        "action": "updated",
                        "id": slot.id,
                        "start_time": slot.start_time.isoformat(),
                        "end_time": slot.end_time.isoformat(),
                        "name": slot.name,
                        "location": slot.location,
                    }
                )

        booked_conflicts = [s for s in overlapping if s.is_available is False]
        if booked_conflicts:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "Requested time overlaps an existing booked slot",
                        "conflicts": [
                            {
                                "id": s.id,
                                "start_time": s.start_time.isoformat(),
                                "end_time": s.end_time.isoformat(),
                                "name": s.name,
                                "location": s.location,
                            }
                            for s in booked_conflicts
                        ],
                    }
                ),
                409,
            )

        # Infer location if not provided.
        location = requested_location
        if location is None:
            locations = {s.location for s in overlapping if s.is_available and s.location}
            if len(locations) == 1:
                location = next(iter(locations))
            else:
                return (
                    jsonify(
                        {
                            "success": False,
                            "message": "Field 'location' is required (could not infer from existing slots)",
                        }
                    ),
                    400,
                )

        # Exact match available slot: convert it in place (keeps ID for iCal UID stability).
        for slot in overlapping:
            if (
                slot.is_available is True
                and slot.start_time == start_time
                and slot.end_time == end_time
            ):
                slot.is_available = False
                slot.name = booking_name
                slot.location = location
                slot.is_repeated = False
                db.session.commit()
                return jsonify(
                    {
                        "success": True,
                        "action": "booked_existing",
                        "id": slot.id,
                        "start_time": slot.start_time.isoformat(),
                        "end_time": slot.end_time.isoformat(),
                        "name": slot.name,
                        "location": slot.location,
                    }
                )

        # Otherwise: adjust any overlapping *available* slots to remove overlap, then insert the booked slot.
        for slot in overlapping:
            if not slot.is_available:
                continue

            # Fully covered: delete the slot.
            if slot.start_time >= start_time and slot.end_time <= end_time:
                db.session.delete(slot)
                continue

            # Overlap on the right: shrink end.
            if slot.start_time < start_time < slot.end_time <= end_time:
                slot.end_time = start_time
                slot.name = None
                slot.is_repeated = False
                continue

            # Overlap on the left: move start.
            if start_time <= slot.start_time < end_time < slot.end_time:
                slot.start_time = end_time
                slot.name = None
                slot.is_repeated = False
                continue

            # Target is strictly inside this available slot: split into left + right.
            if slot.start_time < start_time and slot.end_time > end_time:
                right = TimeSlot(
                    start_time=end_time,
                    end_time=slot.end_time,
                    is_available=True,
                    name=None,
                    location=slot.location,
                    is_repeated=False,
                )
                slot.end_time = start_time
                slot.name = None
                slot.is_repeated = False
                db.session.add(right)
                continue

        booked = TimeSlot(
            start_time=start_time,
            end_time=end_time,
            is_available=False,
            name=booking_name,
            location=location,
            is_repeated=False,
        )
        db.session.add(booked)
        db.session.commit()
        return jsonify(
            {
                "success": True,
                "action": "created",
                "id": booked.id,
                "start_time": booked.start_time.isoformat(),
                "end_time": booked.end_time.isoformat(),
                "name": booked.name,
                "location": booked.location,
            }
        )
    except SQLAlchemyError as e:
        db.session.rollback()
        app.logger.error(f"Database error booking supervision: {str(e)}")
        return jsonify({"success": False, "message": "Database error"}), 500

@app.route('/api/export/<calendar_id>')
def export_calendar(calendar_id):
    app.logger.info(f"Exporting calendar for calendar_id: {calendar_id}")
    admin = Admin.query.filter_by(calendar_id=calendar_id).first()
    if not admin:
        app.logger.warning(f"Calendar export failed: No admin found for calendar_id {calendar_id}")
        return "Calendar not found", 404

    cal = icalendar.Calendar()
    cal.add('prodid', '-//jbr46 Supervision Calendar//jbr46.user.srcf.net//')
    cal.add('version', '2.0')
    cal.add('name', 'jbr46 Supervision Calendar')
    cal.add('x-wr-calname', 'jbr46 Supervision Calendar')
    cal.add('x-wr-caldesc', 'Supervision Calendar for jbr46')
    cal.add('x-published-ttl', 'PT15M')  # Suggest updating every 15 minutes

    # Only get the booked (not available) timeslots
    timeslots = TimeSlot.query.filter_by(is_available=False).all()
    app.logger.debug(f"Found {len(timeslots)} booked timeslots for calendar export")
    for slot in timeslots:
        event = icalendar.Event()
        event.add('summary', slot.name)
        event.add('dtstart', slot.start_time.replace(tzinfo=london_tz))
        event.add('dtend', slot.end_time.replace(tzinfo=london_tz))
        event.add('location', slot.location)
        event['uid'] = f"{slot.id}@jbr46.user.srcf.net"  # Unique identifier for each event
        cal.add_component(event)
    
    ical_data = cal.to_ical()
    response = Response(ical_data)
    response.headers["Content-Type"] = "text/calendar; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=calendar.ics"
    
    # Add headers to encourage subscription and frequent updates
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-PUBLISHED-TTL"] = "PT15M"
    
    # Add ETag header for caching
    etag = hashlib.md5(ical_data).hexdigest()
    response.headers["ETag"] = etag
    
    app.logger.info("Calendar exported successfully")
    return response

@app.route('/questions-set')
def questions_set():
    return render_template('questions-set.html')

@app.route('/faq')
def faq():
    return render_template('faq.html')

def upgrade_schema():
    """Add columns introduced after the database was first created.

    Idempotent and safe on SQLite and PostgreSQL. Existing repeating bookings
    are grouped into series by the rule the old code used to find them
    (same student name, weekday, start and end time).
    """
    columns = {c['name'] for c in sa_inspect(db.engine).get_columns('time_slot')}
    with db.engine.begin() as conn:
        if 'series_id' not in columns:
            conn.execute(text("ALTER TABLE time_slot ADD COLUMN series_id VARCHAR(36)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_time_slot_series_id ON time_slot (series_id)"))
        if 'version' not in columns:
            conn.execute(text("ALTER TABLE time_slot ADD COLUMN version INTEGER NOT NULL DEFAULT 1"))

        inspector = sa_inspect(conn)
        work_columns = (
            {c['name'] for c in inspector.get_columns('work_submission')}
            if inspector.has_table('work_submission') else {'marked', 'source'}
        )
        if 'marked' not in work_columns:
            conn.execute(text("ALTER TABLE work_submission ADD COLUMN marked BOOLEAN NOT NULL DEFAULT FALSE"))
        if 'source' not in work_columns:
            conn.execute(text("ALTER TABLE work_submission ADD COLUMN source VARCHAR(10) NOT NULL DEFAULT 'manual'"))

        if db.engine.dialect.name == 'sqlite':
            # Rows written by raw SQL lack the microseconds SQLAlchemy writes,
            # and SQLite compares datetimes as text, so normalise them once.
            legacy = conn.execute(text(
                "SELECT id, start_time, end_time FROM time_slot "
                "WHERE length(start_time) != 26 OR length(end_time) != 26"
            )).all()
            for row in legacy:
                conn.execute(
                    text("UPDATE time_slot SET start_time = :s, end_time = :e WHERE id = :id"),
                    {
                        "s": isoparse(str(row.start_time)).strftime('%Y-%m-%d %H:%M:%S.%f'),
                        "e": isoparse(str(row.end_time)).strftime('%Y-%m-%d %H:%M:%S.%f'),
                        "id": row.id,
                    },
                )
            if legacy:
                app.logger.info(f"Normalised datetime format of {len(legacy)} slot(s)")

        rows = conn.execute(text(
            "SELECT id, start_time, end_time, name FROM time_slot "
            "WHERE is_repeated = :repeated AND series_id IS NULL ORDER BY start_time"
        ), {"repeated": True}).all()
        series = {}
        for row in rows:
            start = row.start_time if isinstance(row.start_time, datetime) else isoparse(str(row.start_time))
            end = row.end_time if isinstance(row.end_time, datetime) else isoparse(str(row.end_time))
            key = (row.name, start.weekday(), start.time(), end.time())
            series.setdefault(key, str(uuid.uuid4()))
            conn.execute(
                text("UPDATE time_slot SET series_id = :series_id WHERE id = :id"),
                {"series_id": series[key], "id": row.id},
            )
        if rows:
            app.logger.info(f"Grouped {len(rows)} repeating slots into {len(series)} series")

def init_db():
    with app.app_context():
        db.create_all()
        upgrade_schema()
        ensure_admin_account()

# Configure logging
def configure_logging():
    log_formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]')
    
    # Use an environment variable for the log file path, with a default for development
    log_file = os.getenv('FLASK_LOG_FILE', 'flask_app.log')
    
    file_handler = RotatingFileHandler(log_file, maxBytes=100240, backupCount=10)
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)
    
    # Also log to stdout
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(log_formatter)
    stdout_handler.setLevel(logging.INFO)
    
    app.logger.addHandler(file_handler)
    app.logger.addHandler(stdout_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info('Scheduler startup')

# Call this function right after creating the Flask app
configure_logging()
init_db()

# Call this function when you start your app
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5001, debug=True)
