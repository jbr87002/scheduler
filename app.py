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
from sqlalchemy.exc import SQLAlchemyError
from zoneinfo import ZoneInfo
from sqlalchemy import func
import uuid
from flask import send_file, make_response
import logging
from logging.handlers import RotatingFileHandler
import sys
import hashlib
from flask import Response
import time

load_dotenv()  # This line loads the variables from .env

london_tz = ZoneInfo("Europe/London")

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function

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
    password_hash = db.Column(db.String(255), nullable=False)  # Changed from 120 to 255
    calendar_id = db.Column(db.String(36), unique=True, nullable=False)

    def __init__(self, username, password):
        self.username = username
        self.password_hash = generate_password_hash(password)
        self.calendar_id = str(uuid.uuid4())

@app.route('/')
def index():
    app.logger.info('Accessing index page')
    supervision_start_date = os.getenv('SUPERVISION_START_DATE')
    return render_template('index.html', supervision_start_date=supervision_start_date)

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json
    app.logger.info(f"Signup attempt received: {data}")
    timeslot = TimeSlot.query.get(data['id'])
    if timeslot and timeslot.is_available:
        app.logger.info(f"Booking slot {timeslot.id} for {data['name']}")
        # Book the current slot
        timeslot.is_available = False
        timeslot.name = data['name']
        timeslot.is_repeated = data['repeat']
        
        if data['repeat']:
            app.logger.info(f"Creating repeated slots for {data['name']}")
            # Create repeated slots until the end of term
            current_date = timeslot.start_time
            end_of_term = datetime.fromisoformat(os.getenv('END_OF_TERM')).replace(tzinfo=london_tz)
            
            while current_date.date() <= end_of_term.date():
                if current_date.date() != timeslot.start_time.date():  # Skip the original date
                    repeated_start = current_date.replace(
                        hour=timeslot.start_time.hour,
                        minute=timeslot.start_time.minute
                    )
                    repeated_end = repeated_start + (timeslot.end_time - timeslot.start_time)
                    
                    repeated_slot = TimeSlot(
                        start_time=repeated_start,
                        end_time=repeated_end,
                        is_available=False,
                        name=data['name'],
                        location=timeslot.location,
                        is_repeated=True
                    )
                    db.session.add(repeated_slot)
                    app.logger.debug(f"Created repeated slot: {repeated_slot.start_time} - {repeated_slot.end_time}")
                
                current_date += timedelta(weeks=1)
        
        db.session.commit()
        app.logger.info(f"Successfully booked slot(s) for {data['name']}")
        return jsonify({'success': True})
    app.logger.warning(f"Failed to book slot {data['id']} for {data['name']}: Slot not available")
    return jsonify({'success': False, 'message': 'Time slot not available'})

@app.route('/api/admin/set_timeslots', methods=['POST'])
@admin_required
def set_timeslots():
    slots = request.json
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
            slot_id = slot.get('id')
            
            # Parse times as simple datetime objects without timezone info
            start_time = datetime.fromisoformat(slot['start_time'].replace('Z', ''))
            end_time = datetime.fromisoformat(slot['end_time'].replace('Z', ''))
            
            # Check if it's a new slot (temporary ID from frontend)
            if slot_id and slot_id.startswith('temp_'):
                app.logger.debug(f"Creating new slot: {start_time} - {end_time}")
                new_slot = TimeSlot(
                    start_time=start_time,
                    end_time=end_time,
                    is_available=slot.get('is_available', True),
                    name=slot.get('name'),
                    location=slot['location']
                )
                db.session.add(new_slot)
                db.session.flush()  # This will assign an ID to the new slot
                processed_slots.add(str(new_slot.id))
                app.logger.info(f"Added new slot with ID {new_slot.id}")
            elif slot_id in existing_slots and slot_id not in processed_slots:
                app.logger.debug(f"Updating existing slot {slot_id}")
                existing_slot = existing_slots[slot_id]
                existing_slot.start_time = start_time
                existing_slot.end_time = end_time
                existing_slot.is_available = slot.get('is_available', True)
                existing_slot.name = slot.get('name')
                existing_slot.location = slot['location']
                processed_slots.add(slot_id)
                app.logger.info(f"Updated slot {slot_id}")

        # Only delete slots if we received some slots and not all existing slots were updated
        if slots:
            for old_id in set(existing_slots.keys()) - processed_slots:
                app.logger.info(f"Deleting slot {old_id}")
                db.session.delete(existing_slots[old_id])

        # Commit the transaction
        db.session.commit()
        app.logger.info(f"Successfully processed {len(processed_slots)} slots")
        
        return jsonify({
            "success": True, 
            "message": f"Successfully processed {len(processed_slots)} slots"
        })
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

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        app.logger.info("Admin login attempt")
        password = request.form.get('password')
        if password == os.getenv('ADMIN_PASSWORD'):
            session['admin_logged_in'] = True
            admin = Admin.query.first()
            if not admin:
                app.logger.warning("No admin user found, creating default admin")
                admin = Admin(username='admin', password=password)
                db.session.add(admin)
                db.session.commit()
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
    # Check if an admin user exists, if not create one
    admin = Admin.query.first()
    if not admin:
        app.logger.warning("No admin user found, creating default admin")
        # Create a default admin user
        admin = Admin(username='admin', password=os.getenv('ADMIN_PASSWORD'))
        db.session.add(admin)
        db.session.commit()
    
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
    slot = TimeSlot.query.get(id)
    if slot:
        data = request.json
        new_location = data.get('location')
        update_subsequent = data.get('update_subsequent', False)
        
        if new_location:
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
        app.logger.warning(f"Failed to update location for slot {id}: New location not provided")
        return jsonify({'success': False, 'message': 'New location not provided'})
    app.logger.warning(f"Failed to update location for slot {id}: Slot not found")
    return jsonify({'success': False, 'message': 'Time slot not found'})

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

def init_db():
    with app.app_context():
        db.create_all()  # This will create all tables defined in your models
        
        if not Admin.query.first():
            admin = Admin(username='admin', password=os.getenv('ADMIN_PASSWORD'))
            db.session.add(admin)
            db.session.commit()

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

# Call this function when you start your app
if __name__ == '__main__':
    init_db()
    app.run(host='127.0.0.1', port=5001, debug=True)
