from flask import Flask, request, jsonify, render_template, send_file, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from pytz import UTC
import icalendar
import os
import io
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
from dateutil.relativedelta import relativedelta

load_dotenv()  # This line loads the variables from .env

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///timeslots.db')
db = SQLAlchemy(app)

class TimeSlot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    is_available = db.Column(db.Boolean, default=True)
    name = db.Column(db.String(100))
    location = db.Column(db.String(200))  # New field for location
    is_repeated = db.Column(db.Boolean, default=False)  # New field

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/timeslots', methods=['GET'])
def get_timeslots():
    timeslots = TimeSlot.query.all()
    result = [{
        'id': slot.id,
        'start_time': slot.start_time.isoformat(),
        'end_time': slot.end_time.isoformat(),
        'is_available': slot.is_available,
        'name': slot.name if not slot.is_available else None,
        'location': slot.location,
        'is_repeated': slot.is_repeated
    } for slot in timeslots]
    return jsonify(result)

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json
    timeslot = TimeSlot.query.get(data['id'])
    if timeslot and timeslot.is_available:
        # Book the current slot
        timeslot.is_available = False
        timeslot.name = data['name']
        timeslot.is_repeated = data['repeat']
        
        if data['repeat']:
            # Create repeated slots until the end of term
            current_date = timeslot.start_time + timedelta(weeks=1)
            end_of_term = datetime.fromisoformat(os.getenv('END_OF_TERM'))
            while current_date <= end_of_term:
                repeated_slot = TimeSlot(
                    start_time=current_date,
                    end_time=current_date + (timeslot.end_time - timeslot.start_time),
                    is_available=False,
                    name=data['name'],
                    location=timeslot.location,
                    is_repeated=True
                )
                db.session.add(repeated_slot)
                current_date += timedelta(weeks=1)
        
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'Time slot not available'})

@app.route('/api/admin/set_timeslots', methods=['POST'])
@admin_required
def set_timeslots():
    slots = request.json
    
    # Get the date range for the slots
    start_date = min(datetime.fromisoformat(slot['start_time']) for slot in slots).replace(tzinfo=UTC).date()
    end_date = max(datetime.fromisoformat(slot['end_time']) for slot in slots).replace(tzinfo=UTC).date() + timedelta(days=1)
    
    # Delete all existing slots in the date range that are not in the new set
    existing_slots = TimeSlot.query.filter(TimeSlot.start_time >= start_date, TimeSlot.start_time < end_date).all()
    for existing_slot in existing_slots:
        if not any(slot.get('id') == existing_slot.id for slot in slots):
            db.session.delete(existing_slot)
    
    # Update or add new slots
    for slot in slots:
        if slot.get('id'):
            # Update existing slot
            existing_slot = TimeSlot.query.get(slot['id'])
            if existing_slot:
                existing_slot.start_time = datetime.fromisoformat(slot['start_time']).replace(tzinfo=UTC)
                existing_slot.end_time = datetime.fromisoformat(slot['end_time']).replace(tzinfo=UTC)
                existing_slot.is_available = slot.get('is_available', True)
                # Do not update location for existing slots
        else:
            # Add new slot
            new_slot = TimeSlot(
                start_time=datetime.fromisoformat(slot['start_time']).replace(tzinfo=UTC),
                end_time=datetime.fromisoformat(slot['end_time']).replace(tzinfo=UTC),
                is_available=slot.get('is_available', True),
                location=slot['location']  # Location is required for new slots
            )
            db.session.add(new_slot)
    
    db.session.commit()
    return jsonify({"success": True})

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == os.getenv('ADMIN_PASSWORD'):
            session['admin_logged_in'] = True
            return redirect(url_for('admin'))
        else:
            return render_template('admin_login.html', error='Invalid password')
    return render_template('admin_login.html')

@app.route('/admin')
def admin():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    return render_template('admin.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('index'))

@app.route('/api/admin/get_timeslots', methods=['GET'])
@admin_required
def get_admin_timeslots():
    timeslots = TimeSlot.query.order_by(TimeSlot.start_time).all()
    return jsonify([{
        'id': slot.id,
        'start_time': slot.start_time.replace(tzinfo=UTC).isoformat(),
        'end_time': slot.end_time.replace(tzinfo=UTC).isoformat(),
        'is_available': slot.is_available,
        'name': slot.name,
        'location': slot.location,
        'is_repeated': slot.is_repeated
    } for slot in timeslots])

@app.route('/api/admin/delete_timeslot/<int:id>', methods=['DELETE'])
@admin_required
def delete_timeslot(id):
    print(f"Attempting to delete timeslot with id: {id}")  # Debug print
    slot = TimeSlot.query.get(id)
    if slot:
        print(f"Timeslot found: {slot}")  # Debug print
        db.session.delete(slot)
        db.session.commit()
        print("Timeslot deleted successfully")  # Debug print
        return jsonify({'success': True})
    print(f"Timeslot with id {id} not found")  # Debug print
    return jsonify({'success': False, 'message': 'Time slot not found'})

@app.route('/api/export')
def export_calendar():
    cal = icalendar.Calendar()
    timeslots = TimeSlot.query.filter_by(is_available=False).all()
    for slot in timeslots:
        event = icalendar.Event()
        event.add('summary', f"Appointment with {slot.name}")
        event.add('dtstart', slot.start_time)
        event.add('dtend', slot.end_time)
        cal.add_component(event)
    
    response = send_file(
        io.BytesIO(cal.to_ical()),
        mimetype='text/calendar',
        as_attachment=True,
        download_name='appointments.ics'
    )
    return response

def init_db():
    with app.app_context():
        db.create_all()

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5001))  # Changed default port to 5001
    app.run(host='0.0.0.0', port=port)