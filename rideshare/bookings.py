"""Booking and cancellation management with JSON persistence.

Statuses: PENDING -> CONFIRMED -> CANCELLED  (PENDING may also go straight to CANCELLED).

Capacity is never stored as a counter. It is recomputed from the ACTIVE
(PENDING or CONFIRMED) bookings every time, so a cancelled booking cannot keep
occupying seats or luggage space, and a restart cannot desynchronise counts.

Cancellation policy: a booking may be cancelled while
    now <= ride departure time - cancellation_deadline_hours   (default 24 h)
i.e. cancelling exactly 24 h before departure is still allowed, one minute
later is not. The rule applies to PENDING and CONFIRMED bookings alike.

Bookable rides are the booked cabs in available_rides.csv plus any new cabs
registered from a saved solution (outputs/<algorithm>_solution.json).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .compatibility import departure_compatible, group_gender_check
from .config import Config
from .fares import split_fare
from .models import (CONFIRMED_FARE, ESTIMATED_FARE, Dataset, Party,
                     location_key)

PENDING, CONFIRMED, CANCELLED = "PENDING", "CONFIRMED", "CANCELLED"
ACTIVE = (PENDING, CONFIRMED)
TS = "%Y-%m-%d %H:%M:%S"


class BookingError(Exception):
    """An operation was refused; the message explains why."""


class BookingManager:
    def __init__(self, dataset: Dataset, cfg: Config, store_path: Path | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.dataset, self.cfg = dataset, cfg
        self.path = Path(store_path or cfg.bookings_file)
        self.clock = clock or datetime.now
        self.requests = {r.request_id: r for r in dataset.requests}
        self.state = self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "next_number": 1, "registered_rides": {}, "bookings": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BookingError(f"Booking store {self.path} is corrupted: {exc}") from exc

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)
        os.replace(tmp, self.path)   # atomic: a crash never leaves a half-written file

    # ------------------------------------------------------------------ rides
    def _ride(self, ride_id: str) -> dict:
        for r in self.dataset.rides:
            if r.ride_id == ride_id:
                return {"ride_id": r.ride_id, "kind": "EXISTING",
                        "pickup_location": r.pickup_location, "destination": r.destination,
                        "departure_time": r.departure_time, "vehicle_type": r.vehicle_type,
                        "passenger_capacity": r.passenger_capacity,
                        "luggage_capacity": r.luggage_capacity, "total_fare": r.total_fare,
                        "fare_status": CONFIRMED_FARE, "base_passengers": r.booked_passengers,
                        "base_luggage": r.booked_luggage, "booker": r.booker_party}
        reg = self.state["registered_rides"].get(ride_id)
        if reg is None:
            raise BookingError(f"Unknown ride {ride_id}. Booked cabs come from available_rides.csv; "
                               f"new cabs must be registered from a solution first.")
        return {**reg, "departure_time": datetime.fromisoformat(reg["departure_time"]),
                "total_fare": Decimal(reg["total_fare"]), "base_passengers": 0,
                "base_luggage": 0, "booker": None}

    def register_solution(self, solution_file: Path) -> list[str]:
        """Make the NEW cabs of a saved, verified solution bookable."""
        data = json.loads(Path(solution_file).read_text(encoding="utf-8"))
        if not data.get("verified"):
            raise BookingError(f"{solution_file} did not pass verification; refusing to register it")
        added = []
        for ride in data["rides"]:
            if ride["kind"] != "NEW":
                continue
            record = {k: ride[k] for k in ("ride_id", "kind", "pickup_location", "destination",
                                           "departure_time", "vehicle_type", "passenger_capacity",
                                           "luggage_capacity", "total_fare")}
            record["fare_status"] = ESTIMATED_FARE
            existing = self.state["registered_rides"].get(ride["ride_id"])
            if existing and existing != record:
                raise BookingError(f"Ride {ride['ride_id']} is already registered with different "
                                   f"details; use a fresh booking store for a new solution")
            if not existing:
                self.state["registered_rides"][ride["ride_id"]] = record
                added.append(ride["ride_id"])
        self._save()
        return added

    def _active_on(self, ride_id: str) -> list[dict]:
        return [b for b in self.state["bookings"].values()
                if b["ride_id"] == ride_id and b["status"] in ACTIVE]

    def capacity(self, ride_id: str) -> dict:
        ride = self._ride(ride_id)
        active = self._active_on(ride_id)
        seats = ride["base_passengers"] + sum(b["passengers"] for b in active)
        bags = ride["base_luggage"] + sum(b["luggage"] for b in active)
        return {"ride_id": ride_id, "passenger_capacity": ride["passenger_capacity"],
                "seats_used": seats, "seats_remaining": ride["passenger_capacity"] - seats,
                "luggage_capacity": ride["luggage_capacity"], "luggage_used": bags,
                "luggage_remaining": ride["luggage_capacity"] - bags,
                "active_bookings": [b["booking_id"] for b in active]}

    def fare_breakdown(self, ride_id: str) -> dict[str, Decimal]:
        """Current proportional split of the ride's fare over everyone on it."""
        ride = self._ride(ride_id)
        weights = {}
        if ride["booker"] is not None and ride["base_passengers"]:
            weights[ride["booker"].label] = ride["base_passengers"]
        weights.update({b["request_id"]: b["passengers"] for b in self._active_on(ride_id)})
        return split_fare(ride["total_fare"], weights)

    # ------------------------------------------------------------- operations
    def create_booking(self, request_id: str, ride_id: str) -> dict:
        if request_id not in self.requests:
            if any(k.split("@")[0] == request_id for k in self.dataset.invalid_requests):
                raise BookingError(f"Request {request_id} failed input validation and cannot be booked")
            raise BookingError(f"Unknown request {request_id}")
        req = self.requests[request_id]
        for b in self.state["bookings"].values():
            if b["request_id"] == request_id and b["status"] in ACTIVE:
                raise BookingError(f"Duplicate booking: request {request_id} already has "
                                   f"{b['status']} booking {b['booking_id']} on {b['ride_id']}")
        ride = self._ride(ride_id)
        now = self.clock()
        if now >= ride["departure_time"]:
            raise BookingError(f"Ride {ride_id} departed at {ride['departure_time']:%Y-%m-%d %H:%M}")
        if (location_key(req.pickup_location), location_key(req.destination)) != \
                (location_key(ride["pickup_location"]), location_key(ride["destination"])):
            raise BookingError(f"Route mismatch: {request_id} is {req.pickup_location} -> "
                               f"{req.destination}, ride {ride_id} is {ride['pickup_location']} -> "
                               f"{ride['destination']}")
        if not departure_compatible(req, ride["departure_time"], self.cfg):
            raise BookingError(f"Departure time incompatibility: ride leaves "
                               f"{ride['departure_time']:%Y-%m-%d %H:%M}, request window is "
                               f"{req.earliest_departure:%Y-%m-%d %H:%M}-{req.latest_departure:%H:%M}")
        cap = self.capacity(ride_id)
        if req.passengers > cap["seats_remaining"]:
            raise BookingError(f"Insufficient seats: {cap['seats_remaining']} left on {ride_id}, "
                               f"request needs {req.passengers}")
        if req.luggage > cap["luggage_remaining"]:
            raise BookingError(f"Insufficient luggage capacity: {cap['luggage_remaining']} left on "
                               f"{ride_id}, request has {req.luggage}")
        parties: list[Party] = [ride["booker"]] if ride["booker"] else []
        parties += [self.requests[b["request_id"]].party for b in self._active_on(ride_id)]
        g = group_gender_check(parties + [req.party], self.cfg)
        if not g.ok:
            raise BookingError(f"Gender preference incompatibility: {g.detail}")

        booking_id = f"BKG-{self.state['next_number']:04d}"
        self.state["next_number"] += 1
        weights = {}
        if ride["booker"] is not None and ride["base_passengers"]:
            weights[ride["booker"].label] = ride["base_passengers"]
        weights.update({b["request_id"]: b["passengers"] for b in self._active_on(ride_id)})
        weights[request_id] = req.passengers
        share = split_fare(ride["total_fare"], weights)[request_id]
        booking = {
            "booking_id": booking_id, "request_id": request_id, "ride_id": ride_id,
            "status": PENDING, "created_at": now.strftime(TS), "confirmed_at": None,
            "cancelled_at": None, "passengers": req.passengers, "luggage": req.luggage,
            "ride_total_fare": str(ride["total_fare"]), "fare_status": ride["fare_status"],
            "fare_share_at_booking": str(share),
            "fare_note": "share is proportional to passengers and is re-split as others join/cancel",
            "gender_unverified": g.unverified,
        }
        self.state["bookings"][booking_id] = booking
        self._save()
        return booking

    def _get(self, booking_id: str) -> dict:
        try:
            return self.state["bookings"][booking_id]
        except KeyError:
            raise BookingError(f"Unknown booking {booking_id}") from None

    def confirm_booking(self, booking_id: str) -> dict:
        b = self._get(booking_id)
        if b["status"] != PENDING:
            raise BookingError(f"Only PENDING bookings can be confirmed; {booking_id} is {b['status']}")
        b["status"], b["confirmed_at"] = CONFIRMED, self.clock().strftime(TS)
        self._save()
        return b

    def cancellation_deadline(self, booking_id: str) -> datetime:
        b = self._get(booking_id)
        departure = self._ride(b["ride_id"])["departure_time"]
        return departure - timedelta(hours=self.cfg.cancellation_deadline_hours)

    def cancel_booking(self, booking_id: str) -> dict:
        b = self._get(booking_id)
        if b["status"] == CANCELLED:
            raise BookingError(f"Booking {booking_id} is already cancelled")
        deadline = self.cancellation_deadline(booking_id)
        now = self.clock()
        if now > deadline:
            raise BookingError("Cancellation deadline passed")
        b["status"], b["cancelled_at"] = CANCELLED, now.strftime(TS)
        self._save()
        return b

    def book_solution(self, solution_file: Path) -> tuple[list[dict], list[str]]:
        """Create PENDING bookings for every assignment in a saved solution."""
        self.register_solution(solution_file)
        data = json.loads(Path(solution_file).read_text(encoding="utf-8"))
        created, errors = [], []
        for ride in data["rides"]:
            for rid in ride["request_ids"]:
                try:
                    created.append(self.create_booking(rid, ride["ride_id"]))
                except BookingError as exc:
                    errors.append(f"{rid} -> {ride['ride_id']}: {exc}")
        return created, errors

    def list_bookings(self, status: str | None = None) -> list[dict]:
        return [b for b in self.state["bookings"].values() if status in (None, b["status"])]
