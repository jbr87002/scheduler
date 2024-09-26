from flask import Flask, request, jsonify, render_template, send_file
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from pytz import UTC
import icalendar
import os
import io
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # This will enable CORS for all routes
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///timeslots.db')
db = SQLAlchemy(app)

class TimeSlot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    is_available = db.Column(db.Boolean, default=True)
    name = db.Column(db.String(100))

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
        'name': slot.name if not slot.is_available else None
    } for slot in timeslots]
    print("Timeslots returned:", result)  # Debug print
    return jsonify(result)

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json
    timeslot = TimeSlot.query.get(data['id'])
    if timeslot and timeslot.is_available:
        timeslot.is_available = False
        timeslot.name = data['name']
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'Time slot not available'})

@app.route('/api/admin/set_timeslots', methods=['POST'])
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
        if 'id' in slot:
            # Update existing slot
            existing_slot = TimeSlot.query.get(slot['id'])
            if existing_slot:
                existing_slot.start_time = datetime.fromisoformat(slot['start_time']).replace(tzinfo=UTC)
                existing_slot.end_time = datetime.fromisoformat(slot['end_time']).replace(tzinfo=UTC)
                existing_slot.is_available = slot.get('is_available', True)
        else:
            # Add new slot
            new_slot = TimeSlot(
                start_time=datetime.fromisoformat(slot['start_time']).replace(tzinfo=UTC),
                end_time=datetime.fromisoformat(slot['end_time']).replace(tzinfo=UTC),
                is_available=slot.get('is_available', True)
            )
            db.session.add(new_slot)
    
    db.session.commit()
    return jsonify({"success": True})

@app.route('/admin')
def admin():
    return render_template('admin.html')

@app.route('/api/admin/get_timeslots', methods=['GET'])
def get_admin_timeslots():
    timeslots = TimeSlot.query.order_by(TimeSlot.start_time).all()
    return jsonify([{
        'id': slot.id,
        'start_time': slot.start_time.replace(tzinfo=UTC).isoformat(),
        'end_time': slot.end_time.replace(tzinfo=UTC).isoformat(),
        'is_available': slot.is_available,
        'name': slot.name
    } for slot in timeslots])

@app.route('/api/admin/delete_timeslot/<int:id>', methods=['DELETE'])
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