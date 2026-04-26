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
from sqlalchemy import func, or_
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

    try:
        # Atomic transition: only one request can book an available slot.
        update_result = db.session.execute(
            text(
                "UPDATE time_slot "
                "SET is_available = :is_available, name = :name, is_repeated = :is_repeated "
                "WHERE id = :id AND is_available = :expected_available"
            ),
            {
                "is_available": False,
                "name": name,
                "is_repeated": repeat,
                "id": slot_id,
                "expected_available": True,
            }
        )
        if update_result.rowcount != 1:
            db.session.rollback()
            app.logger.warning(f"Failed to book slot {slot_id}: Slot already booked")
            return jsonify({'success': False, 'message': 'Time slot not available or invalid'}), 409

        repeat_occurrences = (
            list(_repeated_occurrences(slot_start, slot_duration, end_of_term))
            if repeat
            else []
        )
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
                db.session.execute(
                    text(
                        "INSERT INTO time_slot (start_time, end_time, is_available, name, location, is_repeated) "
                        "VALUES (:start_time, :end_time, :is_available, :name, :location, :is_repeated)"
                    ),
                    {
                        "start_time": repeated_start,
                        "end_time": repeated_end,
                        "is_available": False,
                        "name": name,
                        "location": slot_location,
                        "is_repeated": True,
                    }
                )
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

@app.route('/api/admin/set_timeslots', methods=['POST'])
@admin_required
def set_timeslots():
    slots = request.get_json(silent=True)
    if not isinstance(slots, list):
        return jsonify({
            "success": False,
            "message": "Request body must be a JSON array of slots"
        }), 400
    app.logger.info(f"Received {len(slots)} slots to set")

    try:
        # Start a transaction
        db.session.begin_nested()

        # Get all existing slots
        existing_slots = {str(slot.id): slot for slot in TimeSlot.query.all()}
        app.logger.debug(f"Found {len(existing_slots)} existing slots")

        # Keep track of processed slots
        processed_slots = set()

        # Update or create slots
        for slot in slots:
            if not isinstance(slot, dict):
                raise ValueError("Each slot must be an object")

            slot_id = slot.get('id')
            slot_id_str = str(slot_id) if slot_id is not None else None
            start_time = _parse_local_datetime(slot.get('start_time'), 'start_time')
            end_time = _parse_local_datetime(slot.get('end_time'), 'end_time')
            if end_time <= start_time:
                raise ValueError("end_time must be after start_time")

            location = _normalize_location({"location": slot.get('location')})
            if location is None:
                raise ValueError("Field 'location' is required")

            name = _normalize_optional_slot_name(slot.get('name'))
            is_available = slot.get('is_available', True)
            if not isinstance(is_available, bool):
                raise ValueError("Field 'is_available' must be a boolean")

            # Check if it's a new slot (temporary ID from frontend)
            if slot_id_str and slot_id_str.startswith('temp_'):
                app.logger.debug(f"Creating new slot: {start_time} - {end_time}")
                new_slot = TimeSlot(
                    start_time=start_time,
                    end_time=end_time,
                    is_available=is_available,
                    name=name,
                    location=location
                )
                db.session.add(new_slot)
                db.session.flush()  # This will assign an ID to the new slot
                processed_slots.add(str(new_slot.id))
                app.logger.info(f"Added new slot with ID {new_slot.id}")
            elif slot_id_str in existing_slots and slot_id_str not in processed_slots:
                app.logger.debug(f"Updating existing slot {slot_id_str}")
                existing_slot = existing_slots[slot_id_str]
                existing_slot.start_time = start_time
                existing_slot.end_time = end_time
                existing_slot.is_available = is_available
                existing_slot.name = name
                existing_slot.location = location
                processed_slots.add(slot_id_str)
                app.logger.info(f"Updated slot {slot_id_str}")

        # Commit the transaction
        db.session.commit()
        app.logger.info(f"Successfully processed {len(processed_slots)} slots")
        
        return jsonify({
            "success": True, 
            "message": f"Successfully processed {len(processed_slots)} slots"
        })
    except ValueError as e:
        db.session.rollback()
        return jsonify({
            "success": False,
            "message": str(e)
        }), 400
    except SQLAlchemyError as e:
        # Rollback the transaction
        db.session.rollback()
        app.logger.error(f"Error saving slots: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error saving slots: {str(e)}. Original data preserved."
        }), 500

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

@app.route('/admin/logout')
def admin_logout():
    app.logger.info(f"Admin logout: {session.get('admin_username')}")
    session.pop('admin_logged_in', None)
    return redirect(url_for('index'))

@app.route('/api/admin/delete_timeslot/<int:id>', methods=['DELETE'])
@admin_required
def delete_timeslot(id):
    app.logger.info(f"Attempting to delete timeslot {id}")
    slot = TimeSlot.query.get(id)
    if slot:
        delete_subsequent = request.args.get('delete_subsequent', 'false').lower() == 'true'
        
        if delete_subsequent and slot.is_repeated:
            app.logger.info(f"Deleting slot {id} and subsequent repeating slots")
            subsequent_slots = TimeSlot.query.filter(
                TimeSlot.start_time >= slot.start_time,
                func.extract('dow', TimeSlot.start_time) == func.extract('dow', slot.start_time),
                func.extract('hour', TimeSlot.start_time) == func.extract('hour', slot.start_time),
                func.extract('minute', TimeSlot.start_time) == func.extract('minute', slot.start_time),
                func.extract('hour', TimeSlot.end_time) == func.extract('hour', slot.end_time),
                func.extract('minute', TimeSlot.end_time) == func.extract('minute', slot.end_time),
                TimeSlot.is_repeated == True,
                TimeSlot.name == slot.name
            ).all()
            
            for subsequent_slot in subsequent_slots:
                db.session.delete(subsequent_slot)
                app.logger.debug(f"Deleted slot {subsequent_slot.id}")
        else:
            app.logger.info(f"Deleting single slot {id}")
            db.session.delete(slot)
        
        db.session.commit()
        app.logger.info(f"Successfully deleted timeslot(s)")
        return jsonify({'success': True})
    app.logger.warning(f"Failed to delete timeslot {id}: Slot not found")
    return jsonify({'success': False, 'message': 'Time slot not found'})

@app.route('/api/admin/change_location/<int:id>', methods=['POST'])
@admin_required
def change_location(id):
    app.logger.info(f"Attempting to change location for timeslot {id}")
    slot = db.session.get(TimeSlot, id)
    if slot:
        data = request.get_json(silent=True) or {}
        update_subsequent = bool(data.get('update_subsequent', False))
        try:
            new_location = _normalize_location({"location": data.get('location')})
        except ValueError as e:
            return jsonify({'success': False, 'message': str(e)}), 400

        if new_location is None:
            app.logger.warning(f"Failed to update location for slot {id}: New location not provided")
            return jsonify({'success': False, 'message': 'New location not provided'}), 400

        if update_subsequent and slot.is_repeated:
            app.logger.info(f"Updating location for slot {id} and subsequent repeating slots")
            # Update this slot and all subsequent repeating slots
            subsequent_slots = TimeSlot.query.filter(
                TimeSlot.start_time >= slot.start_time,
                func.extract('dow', TimeSlot.start_time) == func.extract('dow', slot.start_time),  # Match weekday
                func.extract('hour', TimeSlot.start_time) == func.extract('hour', slot.start_time),  # Match start hour
                func.extract('minute', TimeSlot.start_time) == func.extract('minute', slot.start_time),  # Match start minute
                func.extract('hour', TimeSlot.end_time) == func.extract('hour', slot.end_time),  # Match end hour
                func.extract('minute', TimeSlot.end_time) == func.extract('minute', slot.end_time),  # Match end minute
                TimeSlot.is_repeated == True,
                TimeSlot.name == slot.name
            ).all()

            for subsequent_slot in subsequent_slots:
                subsequent_slot.location = new_location
                app.logger.debug(f"Updated location for slot {subsequent_slot.id}")
        else:
            app.logger.info(f"Updating location for slot {id}")
            # Update only this slot
            slot.location = new_location

        db.session.commit()
        app.logger.info(f"Successfully updated location for slot {id}")
        return jsonify({'success': True})
    app.logger.warning(f"Failed to update location for slot {id}: Slot not found")
    return jsonify({'success': False, 'message': 'Time slot not found'})

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

def init_db():
    with app.app_context():
        db.create_all()
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
