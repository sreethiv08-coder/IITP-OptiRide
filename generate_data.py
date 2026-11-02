import csv
import random
from datetime import datetime, timedelta

# ==========================================
# Helper Functions
# ==========================================
def random_datetime():
    """Generates a datetime string in the exact format the backend expects: YYYY-MM-DD HH:MM"""
    start_date = datetime(2026, 10, 30)
    end_date = datetime(2026, 11, 30)
    delta_days = random.randint(0, (end_date - start_date).days)
    hour = random.randint(4, 22)
    minute = random.choice([0, 15, 30, 45])
    dt = start_date + timedelta(days=delta_days, hours=hour, minutes=minute)
    return dt.strftime("%Y-%m-%d %H:%M")

def generate_roll_number():
    year = random.choice([21, 22, 23, 24, 25])
    branch = random.choice(['CS', 'EE', 'ME', 'CE', 'CSE', 'ECE', 'MNC', 'MEE'])
    roll = random.randint(1, 60)
    return f"{year}01{branch}{roll:02d}"

# ==========================================
# 1. Generate vehicle_options.csv
# ==========================================
print("Generating vehicle_options.csv...")
vehicle_types = [
    {'type': 'Auto', 'pax': 3, 'lugg': 1, 'base_fare': 300},
    {'type': 'Sedan', 'pax': 4, 'lugg': 2, 'base_fare': 600},
    {'type': 'SUV', 'pax': 4, 'lugg': 3, 'base_fare': 800},
    {'type': 'MPV', 'pax': 6, 'lugg': 4, 'base_fare': 1000},
    {'type': 'Large Van', 'pax': 10, 'lugg': 8, 'base_fare': 1500},
    {'type': 'Tempo Traveller', 'pax': 12, 'lugg': 12, 'base_fare': 1800}
]
# Expanded destinations for Bihar
destinations = ['Patna Airport', 'Patna Junction', 'Gaya', 'Muzaffarpur', 'Darbhanga']

vehicles = []
for v in vehicle_types:
    for dest in destinations:
        # Fare varies slightly by destination distance
        fare_multiplier = 1.0 if 'Airport' in dest or 'Junction' in dest else 1.5
        total_fare = int(v['base_fare'] * fare_multiplier)
        vehicles.append({
            'vehicle_type': v['type'],
            'destination': dest,
            'passenger_capacity': v['pax'],
            'luggage_capacity': v['lugg'],
            'total_fare': total_fare
        })

with open('vehicle_options.csv', mode='w', newline='', encoding='utf-8') as f:
    # Exact column names required by the backend
    writer = csv.DictWriter(f, fieldnames=['vehicle_type', 'destination', 'passenger_capacity', 'luggage_capacity', 'total_fare'])
    writer.writeheader()
    writer.writerows(vehicles)
print("✅ Saved: vehicle_options.csv\n")

# ==========================================
# 2. Generate trip_requests.csv
# ==========================================
print("Generating trip_requests.csv...")
first_names = ['Aarav', 'Vivaan', 'Aditya', 'Vihaan', 'Arjun', 'Diya', 'Ananya', 'Isha', 'Saanvi', 'Aanya', 'Rohan', 'Kabir', 'Rahul', 'Priya', 'Neha', 'Sneha', 'Vikram']
last_names = ['Sharma', 'Verma', 'Gupta', 'Singh', 'Kumar', 'Patel', 'Reddy', 'Nair', 'Iyer', 'Das', 'Mishra', 'Jha', 'Pandey', 'Tiwari']
genders = ['Male', 'Female']
preferences = ['Male', 'Female', 'Any']

requests = []
for i in range(1, 101):
    pax = random.randint(1, 4)
    lugg = random.randint(0, 3)
    
    # Generate a time window (e.g., 30 to 60 minutes wide) for earliest/latest departure
    start_dt = datetime.strptime(random_datetime(), "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(minutes=random.choice([30, 45, 60]))
    
    # Solo fare is what they would pay alone (used by backend to calculate "Estimated saving")
    solo_fare = random.randint(400, 800) if random.random() > 0.3 else ""

    requests.append({
        'request_id': f"REQ{i:03d}",
        'student_name': f"{random.choice(first_names)} {random.choice(last_names)}",
        'roll_number': generate_roll_number(),
        'contact_number': f"9{random.randint(100000000, 999999999)}",
        'pickup_location': 'IIT Patna Campus',
        'destination': random.choice(destinations),
        'earliest_departure': start_dt.strftime("%Y-%m-%d %H:%M"),
        'latest_departure': end_dt.strftime("%Y-%m-%d %H:%M"),
        'passengers': pax,
        'luggage': lugg,
        'solo_fare': solo_fare,
        'gender': random.choice(genders),
        'gender_preference': random.choice(preferences),
        'ride_status': random.choice(['NEEDS_RIDE', 'OFFERS_RIDE'])
    })

with open('trip_requests.csv', mode='w', newline='', encoding='utf-8') as f:
    # Backend strictly requires these. Extra columns are safely ignored by the backend's parser.
    required_cols = ['request_id', 'student_name', 'pickup_location', 'destination', 'earliest_departure', 'latest_departure', 'passengers', 'luggage']
    optional_cols = ['roll_number', 'contact_number', 'gender', 'gender_preference', 'ride_status', 'solo_fare']
    writer = csv.DictWriter(f, fieldnames=required_cols + optional_cols)
    writer.writeheader()
    writer.writerows(requests)
print("✅ Saved: trip_requests.csv\n")

# ==========================================
# 3. Generate available_rides.csv
# ==========================================
print("Generating available_rides.csv...")
rides = []
for i in range(1, 26):
    v = random.choice(vehicle_types)
    dest = random.choice(destinations)
    
    # Ensure booked passengers is at least 1, and leaves at least 1 seat available
    booked_p = random.randint(1, v['pax'] - 1)
    booked_l = random.randint(0, v['lugg'])
    
    rides.append({
        'ride_id': f"RIDE{i:03d}",
        'booked_by': f"Student_{random.randint(100, 999)}",
        'pickup_location': 'IIT Patna Campus',
        'destination': dest,
        'departure_time': random_datetime(),
        'vehicle_type': v['type'],
        'total_fare': int(v['base_fare'] * (1.5 if 'Airport' in dest or 'Junction' in dest else 2.0)),
        'passenger_capacity': v['pax'],
        'luggage_capacity': v['lugg'],
        'booked_passengers': booked_p,
        'booked_luggage': booked_l
    })

with open('available_rides.csv', mode='w', newline='', encoding='utf-8') as f:
    # Exact column names required by the backend
    required_cols = ['ride_id', 'booked_by', 'pickup_location', 'destination', 'departure_time', 'vehicle_type', 'total_fare', 'passenger_capacity', 'luggage_capacity', 'booked_passengers', 'booked_luggage']
    writer = csv.DictWriter(f, fieldnames=required_cols)
    writer.writeheader()
    writer.writerows(rides)
print("✅ Saved: available_rides.csv\n")

# ==========================================
# 4. Generate booking_records.csv
# ==========================================
print("Generating booking_records.csv...")
booking_statuses = ['CONFIRMED', 'CONFIRMED', 'CONFIRMED', 'CANCELLED', 'PENDING']
sampled_request_ids = random.sample([req['request_id'] for req in requests], 20)

bookings = []
for i, req_id in enumerate(sampled_request_ids, start=1):
    if random.random() < 0.7:
        assigned_ride = f"RIDE{random.randint(1, 25):03d}"
    else:
        assigned_ride = "NEW_RIDE_BOOKED"
        
    status = random.choice(booking_statuses)
    fare_share = random.randint(100, 400) if status != 'CANCELLED' else 0
    booking_date = random_datetime().split()[0] # Just the date part

    bookings.append({
        'booking_id': f"BK{i:03d}",
        'request_id': req_id,
        'assigned_ride_id': assigned_ride,
        'status': status,
        'booking_date': booking_date,
        'fare_share_inr': fare_share
    })

with open('booking_records.csv', mode='w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=bookings[0].keys())
    writer.writeheader()
    writer.writerows(bookings)
print("✅ Saved: booking_records.csv\n")

print("🎉 All 4 datasets generated successfully, perfectly matching the backend schema!")