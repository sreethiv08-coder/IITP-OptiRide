"""Builders for small, controlled CSV datasets used by the tests."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from rideshare.config import Config
from rideshare.data_loader import load_dataset

TRIP_HEADER = ["request_id", "student_name", "pickup_location", "destination",
               "earliest_departure", "latest_departure", "passengers", "luggage",
               "gender", "gender_preference", "solo_fare"]
RIDE_HEADER = ["ride_id", "booked_by", "pickup_location", "destination", "departure_time",
               "vehicle_type", "total_fare", "passenger_capacity", "luggage_capacity",
               "booked_passengers", "booked_luggage", "booker_gender", "booker_gender_preference"]
VEHICLE_HEADER = ["vehicle_type", "destination", "passenger_capacity", "luggage_capacity", "total_fare"]

CAMPUS = "IIT Patna Campus"
PJ = "Patna Junction"

# Two-vehicle price list: makes greedy-vs-IP differences easy to construct.
SMALL_VEHICLES = [["Auto", PJ, 3, 1, 300], ["Sedan", PJ, 4, 2, 500]]
FULL_VEHICLES = [["Auto", PJ, 3, 1, 300], ["Sedan", PJ, 4, 2, 600], ["SUV", PJ, 4, 3, 800],
                 ["MPV", PJ, 6, 4, 1000]]


def req(rid, dest=PJ, start="2026-11-10 10:00", end="2026-11-10 11:00", pax=1, lug=0,
        gender="Male", pref="Any", solo="", pickup=CAMPUS, name=None):
    return [rid, name or f"Student {rid}", pickup, dest, start, end, pax, lug, gender, pref, solo]


def ride(rid, dest=PJ, dep="2026-11-10 10:30", cap=4, lugcap=2, booked=1, booked_lug=0,
         fare=900, vehicle="Sedan", gender="Male", pref="Any", pickup=CAMPUS):
    return [rid, f"Booker {rid}", pickup, dest, dep, vehicle, fare, cap, lugcap, booked,
            booked_lug, gender, pref]


def write(path: Path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def make_dataset(tmp_path: Path, requests, rides=(), vehicles=FULL_VEHICLES,
                 ride_header=RIDE_HEADER, trip_header=TRIP_HEADER):
    write(tmp_path / "trip_requests.csv", trip_header, requests)
    write(tmp_path / "available_rides.csv", ride_header, rides)
    write(tmp_path / "vehicle_options.csv", VEHICLE_HEADER, vehicles)
    cfg = Config(data_dir=tmp_path, output_dir=tmp_path / "out",
                 bookings_file=tmp_path / "out" / "bookings.json")
    return load_dataset(tmp_path), cfg


class Clock:
    """Controllable clock for booking tests."""

    def __init__(self, when: str):
        self.now = datetime.strptime(when, "%Y-%m-%d %H:%M")

    def __call__(self):
        return self.now

    def set(self, when: str):
        self.now = datetime.strptime(when, "%Y-%m-%d %H:%M")
