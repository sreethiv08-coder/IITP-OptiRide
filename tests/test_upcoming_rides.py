import json
import tempfile
import unittest
import sys
import types
sys.modules.setdefault("pulp", types.ModuleType("pulp"))
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from rideshare.server import Api, ApiError
from rideshare.config import Config


class UpcomingRideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = Api.__new__(Api)
        self.api.base = Config(data_dir=Path(self.tmp.name) / "data", output_dir=Path(self.tmp.name),
                               bookings_file=Path(self.tmp.name) / "bookings.json")
        self.user = {"name": "A Student", "roll_number": "2401CS01", "contact_number": "9876543210"}
        self.departure = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
        self.api._student_write("student_ride_offers.json", [{
            "ride_id": "RIDE-1", "student_name": "Owner", "roll_number": "2401CS02",
            "pickup_location": "IIT Patna", "destination": "Patna Airport",
            "departure_time": self.departure, "vehicle_type": "Uber Go",
            "passenger_capacity": 4, "available_seats": 3, "luggage_capacity": 4,
            "total_fare": 900, "status": "OPEN"
        }])
        self.api._student_write("student_join_requests.json", [
            {"join_id": "J-PENDING", "ride_id": "RIDE-1", "owner_roll_number": "2401CS02",
             "request_roll_number": "2401CS01", "request_name": "A Student", "passengers": 1,
             "luggage": 1, "status": "PENDING"},
            {"join_id": "J-ACCEPTED", "ride_id": "RIDE-1", "owner_roll_number": "2401CS02",
             "request_roll_number": "2401CS01", "request_name": "A Student", "passengers": 1,
             "luggage": 1, "status": "ACCEPTED"}
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_request_is_not_upcoming_and_accepted_is(self):
        result = self.api.student_upcoming(self.user)
        self.assertEqual([r["participation_id"] for r in result["rides"]], ["J-ACCEPTED"])

    def test_cancellation_releases_join_and_keeps_cab_open(self):
        result = self.api.cancel_student_participation("J-ACCEPTED", self.user)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(self.api._student_read("student_ride_offers.json")[0]["status"], "OPEN")
        join = next(j for j in self.api._student_read("student_join_requests.json") if j["join_id"] == "J-ACCEPTED")
        self.assertEqual(join["status"], "CANCELLED")
        self.assertEqual(self.api.student_upcoming(self.user)["rides"], [])

    def test_cannot_cancel_another_students_participation(self):
        with self.assertRaises(ApiError) as ctx:
            self.api.cancel_student_participation("J-ACCEPTED", {"roll_number": "2401CS99"})
        self.assertEqual(ctx.exception.status, 403)

    def test_inside_24_hour_deadline_is_rejected(self):
        rides = self.api._student_read("student_ride_offers.json")
        rides[0]["departure_time"] = (datetime.now() + timedelta(hours=23)).strftime("%Y-%m-%d %H:%M")
        self.api._student_write("student_ride_offers.json", rides)
        with self.assertRaises(ApiError) as ctx:
            self.api.cancel_student_participation("J-ACCEPTED", self.user)
        self.assertEqual(str(ctx.exception), "Cancellation is allowed only at least 24 hours before departure.")

    def test_duplicate_cancellation_is_prevented(self):
        self.api.cancel_student_participation("J-ACCEPTED", self.user)
        with self.assertRaises(ApiError):
            self.api.cancel_student_participation("J-ACCEPTED", self.user)

    def test_owner_can_accept_request_and_only_owner_can(self):
        self.api._student_write("student_join_requests.json", [{
            "join_id": "J2", "ride_id": "RIDE-1", "owner_roll_number": "2401CS02",
            "request_roll_number": "2401CS03", "passengers": 1, "luggage": 1, "status": "PENDING"
        }])
        with self.assertRaises(ApiError) as ctx:
            self.api.owner_join_action("J2", "accept", {"roll_number": "2401CS99"})
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(self.api.owner_join_action("J2", "accept", {"roll_number": "2401CS02"})["join"]["status"], "ACCEPTED")


if __name__ == "__main__":
    unittest.main()
