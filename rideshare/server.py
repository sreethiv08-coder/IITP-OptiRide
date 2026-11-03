"""Web UI server and JSON API, built only on Python's standard library.

    python -m rideshare serve -> http://127.0.0.1:8000

Serves the static frontend and JSON API.
"""
from __future__ import annotations

import os
import re
import json
import mimetypes
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import service, auth
from .bookings import BookingError, BookingManager
from .compatibility import REASONS
from .config import PROJECT_ROOT, Config
from .data_loader import DataValidationError
from .fares import savings_summary

from .ip_model import run_ip
from .greedy import run_greedy
from .models import Dataset, TripRequest
from .reporting import (
    comparison_markdown, export_solution, metrics,
    validation_markdown
)
from .request_manager import RequestBook, submit_request
from .sensitivity import run_sensitivity, sensitivity_markdown
from .vehicle_selection import VehicleCatalog
from .verification import verify_solution
from .location_validation import validate_route_locations

FRONTEND = PROJECT_ROOT / "frontend"
REPORTS = {"validation", "greedy", "ip", "comparison", "sensitivity"}
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml"
}
LOCK = threading.Lock()


# Exact vehicle choices and passenger capacities accepted by the student UI.
# Auto capacity is selectable as either 4 or 5; its fare may be a driver quote.
STUDENT_VEHICLE_CAPACITIES = {
    "Uber Go": (4,),
    "Go Non AC": (4,),
    "Premier": (4,),
    "Uber XL": (6,),
    "Auto": (4, 5),
    "Other 4 Seater Car": (4,),
    "Other 6 Seater Car": (6,),
}


def _validate_student_vehicle(vehicle_type, capacity):
    vehicle = str(vehicle_type or "").strip()
    if vehicle not in STUDENT_VEHICLE_CAPACITIES:
        raise ApiError(400, "Please select a valid vehicle type.")
    if capacity not in STUDENT_VEHICLE_CAPACITIES[vehicle]:
        allowed = " or ".join(str(x) for x in STUDENT_VEHICLE_CAPACITIES[vehicle])
        raise ApiError(
            400,
            f"{vehicle} must have a passenger capacity of {allowed}."
        )


def _fare_display(total_fare, fare_status=None):
    if total_fare is None:
        return "Fare depends on the auto driver's quotation."
    try:
        amount = Decimal(str(total_fare))
        if not amount.is_finite() or amount <= 0:
            return "Fare not available"
        return f"Rs {amount:,.2f}"
    except Exception:
        return "Fare not available"



class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status

def _validate_route_locations(pickup, destination):
    try:
        validate_route_locations(pickup, destination)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc


def _json_default(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.strftime("%Y-%m-%d %H:%M")
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    raise TypeError(f"not serialisable: {type(o)}")


def config_from_settings(base: Config, s: dict) -> Config:
    try:
        penalty = s.get("penalty")
        return base.with_(
            time_tolerance_minutes=int(s.get("time_tolerance", 0) or 0),
            gender_matching=bool(s.get("gender_matching", True)),
            missing_gender_policy=s.get("gender_policy", "strict"),
            objective_mode=s.get("objective", "lexicographic"),
            unserved_penalty=float(penalty)
            if penalty not in (None, "") else None,
            solver_time_limit_seconds=int(s.get("time_limit", 60) or 60),
            cancellation_deadline_hours=float(
                s.get("deadline_hours", 24) or 24
            )
        )
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"Invalid settings: {exc}") from exc


def _submitted_file(cfg: Config) -> Path:
    return cfg.output_dir / "submitted_requests.json"


def _read_submitted(cfg: Config) -> list[dict]:
    path = _submitted_file(cfg)
    return (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists() else []
    )


def _to_request(d: dict) -> TripRequest:
    return TripRequest(
        d["request_id"],
        d["student_name"],
        d["pickup_location"],
        d["destination"],
        datetime.strptime(d["earliest_departure"], "%Y-%m-%d %H:%M"),
        datetime.strptime(d["latest_departure"], "%Y-%m-%d %H:%M"),
        int(d["passengers"]),
        int(d["luggage"]),
        d.get("gender"),
        d.get("gender_preference"),
        Decimal(d["solo_fare"]) if d.get("solo_fare") else None,
        ride_status="SUBMITTED_VIA_UI"
    )


def load_dataset_with_submissions(cfg: Config) -> Dataset:
    ds = service.load(cfg)
    extra = tuple(_to_request(d) for d in _read_submitted(cfg))
    return replace(ds, requests=ds.requests + extra) if extra else ds


def _parse_dt(value: str, field: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except (TypeError, ValueError):
            pass
    raise ApiError(400, f"{field}: invalid date-time '{value}'")


def request_payload(r: TripRequest, submitted: set[str]) -> dict:
    return {
        "request_id": r.request_id,
        "student_name": r.student_name,
        "pickup_location": r.pickup_location,
        "destination": r.destination,
        "earliest_departure": r.earliest_departure,
        "latest_departure": r.latest_departure,
        "passengers": r.passengers,
        "luggage": r.luggage,
        "gender": r.gender,
        "gender_preference": r.gender_preference,
        "solo_fare": r.solo_fare,
        "ride_status": r.ride_status,
        "submitted_via_ui": r.request_id in submitted
    }


def solution_payload(solution, ds: Dataset, errors: list[str]) -> dict:
    reqs = {r.request_id: r for r in ds.requests}
    m = metrics(solution, ds)
    rides = []

    for p in solution.rides:
        if not p.request_ids:
            continue

        parties = []

        for key, share in p.fare_shares.items():
            if key in reqs:
                s = savings_summary(reqs[key], share)
                parties.append({
                    "party": key,
                    "student_name": reqs[key].student_name,
                    "passengers": reqs[key].passengers,
                    "luggage": reqs[key].luggage,
                    "share": share,
                    "solo_fare": s["solo_fare"],
                    "savings": s["savings"],
                    "verdict": s["verdict"]
                })
            else:
                parties.append({
                    "party": key,
                    "student_name": "Original booker",
                    "passengers": p.base_passengers,
                    "luggage": p.base_luggage,
                    "share": share,
                    "solo_fare": None,
                    "savings": None,
                    "verdict": f"was paying Rs {p.total_fare} alone"
                })

        pax = p.base_passengers + sum(
            reqs[i].passengers for i in p.request_ids
        )
        lug = p.base_luggage + sum(
            reqs[i].luggage for i in p.request_ids
        )

        rides.append({
            "ride_id": p.ride_id,
            "kind": p.kind,
            "destination": p.destination,
            "pickup_location": p.pickup_location,
            "departure_time": p.departure_time,
            "vehicle_type": p.vehicle_type,
            "seats_used": pax,
            "passenger_capacity": p.passenger_capacity,
            "luggage_used": lug,
            "luggage_capacity": p.luggage_capacity,
            "total_fare": p.total_fare,
            "fare_display": _fare_display(p.total_fare, p.fare_status),
            "fare_status": p.fare_status,
            "request_ids": p.request_ids,
            "parties": parties,
            "gender_unverified": p.gender_unverified
        })

    unmatched = [{
        "request_id": u.request_id,
        "reason_code": u.reason_code,
        "reason": REASONS.get(u.reason_code, u.reason_code),
        "detail": u.detail
    } for u in solution.unmatched]

    w1 = [{
        "request_id": k,
        "reason_code": c,
        "reason": REASONS.get(c, c),
        "detail": d
    } for k, (c, d) in sorted(
        solution.existing_cab_reasons.items()
    )]

    return {
        "algorithm": solution.algorithm,
        "status": solution.status,
        "status_detail": solution.status_detail,
        "notes": solution.notes,
        "objective_value": solution.objective_value,
        "metrics": m,
        "verified": not errors,
        "verification_errors": errors,
        "rides": rides,
        "unmatched": unmatched,
        "workflow1_reasons": w1
    }


# ------------------------------------------------------------------
# API
# ------------------------------------------------------------------

class Api:
    def __init__(self, base_cfg: Config):
        self.base = base_cfg

    def _cfg(self, body: dict) -> Config:
        return config_from_settings(
            self.base, body.get("settings") or {}
        )

    def _manager(self, cfg: Config, body: dict) -> BookingManager:
        now = (body.get("settings") or {}).get("now")
        clock = (lambda: _parse_dt(now, "now")) if now else None
        return BookingManager(
            load_dataset_with_submissions(cfg), cfg, clock=clock
        )

    def _student_read(self, filename):
        path = self.base.output_dir / filename
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def _student_write(self, filename, records):
        path = self.base.output_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(records, indent=2), encoding="utf-8"
        )
        temp.replace(path)

    def _student_record(self, roll_number):
        roll = str(roll_number or "").strip().upper()
        users = auth._read(auth.USERS_FILE)
        return next((u for u in users.values() if str(u.get("roll_number", "")).upper() == roll), None)

    @staticmethod
    def _parse_student_departure(value):
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M")
        except ValueError:
            raise ApiError(400, "Invalid ride departure time.")

    def student_profile(self, target_roll, viewer):
        target = str(target_roll or "").strip().upper()
        viewer_roll = str(viewer.get("roll_number", "")).strip().upper()
        record = self._student_record(target)
        if not record:
            raise ApiError(404, "Student profile not found.")
        if target == viewer_roll:
            accepted = True
        else:
            accepted = False
            joins = self._student_read("student_join_requests.json")
            for item in joins:
                if item.get("status") not in ("ACCEPTED", "CONFIRMED"):
                    continue
                owner = str(item.get("owner_roll_number", "")).upper()
                requester = str(item.get("request_roll_number", "")).upper()
                if {owner, requester} == {viewer_roll, target}:
                    accepted = True
                    break
            if not accepted:
                for group in self._student_read("student_confirmed_groups.json"):
                    member_rolls = {str(m.get("roll_number", "")).upper() for m in group.get("members", [])}
                    if viewer_roll in member_rolls and target in member_rolls and group.get("status") == "CONFIRMED":
                        accepted = True
                        break
        profile = {
            "name": record.get("name", ""),
            "roll_number": record.get("roll_number", target),
            "gender": record.get("gender", ""),
            "contact_visible": accepted,
        }
        if accepted:
            profile["contact_number"] = record.get("contact_number", "")
        return profile

    def student_my_rides(self, user):
        roll = user["roll_number"].upper()
        offers = self._student_read("student_ride_offers.json")
        joins = self._student_read("student_join_requests.json")
        mine_offers = [dict(r, incoming_requests=[j for j in joins
                         if j.get("ride_id") == r.get("ride_id") and j.get("status") == "PENDING"],
                         accepted_passengers=[j for j in joins
                         if j.get("ride_id") == r.get("ride_id") and (
                             j.get("status") in ("ACCEPTED", "CONFIRMED")
                             or (j.get("status") == "CANCELLED_BY_OWNER"
                                 and j.get("previous_status") in ("ACCEPTED", "CONFIRMED"))
                         )])
                       for r in offers if str(r.get("roll_number", "")).upper() == roll]
        sent = [j for j in joins if str(j.get("request_roll_number", "")).upper() == roll]
        incoming = [j for j in joins if str(j.get("owner_roll_number", "")).upper() == roll]
        requests = [r for r in self._student_read("student_group_requests.json") if str(r.get("roll_number", "")).upper() == roll]
        group_links = self._student_read("student_group_join_requests.json")
        confirmed_groups = [g for g in self._student_read("student_confirmed_groups.json")
                            if any(str(m.get("roll_number", "")).upper() == roll for m in g.get("members", []))]
        return {"offered_rides": mine_offers, "incoming_requests": incoming,
                "sent_join_requests": sent, "group_requests": requests,
                "incoming_group_requests": [g for g in group_links if str(g.get("to_roll", "")).upper() == roll],
                "sent_group_requests": [g for g in group_links if str(g.get("from_roll", "")).upper() == roll],
                "confirmed_group_rides": confirmed_groups}

    def student_upcoming(self, user):
        roll = user["roll_number"].upper()
        rides = {r["ride_id"]: r for r in self._student_read("student_ride_offers.json")}
        joins = self._student_read("student_join_requests.json")
        upcoming = []
        for join in joins:
            if str(join.get("request_roll_number", "")).upper() != roll:
                continue
            if join.get("status") not in ("ACCEPTED", "CONFIRMED"):
                continue
            ride = rides.get(join.get("ride_id"))
            if not ride:
                continue
            active = [j for j in joins if j.get("ride_id") == ride["ride_id"]
                      and j.get("status") in ("ACCEPTED", "CONFIRMED")]
            passengers = int(ride.get("passenger_capacity", 0)) - int(ride.get("available_seats", 0)) + sum(
                int(j.get("passengers", 0)) for j in active
            )
            total_fare = ride.get("total_fare")
            all_passengers = int(ride.get("passenger_capacity", 0)) - int(ride.get("available_seats", 0)) + sum(
                int(j.get("passengers", 0)) for j in active
            )
            share = (float(total_fare) * int(join.get("passengers", 1)) / all_passengers) if total_fare and all_passengers else None
            others = [{"name": ride.get("student_name"), "roll_number": ride.get("roll_number"),
                       "passengers": int(ride.get("passenger_capacity", 0)) - int(ride.get("available_seats", 0)),
                       "pickup_location": ride.get("pickup_location"), "destination": ride.get("destination")}]
            others += [{"name": j.get("request_name"), "roll_number": j.get("request_roll_number"),
                        "passengers": j.get("passengers", 1), "pickup_location": ride.get("pickup_location"),
                        "destination": ride.get("destination")} for j in active]
            upcoming.append({
                "participation_id": join["join_id"], "source": "OFFERED_CAB",
                "ride_id": ride["ride_id"], "pickup_location": ride["pickup_location"],
                "destination": ride["destination"], "departure_time": ride["departure_time"],
                "vehicle_type": ride["vehicle_type"], "total_fare": total_fare,
                "fare_status": ride.get("fare_status", "ACTUAL" if total_fare is not None else "DRIVER_QUOTATION_REQUIRED"),
                "fare_display": _fare_display(total_fare),
                "individual_fare_share": share,
                "passengers": all_passengers, "available_seats": max(0, int(ride.get("available_seats", 0)) - sum(int(j.get("passengers",0)) for j in active)),
                "other_students": [x for x in others if str(x.get("roll_number","")).upper() != roll],
                "status": "CONFIRMED"
            })

        # Confirmed optimized bookings are tied to a request owned by this session.
        cfg = self.base
        try:
            manager = BookingManager(load_dataset_with_submissions(cfg), cfg)
            submitted = {x.get("request_id"): x for x in _read_submitted(cfg)}
            for booking in manager.list_bookings("CONFIRMED"):
                req_record = submitted.get(booking.get("request_id"), {})
                if str(req_record.get("roll_number", "")).upper() != roll:
                    continue
                ride = manager._ride(booking["ride_id"])
                req = manager.requests.get(booking["request_id"])
                split = manager.fare_breakdown(booking["ride_id"])
                capacity = manager.capacity(booking["ride_id"])
                other_students = []
                for other_booking in manager.list_bookings("CONFIRMED"):
                    if other_booking["ride_id"] != booking["ride_id"] or other_booking["booking_id"] == booking["booking_id"]:
                        continue
                    other_request = manager.requests.get(other_booking["request_id"])
                    other_record = submitted.get(other_booking["request_id"], {})
                    if other_request:
                        other_students.append({"name": other_request.student_name,
                            "roll_number": other_record.get("roll_number", ""),
                            "passengers": other_booking.get("passengers", 1),
                            "pickup_location": ride["pickup_location"], "destination": ride["destination"]})
                if ride.get("booker") is not None:
                    other_students.append({"name": ride["booker"].label,
                        "roll_number": "",
                        "passengers": ride.get("base_passengers", 1),
                        "pickup_location": ride["pickup_location"], "destination": ride["destination"]})
                upcoming.append({
                    "participation_id": booking["booking_id"], "source": "OPTIMIZED_GROUP",
                    "ride_id": booking["ride_id"], "pickup_location": ride["pickup_location"],
                    "destination": ride["destination"], "departure_time": ride["departure_time"].strftime("%Y-%m-%d %H:%M"),
                    "vehicle_type": ride["vehicle_type"], "total_fare": float(ride["total_fare"]) if ride.get("total_fare") is not None else None,
                    "fare_status": ride.get("fare_status", "ESTIMATED"),
                    "fare_display": _fare_display(ride.get("total_fare"), ride.get("fare_status")),
                    "individual_fare_share": float(split.get(booking["request_id"], 0)) if booking["request_id"] in split else None,
                    "passengers": capacity["seats_used"],
                    "available_seats": capacity["seats_remaining"],
                    "other_students": other_students, "status": "CONFIRMED"
                })
        except (BookingError, DataValidationError, OSError, ValueError):
            pass
        for group in self._student_read("student_confirmed_groups.json"):
            members = group.get("members", [])
            mine = next((m for m in members if str(m.get("roll_number", "")).upper() == roll), None)
            if not mine or group.get("status") != "CONFIRMED":
                continue
            recommendation = group.get("vehicle_recommendation") or {}
            total_fare = recommendation.get("total_fare")
            total_people = max(1, int(group.get("passengers", 1)))
            share = (float(total_fare) * int(mine.get("passengers", 1)) / total_people) if total_fare is not None else None
            upcoming.append({"participation_id": group["group_ride_id"], "source": "CONFIRMED_STUDENT_GROUP",
                "ride_id": group["group_ride_id"], "pickup_location": group["pickup_location"],
                "destination": group["destination"], "departure_time": group["departure_time"],
                "vehicle_type": recommendation.get("vehicle_type", "To be confirmed"),
                "total_fare": total_fare, "fare_status": recommendation.get("fare_status", "DRIVER_QUOTATION_REQUIRED"),
                "fare_display": recommendation.get("fare_display", "Fare requires confirmation"),
                "individual_fare_share": share, "passengers": group.get("passengers", 0),
                "available_seats": None, "other_students": [m for m in members if str(m.get("roll_number", "")).upper() != roll],
                "status": "CONFIRMED"})

        history = [{"participation_id": j.get("join_id"), "ride_id": j.get("ride_id"),
                    "status": "CANCELLED"} for j in joins
                   if str(j.get("request_roll_number", "")).upper() == roll and j.get("status") == "CANCELLED"]
        try:
            history.extend({"participation_id": b["booking_id"], "ride_id": b["ride_id"],
                            "status": "CANCELLED"} for b in manager.list_bookings("CANCELLED")
                           if str(submitted.get(b.get("request_id"), {}).get("roll_number", "")).upper() == roll)
        except (NameError, UnboundLocalError):
            pass
        return {"rides": upcoming, "history": history}

    def cancel_student_participation(self, participation_id, user):
        roll = user["roll_number"].upper()
        joins = self._student_read("student_join_requests.json")
        join = next((j for j in joins if j.get("join_id") == participation_id), None)
        if join:
            if str(join.get("request_roll_number", "")).upper() != roll:
                raise ApiError(403, "You can cancel only your own participation.")
            if join.get("status") not in ("ACCEPTED", "CONFIRMED"):
                raise ApiError(409, "This participation is not an active confirmed ride.")
            rides = self._student_read("student_ride_offers.json")
            ride = next((r for r in rides if r.get("ride_id") == join.get("ride_id")), None)
            if not ride:
                raise ApiError(404, "The associated cab offer was not found.")
            deadline = self._parse_student_departure(ride["departure_time"]) - timedelta(hours=24)
            if datetime.now() > deadline:
                raise ApiError(409, "Cancellation is allowed only at least 24 hours before departure.")
            join["status"] = "CANCELLED"
            join["cancelled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._student_write("student_join_requests.json", joins)
            # Keep the cab offer active. Capacity is recomputed from active accepted joins.
            return {"message": "Your participation has been cancelled successfully.", "status": "CANCELLED",
                    "ride_id": ride["ride_id"], "reoptimization": "Capacity released; compatible pending requests may now be accepted."}

        cfg = self.base
        manager = BookingManager(load_dataset_with_submissions(cfg), cfg)
        booking = next((b for b in manager.list_bookings() if b.get("booking_id") == participation_id), None)
        if not booking:
            raise ApiError(404, "Upcoming ride not found.")
        submitted = {x.get("request_id"): x for x in _read_submitted(cfg)}
        if str(submitted.get(booking["request_id"], {}).get("roll_number", "")).upper() != roll:
            raise ApiError(403, "You can cancel only your own participation.")
        if booking["status"] != "CONFIRMED":
            raise ApiError(409, "This participation is not an active confirmed ride.")
        try:
            manager.cancel_booking(participation_id)
        except BookingError as exc:
            if "Cancellation deadline passed" in str(exc):
                raise ApiError(409, "Cancellation deadline passed") from exc
            raise
        return {"message": "Your participation has been cancelled successfully.", "status": "CANCELLED",
                "ride_id": booking["ride_id"], "reoptimization": "Remaining capacity released for future matching."}

    def owner_join_action(self, join_id, action, user):
        joins = self._student_read("student_join_requests.json")
        join = next((j for j in joins if j.get("join_id") == join_id), None)
        if not join:
            raise ApiError(404, "Join request not found.")
        if str(join.get("owner_roll_number", "")).upper() != user["roll_number"].upper():
            raise ApiError(403, "Only the cab owner can manage this request.")
        if join.get("status") != "PENDING":
            raise ApiError(409, "This join request has already been processed.")
        rides = self._student_read("student_ride_offers.json")
        ride = next((r for r in rides if r.get("ride_id") == join.get("ride_id")), None)
        if not ride or ride.get("status") != "OPEN":
            raise ApiError(409, "This cab offer is no longer available.")
        if action == "reject":
            join["status"] = "REJECTED"
        else:
            active = [j for j in joins if j.get("ride_id") == ride["ride_id"] and j.get("join_id") != join_id and j.get("status") in ("PENDING", "ACCEPTED", "CONFIRMED")]
            used_seats = sum(int(j.get("passengers", 0)) for j in active)
            used_bags = sum(int(j.get("luggage", 0)) for j in active)
            if int(ride["available_seats"]) - used_seats < int(join["passengers"]):
                raise ApiError(409, "Not enough available seats.")
            if int(ride["luggage_capacity"]) - used_bags < int(join["luggage"]):
                raise ApiError(409, "Not enough luggage capacity.")
            join["status"] = "ACCEPTED"
            join["accepted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._student_write("student_join_requests.json", joins)
        return {"join": join, "message": f"Join request {action}ed."}

    def student_post_offer(self, body, user=None):
        if user:
            body = {
                **body,
                "student_name": user["name"],
                "roll_number": user["roll_number"],
                "contact_number": user.get("contact_number", ""),
                "gender": user.get("gender", ""),
            }

        required = [
            "student_name", "roll_number", "contact_number",
            "pickup_location", "destination", "travel_date",
            "departure_time", "vehicle_type", "gender",
            "gender_preference", "passenger_capacity",
            "available_seats", "luggage_capacity",
        ]

        if any(not str(body.get(key, "")).strip() for key in required):
            raise ApiError(400, "Please fill all required fields.")

        _validate_route_locations(
            body.get("pickup_location"),
            body.get("destination"),
        )

        if body.get("cab_booked") != "yes":
            raise ApiError(
                400,
                "You must confirm that the cab is already booked.",
            )

        vehicle_type = str(body["vehicle_type"]).strip()

        try:
            capacity = int(body["passenger_capacity"])
            seats = int(body["available_seats"])
            luggage = int(body["luggage_capacity"])
            departure = datetime.strptime(
                body["travel_date"] + " " + body["departure_time"],
                "%Y-%m-%d %H:%M",
            )
        except (KeyError, TypeError, ValueError):
            raise ApiError(400, "Please enter valid ride details.")

        _validate_student_vehicle(vehicle_type, capacity)

        raw_fare = body.get("total_fare")
        if raw_fare in (None, ""):
            if vehicle_type != "Auto":
                raise ApiError(
                    400,
                    "Enter the actual total fare for this booked cab.",
                )
            fare = None
            fare_status = "DRIVER_QUOTATION_REQUIRED"
        else:
            try:
                fare = Decimal(str(raw_fare))
            except Exception as exc:
                raise ApiError(400, "Please enter a valid total fare.") from exc

            if not fare.is_finite() or fare <= 0:
                raise ApiError(400, "The total fare must be greater than zero.")

            fare_status = "ACTUAL"

        if (
            capacity < 1
            or seats < 1
            or seats > capacity
            or luggage < 0
        ):
            raise ApiError(
                400,
                "Please check the capacity, available seats and luggage.",
            )

        if body["gender"].upper() not in ("FEMALE", "MALE", "OTHER"):
            raise ApiError(400, "Invalid gender.")

        if body["gender_preference"].upper() not in ("SAME", "ANY"):
            raise ApiError(400, "Invalid gender preference.")

        rides = self._student_read("student_ride_offers.json")

        ride = {
            "ride_id": f"STU-RIDE-{len(rides) + 1:04d}",
            "student_name": body["student_name"].strip(),
            "roll_number": body["roll_number"].strip(),
            "contact_number": body["contact_number"].strip(),
            "pickup_location": body["pickup_location"].strip(),
            "destination": body["destination"].strip(),
            "departure_time": departure.strftime("%Y-%m-%d %H:%M"),
            "vehicle_type": vehicle_type,
            "passenger_capacity": capacity,
            "available_seats": seats,
            "luggage_capacity": luggage,
            "total_fare": float(fare) if fare is not None else None,
            "fare_status": fare_status,
            "gender": body["gender"].upper(),
            "gender_preference": body["gender_preference"].upper(),
            "status": "OPEN",
        }

        rides.append(ride)
        self._student_write("student_ride_offers.json", rides)

        return {
            "ride": {
                "ride_id": ride["ride_id"],
                "total_fare": ride["total_fare"],
                "fare_status": ride["fare_status"],
                "fare_display": _fare_display(ride["total_fare"]),
            }
        }

    def student_search(self, body):
        required = [
            "student_name", "roll_number", "contact_number",
            "pickup_location", "destination", "travel_date",
            "travel_time", "gender", "gender_preference"
        ]

        if any(not str(body.get(k, "")).strip() for k in required):
            raise ApiError(400, "Please fill all required fields.")

        _validate_route_locations(
            body.get("pickup_location"),
            body.get("destination")
        )

        try:
            passengers = int(body["passengers"])
            luggage = int(body["luggage"])
            wanted = datetime.strptime(
                body["travel_date"] + " " + body["travel_time"],
                "%Y-%m-%d %H:%M"
            )
        except (KeyError, ValueError):
            raise ApiError(
                400, "Invalid passenger, luggage, date or time."
            )

        if passengers < 1 or luggage < 0:
            raise ApiError(400, "Invalid passenger or luggage count.")

        if body["gender"].upper() not in ("FEMALE", "MALE", "OTHER"):
            raise ApiError(400, "Invalid gender.")

        if body["gender_preference"].upper() not in ("SAME", "ANY"):
            raise ApiError(400, "Invalid gender preference.")

        requests = self._student_read("student_trip_requests.json")
        requests.append({
            "student_name": body["student_name"].strip(),
            "roll_number": body["roll_number"].strip(),
            "contact_number": body["contact_number"].strip(),
            "pickup_location": body["pickup_location"].strip(),
            "destination": body["destination"].strip(),
            "passengers": passengers,
            "luggage": luggage
        })
        self._student_write("student_trip_requests.json", requests)

        rides = self._student_read("student_ride_offers.json")
        matches = []

        def accepts(preference, own_gender, other_gender):
            if preference == "ANY":
                return True
            return own_gender == other_gender

        for ride in rides:
            if ride["status"] != "OPEN":
                continue

            if (
                ride["pickup_location"].strip().casefold()
                != body["pickup_location"].strip().casefold()
            ):
                continue

            if (
                ride["destination"].strip().casefold()
                != body["destination"].strip().casefold()
            ):
                continue

            departure = datetime.strptime(
                ride["departure_time"], "%Y-%m-%d %H:%M"
            )

            if departure.date() != wanted.date():
                continue

            if abs((departure - wanted).total_seconds()) > 7200:
                continue

            if ride["available_seats"] < passengers:
                continue

            reserved_luggage = sum(j.get("luggage", 0) for j in self._student_read("student_join_requests.json")
                                   if j.get("ride_id") == ride["ride_id"]
                                   and j.get("status") in ("PENDING", "ACCEPTED", "CONFIRMED"))
            if ride["luggage_capacity"] - reserved_luggage < luggage:
                continue

            if not accepts(
                body["gender_preference"].upper(),
                body["gender"].upper(),
                ride["gender"]
            ):
                continue

            if not accepts(
                ride["gender_preference"],
                ride["gender"],
                body["gender"].upper()
            ):
                continue

            active_joins = [
                j for j in self._student_read("student_join_requests.json")
                if j.get("ride_id") == ride["ride_id"]
                and j.get("status") in ("ACCEPTED", "CONFIRMED")
            ]
            base_passengers = (
                int(ride["passenger_capacity"]) - int(ride["available_seats"])
            )
            occupied_passengers = base_passengers + sum(
                int(j.get("passengers", 0)) for j in active_joins
            )
            projected_passengers = occupied_passengers + passengers

            if ride.get("total_fare") is not None and projected_passengers > 0:
                estimated_share = round(
                    float(ride["total_fare"]) * passengers / projected_passengers,
                    2,
                )
            else:
                estimated_share = None

            matches.append({
                "ride_id": ride["ride_id"],
                "roll_number": ride.get("roll_number", ""),
                "student_name": ride["student_name"],
                "pickup_location": ride["pickup_location"],
                "destination": ride["destination"],
                "departure_time": ride["departure_time"],
                "vehicle_type": ride["vehicle_type"],
                "available_seats": ride["available_seats"],
                "luggage_capacity": ride["luggage_capacity"],
                "total_fare": ride.get("total_fare"),
                "fare_status": ride.get(
                    "fare_status",
                    "ACTUAL" if ride.get("total_fare") is not None
                    else "DRIVER_QUOTATION_REQUIRED",
                ),
                "fare_display": _fare_display(ride.get("total_fare")),
                "estimated_share_per_person": estimated_share,
            })

        return {"matches": matches}

    @staticmethod
    def _student_gender_accepts(preference, own_gender, other_gender):
        preference = str(preference or "").upper()
        own_gender = str(own_gender or "").upper()
        other_gender = str(other_gender or "").upper()
        return preference == "ANY" or (
            preference == "SAME" and own_gender == other_gender
        )

    def _student_group_matches(self, request, requests):
        matches = []
        wanted = datetime.strptime(request["travel_time"], "%Y-%m-%d %H:%M")
        for other in requests:
            if other.get("status") != "ACTIVE":
                continue
            if str(other.get("roll_number", "")).casefold() == str(request.get("roll_number", "")).casefold():
                continue
            try:
                other_time = datetime.strptime(other["travel_time"], "%Y-%m-%d %H:%M")
            except (KeyError, TypeError, ValueError):
                continue
            if other_time.date() != wanted.date():
                continue
            if other_time < datetime.now():
                continue
            if abs((other_time - wanted).total_seconds()) > 7200:
                continue
            if str(other.get("pickup_location", "")).strip().casefold() != request["pickup_location"].strip().casefold():
                continue
            if str(other.get("destination", "")).strip().casefold() != request["destination"].strip().casefold():
                continue
            if not self._student_gender_accepts(request["gender_preference"], request["gender"], other.get("gender")):
                continue
            if not self._student_gender_accepts(other.get("gender_preference"), other.get("gender"), request["gender"]):
                continue
            combined_passengers = int(request["passengers"]) + int(other["passengers"])
            if combined_passengers > 6:
                continue
            matches.append({
                "request_id": other["request_id"],
                "roll_number": other.get("roll_number", ""),
                "student_name": other["student_name"],
                "pickup_location": other["pickup_location"],
                "destination": other["destination"],
                "travel_time": other["travel_time"],
                "passengers": int(other["passengers"]),
                "luggage": int(other["luggage"]),
                "combined_passengers": combined_passengers,
                "combined_luggage": int(request["luggage"]) + int(other["luggage"]),
            })
        return matches

    def student_form_group(self, body, user):
        required = ("pickup_location", "destination", "travel_date", "travel_time",
                    "passengers", "luggage", "gender_preference")
        if any(not str(body.get(key, "")).strip() for key in required):
            raise ApiError(400, "Please fill all required fields.")
        _validate_route_locations(body.get("pickup_location"), body.get("destination"))
        try:
            passengers = int(body["passengers"])
            luggage = int(body["luggage"])
            wanted = datetime.strptime(
                str(body["travel_date"]) + " " + str(body["travel_time"]),
                "%Y-%m-%d %H:%M"
            )
        except (TypeError, ValueError):
            raise ApiError(400, "Invalid passenger, luggage, date or time.")
        if passengers < 1 or passengers > 6 or luggage < 0:
            raise ApiError(400, "Passenger count must be between 1 and 6 and luggage cannot be negative.")
        if wanted <= datetime.now():
            raise ApiError(400, "Please choose a future travel date and time.")
        gender = str(user.get("gender", "")).upper()
        preference = str(body["gender_preference"]).upper()
        if gender not in ("FEMALE", "MALE", "OTHER"):
            raise ApiError(400, "Your account must have a valid gender to form a travel group.")
        if preference not in ("SAME", "ANY"):
            raise ApiError(400, "Invalid gender preference.")

        requests = self._student_read("student_group_requests.json")
        roll = str(user["roll_number"]).upper()
        # Keep one active request per student; preserve older requests as history.
        for old in requests:
            if str(old.get("roll_number", "")).upper() == roll and old.get("status") == "ACTIVE":
                old["status"] = "SUPERSEDED"
        numeric_ids = []
        for item in requests:
            match = re.fullmatch(r"GROUP-(\d+)", str(item.get("request_id", "")))
            if match:
                numeric_ids.append(int(match.group(1)))
        request = {
            "request_id": f"GROUP-{max(numeric_ids, default=0) + 1:04d}",
            "student_name": user["name"],
            "roll_number": roll,
            "pickup_location": str(body["pickup_location"]).strip(),
            "destination": str(body["destination"]).strip(),
            "travel_time": wanted.strftime("%Y-%m-%d %H:%M"),
            "passengers": passengers,
            "luggage": luggage,
            "gender": gender,
            "gender_preference": preference,
            "status": "ACTIVE",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        requests.append(request)
        self._student_write("student_group_requests.json", requests)
        matches = self._student_group_matches(request, requests)
        group_links = self._student_read("student_group_join_requests.json")
        for match in matches:
            related = [g for g in group_links if {str(g.get("from_roll", "")).upper(), str(g.get("to_roll", "")).upper()} == {roll, str(match.get("roll_number", "")).upper()}]
            match["group_request_status"] = related[-1].get("status") if related else "NONE"
        return {
            "request": {k: request[k] for k in (
                "request_id", "pickup_location", "destination", "travel_time",
                "passengers", "luggage", "status"
            )},
            "student_matches": matches,
        }

    def student_group_matches(self, user):
        roll = str(user["roll_number"]).upper()
        requests = self._student_read("student_group_requests.json")
        mine = [r for r in requests if str(r.get("roll_number", "")).upper() == roll and r.get("status") == "ACTIVE"]
        if not mine:
            return {"request": None, "student_matches": []}
        request = mine[-1]
        try:
            wanted = datetime.strptime(request["travel_time"], "%Y-%m-%d %H:%M")
        except (KeyError, TypeError, ValueError):
            return {"request": None, "student_matches": []}
        if wanted <= datetime.now():
            request["status"] = "EXPIRED"
            self._student_write("student_group_requests.json", requests)
            return {"request": None, "student_matches": []}
        matches = self._student_group_matches(request, requests)
        group_links = self._student_read("student_group_join_requests.json")
        for match in matches:
            related = [g for g in group_links if {str(g.get("from_roll", "")).upper(), str(g.get("to_roll", "")).upper()} == {roll, str(match.get("roll_number", "")).upper()}]
            match["group_request_status"] = related[-1].get("status") if related else "NONE"
        return {
            "request": {k: request[k] for k in (
                "request_id", "pickup_location", "destination", "travel_time",
                "passengers", "luggage", "status"
            )},
            "student_matches": matches,
        }

    def student_send_group_request(self, target_request_id, user):
        requests = self._student_read("student_group_requests.json")
        roll = str(user.get("roll_number", "")).upper()
        own = next((r for r in reversed(requests) if str(r.get("roll_number", "")).upper() == roll and r.get("status") == "ACTIVE"), None)
        target = next((r for r in requests if r.get("request_id") == target_request_id and r.get("status") == "ACTIVE"), None)
        if not own or not target:
            raise ApiError(404, "An active travel group request could not be found.")
        if str(target.get("roll_number", "")).upper() == roll:
            raise ApiError(400, "You cannot request to join your own group.")
        compatible = {m["request_id"] for m in self._student_group_matches(own, requests)}
        if target_request_id not in compatible:
            raise ApiError(409, "These travel requests are no longer compatible.")
        links = self._student_read("student_group_join_requests.json")
        existing = next((g for g in reversed(links) if g.get("from_request_id") == own["request_id"] and g.get("to_request_id") == target_request_id and g.get("status") in ("PENDING", "ACCEPTED")), None)
        if existing:
            raise ApiError(409, "You have already sent a group request to this student.")
        gid = f"GREQ-{max([int(re.sub(r'\D', '', str(x.get('group_join_id','0'))) or 0) for x in links] + [0]) + 1:04d}"
        links.append({"group_join_id": gid, "from_request_id": own["request_id"], "to_request_id": target_request_id,
                      "from_roll": roll, "to_roll": str(target["roll_number"]).upper(),
                      "from_name": user.get("name", ""), "to_name": target.get("student_name", ""),
                      "status": "PENDING", "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        self._student_write("student_group_join_requests.json", links)
        return {"message": "Group request sent successfully.", "status": "PENDING"}

    def student_group_request_action(self, group_join_id, action, user):
        links = self._student_read("student_group_join_requests.json")
        item = next((g for g in links if g.get("group_join_id") == group_join_id), None)
        if not item:
            raise ApiError(404, "Group request not found.")
        roll = str(user.get("roll_number", "")).upper()
        if str(item.get("to_roll", "")).upper() != roll:
            raise ApiError(403, "Only the recipient can accept or reject this group request.")
        if item.get("status") != "PENDING":
            raise ApiError(409, "This group request has already been processed.")
        if action == "reject":
            item["status"] = "REJECTED"
            self._student_write("student_group_join_requests.json", links)
            return {"message": "Group request rejected.", "status": "REJECTED"}
        requests = self._student_read("student_group_requests.json")
        a = next((r for r in requests if r.get("request_id") == item.get("from_request_id") and r.get("status") == "ACTIVE"), None)
        b = next((r for r in requests if r.get("request_id") == item.get("to_request_id") and r.get("status") == "ACTIVE"), None)
        if not a or not b:
            raise ApiError(409, "One of the travel requests is no longer active.")
        if b["travel_time"] < datetime.now().strftime("%Y-%m-%d %H:%M"):
            raise ApiError(409, "This travel request has expired.")
        if b["request_id"] not in {m["request_id"] for m in self._student_group_matches(a, requests)}:
            raise ApiError(409, "These travel requests are no longer compatible.")
        members = [{"name": r.get("student_name", ""), "roll_number": str(r.get("roll_number", "")).upper(),
                    "passengers": int(r.get("passengers", 1)), "luggage": int(r.get("luggage", 0))} for r in (a, b)]
        pax = sum(m["passengers"] for m in members); luggage = sum(m["luggage"] for m in members)
        if pax > 6:
            raise ApiError(409, "The combined group exceeds the six-passenger limit.")
        catalog = VehicleCatalog(service.load(self.base).vehicles)
        option = catalog.cheapest_fitting(b["destination"], pax, luggage)
        recommendation = None
        if option:
            recommendation = {"vehicle_type": option.vehicle_type, "total_fare": float(option.total_fare),
                "fare_status": "FIXED_OR_LISTED", "fare_display": f"Rs {option.total_fare:,.2f}",
                "passenger_capacity": option.passenger_capacity, "luggage_capacity": option.luggage_capacity}
        else:
            unpriced = [v for v in catalog.unpriced_options(b["destination"]) if v.passenger_capacity >= pax and v.luggage_capacity >= luggage]
            if unpriced:
                v = unpriced[0]
                recommendation = {"vehicle_type": v.vehicle_type, "total_fare": None, "fare_status": "DRIVER_QUOTATION_REQUIRED",
                    "fare_display": "Fare depends on the driver's quotation.", "passenger_capacity": v.passenger_capacity, "luggage_capacity": v.luggage_capacity}
        group_id = f"GROUPRIDE-{datetime.now().strftime('%Y%m%d%H%M%S')}-{len(self._student_read('student_confirmed_groups.json'))+1}"
        groups = self._student_read("student_confirmed_groups.json")
        group = {"group_ride_id": group_id, "status": "CONFIRMED", "pickup_location": b["pickup_location"],
            "destination": b["destination"], "departure_time": b["travel_time"], "members": members,
            "passengers": pax, "luggage": luggage, "vehicle_recommendation": recommendation,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        groups.append(group); self._student_write("student_confirmed_groups.json", groups)
        item["status"] = "ACCEPTED"; item["group_ride_id"] = group_id
        for other_link in links:
            if other_link is item:
                continue
            if {str(other_link.get("from_roll", "")).upper(), str(other_link.get("to_roll", "")).upper()} == {str(item.get("from_roll", "")).upper(), str(item.get("to_roll", "")).upper()} and other_link.get("status") == "PENDING":
                other_link["status"] = "ALREADY_GROUPED"
                other_link["group_ride_id"] = group_id
        self._student_write("student_group_join_requests.json", links)
        a["status"] = "CONFIRMED"; b["status"] = "CONFIRMED"; a["group_ride_id"] = group_id; b["group_ride_id"] = group_id
        self._student_write("student_group_requests.json", requests)
        return {"message": "Group request accepted. The confirmed group ride is now in My Rides and My Upcoming Rides.", "status": "ACCEPTED", "group_ride_id": group_id}

    def student_cancel_offer(self, ride_id, user):
        rides = self._student_read("student_ride_offers.json")
        ride = next((r for r in rides if r.get("ride_id") == ride_id), None)
        if not ride:
            raise ApiError(404, "Cab offer not found.")
        if str(ride.get("roll_number", "")).upper() != str(user.get("roll_number", "")).upper():
            raise ApiError(403, "Only the owner can cancel this cab offer.")
        if ride.get("status") != "OPEN":
            raise ApiError(409, "This cab offer is no longer open.")
        ride["status"] = "CANCELLED"
        ride["cancelled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        joins = self._student_read("student_join_requests.json")
        cancelled = []
        for join in joins:
            if join.get("ride_id") == ride_id and join.get("status") in ("PENDING", "ACCEPTED", "CONFIRMED"):
                join["previous_status"] = join.get("status")
                join["status"] = "CANCELLED_BY_OWNER"
                join["cancellation_reason"] = "The cab owner cancelled this offer."
                join["cancelled_at"] = ride["cancelled_at"]
                cancelled.append(join.get("join_id"))
        self._student_write("student_ride_offers.json", rides)
        self._student_write("student_join_requests.json", joins)
        return {
            "message": "Your cab offer has been cancelled.",
            "status": "CANCELLED",
            "cancelled_pending_requests": len(cancelled),
        }

    def student_join(self, body, user=None):
        ride_id = str(body.get("ride_id", "")).strip()
        roll = user["roll_number"] if user else str(body.get("request_roll_number", "")).strip()

        if not ride_id or not roll:
            raise ApiError(400, "Missing ride or roll number.")

        rides = self._student_read("student_ride_offers.json")
        ride = next(
            (r for r in rides
             if r["ride_id"] == ride_id and r["status"] == "OPEN"),
            None
        )

        if not ride:
            raise ApiError(404, "Ride not found.")

        if ride["roll_number"].casefold() == roll.casefold():
            raise ApiError(400, "You cannot join your own ride.")

        joins = self._student_read("student_join_requests.json")

        if any(
            j["ride_id"] == ride_id
            and j["request_roll_number"].casefold() == roll.casefold()
            and j["status"] in ("PENDING", "CONFIRMED")
            for j in joins
        ):
            raise ApiError(409, "You already requested this ride.")

        passengers = int(body.get("passengers", 0))
        luggage = int(body.get("luggage", 0))

        reserved = sum(
            j["passengers"] for j in joins
            if j["ride_id"] == ride_id
            and j["status"] in ("PENDING", "ACCEPTED", "CONFIRMED")
        )
        reserved_luggage = sum(
            j.get("luggage", 0) for j in joins
            if j["ride_id"] == ride_id
            and j["status"] in ("PENDING", "ACCEPTED", "CONFIRMED")
        )

        if passengers < 1 or luggage < 0:
            raise ApiError(400, "Invalid passenger or luggage count.")

        if (
            ride["available_seats"] - reserved < passengers
            or ride["luggage_capacity"] - reserved_luggage < luggage
        ):
            raise ApiError(409, "Not enough seats or luggage capacity.")

        join = {
            "join_id": f"JOIN-{len(joins)+1:04d}",
            "ride_id": ride_id,
            "owner_roll_number": ride["roll_number"],
            "request_roll_number": roll,
            "request_name": body.get("request_name", ""),
            "request_contact": body.get("request_contact", ""),
            "passengers": passengers,
            "luggage": luggage,
            "status": "PENDING"
        }

        joins.append(join)
        self._student_write("student_join_requests.json", joins)

        return {"message": "Join request sent."}

    def get_data(self, body):
        cfg = self.base
        ds = load_dataset_with_submissions(cfg)
        submitted = {d["request_id"] for d in _read_submitted(cfg)}
        return {
            "requests": [
                request_payload(r, submitted) for r in ds.requests
            ],
            "rides": [
                {
                    **{
                        k: getattr(r, k) for k in (
                            "ride_id", "booked_by", "pickup_location",
                            "destination", "departure_time", "vehicle_type",
                            "total_fare", "passenger_capacity",
                            "luggage_capacity", "booked_passengers",
                            "booked_luggage", "booker_gender",
                            "booker_gender_preference"
                        )
                    },
                    "remaining_seats": r.remaining_seats,
                    "remaining_luggage": r.remaining_luggage
                }
                for r in ds.rides
            ],
            "vehicles": [
                {
                    k: getattr(v, k) for k in (
                        "vehicle_type", "destination",
                        "passenger_capacity", "luggage_capacity",
                        "total_fare"
                    )
                }
                for v in ds.vehicles
            ],
            "issues": [vars(i) for i in ds.issues],
            "invalid_requests": ds.invalid_requests,
            "invalid_rides": ds.invalid_rides,
            "ride_gender_columns_present": ds.ride_gender_columns_present
        }

    def post_requests(self, body, user=None):
        _validate_route_locations(
            body.get("pickup_location"),
            body.get("destination")
        )
        cfg = self.base
        ds = load_dataset_with_submissions(cfg)
        book = RequestBook(ds.requests)

        try:
            solo = body.get("solo_fare")
            req = submit_request(
                book,
                request_id=str(body.get("request_id", "")).strip(),
                student_name=str(body.get("student_name", "")).strip(),
                pickup_location=str(body.get("pickup_location", "")).strip(),
                destination=str(body.get("destination", "")).strip(),
                earliest=_parse_dt(
                    body.get("earliest_departure"), "earliest_departure"
                ),
                latest=_parse_dt(
                    body.get("latest_departure"), "latest_departure"
                ),
                passengers=int(body.get("passengers", 0)),
                luggage=int(body.get("luggage", 0)),
                gender=body.get("gender") or None,
                gender_preference=body.get("gender_preference") or None,
                solo_fare=Decimal(str(solo)) if solo not in (None, "") else None
            )
        except (ValueError, ArithmeticError) as exc:
            raise ApiError(400, str(exc)) from exc

        if any(
            k.split("@")[0] == req.request_id for k in ds.invalid_requests
        ):
            raise ApiError(
                400,
                f"request {req.request_id} already exists (invalid CSV record)"
            )

        record = {
            "request_id": req.request_id,
            "roll_number": user["roll_number"] if user else str(body.get("roll_number", "")),
            "student_name": req.student_name,
            "pickup_location": req.pickup_location,
            "destination": req.destination,
            "earliest_departure": req.earliest_departure.strftime(
                "%Y-%m-%d %H:%M"
            ),
            "latest_departure": req.latest_departure.strftime(
                "%Y-%m-%d %H:%M"
            ),
            "passengers": req.passengers,
            "luggage": req.luggage,
            "gender": req.gender,
            "gender_preference": req.gender_preference,
            "solo_fare": (
                str(req.solo_fare)
                if req.solo_fare is not None else None
            )
        }

        items = _read_submitted(cfg) + [record]
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        _submitted_file(cfg).write_text(
            json.dumps(items, indent=2), encoding="utf-8"
        )

        note = None
        if not VehicleCatalog(ds.vehicles).has_destination(req.destination):
            note = (
                f"'{req.destination}' is not in vehicle_options.csv. "
                "The request is accepted; vehicle and fare will need "
                "manual confirmation."
            )

        return {
            "request": request_payload(req, {req.request_id}),
            "note": note
        }

    def delete_request(self, request_id):
        cfg = self.base
        active = [
            b for b in BookingManager(
                load_dataset_with_submissions(cfg), cfg
            ).list_bookings()
            if b["request_id"] == request_id
            and b["status"] != "CANCELLED"
        ]

        if active:
            raise ApiError(
                409,
                f"{request_id} has active booking "
                f"{active[0]['booking_id']}; cancel it before deleting "
                "the request"
            )

        items = _read_submitted(cfg)
        kept = [d for d in items if d["request_id"] != request_id]

        if len(kept) == len(items):
            raise ApiError(
                404,
                f"{request_id} is not a UI-submitted request "
                "(CSV requests cannot be deleted from the UI)"
            )

        _submitted_file(cfg).write_text(
            json.dumps(kept, indent=2), encoding="utf-8"
        )
        return {"deleted": request_id}

    def post_solve(self, body):
        cfg = self._cfg(body)
        algorithm = body.get("algorithm")

        if algorithm not in ("greedy", "ip"):
            raise ApiError(400, "algorithm must be 'greedy' or 'ip'")

        ds = load_dataset_with_submissions(cfg)
        sol = (run_greedy if algorithm == "greedy" else run_ip)(ds, cfg)
        errors = verify_solution(sol, ds, cfg)

        export_solution(sol, ds, cfg, errors, cfg.output_dir, algorithm)
        (
            cfg.output_dir / "validation_report.md"
        ).write_text(validation_markdown(ds), encoding="utf-8")

        return solution_payload(sol, ds, errors)

    def post_compare(self, body):
        cfg = self._cfg(body)
        ds = load_dataset_with_submissions(cfg)

        g, i = run_greedy(ds, cfg), run_ip(ds, cfg)
        ge, ie = verify_solution(g, ds, cfg), verify_solution(i, ds, cfg)

        export_solution(g, ds, cfg, ge, cfg.output_dir, "greedy")
        export_solution(i, ds, cfg, ie, cfg.output_dir, "ip")

        md = comparison_markdown(g, i, ds, ge, ie)
        (
            cfg.output_dir / "comparison_report.md"
        ).write_text(md, encoding="utf-8")

        gp, ip = solution_payload(g, ds, ge), solution_payload(i, ds, ie)
        diff = g.new_cab_cost - i.new_cab_cost
        pct = float(diff / g.new_cab_cost) if g.new_cab_cost else None

        return {
            "greedy": gp,
            "ip": ip,
            "cost_difference": diff,
            "percentage_saving": pct,
            "served_difference": len(i.served_ids) - len(g.served_ids),
            "markdown": md
        }

    def post_sensitivity(self, body):
        cfg = self._cfg(body)
        ds = load_dataset_with_submissions(cfg)
        result = run_sensitivity(ds, cfg)
        md = sensitivity_markdown(result)

        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        (
            cfg.output_dir / "sensitivity_report.md"
        ).write_text(md, encoding="utf-8")

        lp = result["lp"]
        assign = {
            k[len("assign_"):]: v
            for k, v in lp["duals"].items()
            if k.startswith("assign_")
        }
        capacity = {
            k: v for k, v in lp["duals"].items()
            if k.startswith(("seats_", "luggage_"))
        }

        return {
            "base": result["base"],
            "base_status": result["base_status"],
            "rows": result["rows"],
            "markdown": md,
            "lp": {
                k: lp[k] for k in (
                    "status", "objective", "penalty",
                    "cost", "unserved", "size"
                )
            },
            "ip_penalty_objective": result["ip_penalty_objective"],
            "assignment_duals": assign,
            "capacity_duals": capacity
        }

    def get_bookings(self, body):
        cfg = self._cfg(body)
        mgr = self._manager(cfg, body)

        ride_ids = (
            [r.ride_id for r in mgr.dataset.rides]
            + list(mgr.state["registered_rides"])
        )

        rides = []
        for rid in ride_ids:
            ride = mgr._ride(rid)
            rides.append({
                "ride_id": rid,
                "kind": ride["kind"],
                "destination": ride["destination"],
                "pickup_location": ride["pickup_location"],
                "departure_time": ride["departure_time"],
                "vehicle_type": ride["vehicle_type"],
                "total_fare": ride["total_fare"],
                "fare_status": ride["fare_status"],
                **mgr.capacity(rid)
            })

        bookings = []
        for b in mgr.list_bookings():
            deadline = mgr.cancellation_deadline(b["booking_id"])
            bookings.append({
                **b,
                "cancellation_deadline": deadline,
                "student_name": (
                    mgr.requests[b["request_id"]].student_name
                    if b["request_id"] in mgr.requests else ""
                )
            })

        return {
            "rides": rides,
            "bookings": bookings,
            "now": mgr.clock(),
            "deadline_hours": cfg.cancellation_deadline_hours
        }

    def post_booking(self, body):
        cfg = self._cfg(body)
        return self._manager(cfg, body).create_booking(
            body.get("request_id", ""),
            body.get("ride_id", "")
        )

    def booking_action(self, booking_id, action, body):
        cfg = self._cfg(body)
        mgr = self._manager(cfg, body)

        return (
            mgr.confirm_booking(booking_id)
            if action == "confirm"
            else mgr.cancel_booking(booking_id)
        )

    def post_register(self, body, book: bool):
        cfg = self._cfg(body)
        algorithm = body.get("algorithm")

        if algorithm not in ("greedy", "ip"):
            raise ApiError(400, "algorithm must be 'greedy' or 'ip'")

        path = cfg.output_dir / f"{algorithm}_solution.json"

        if not path.exists():
            raise ApiError(
                400,
                f"No saved {algorithm} solution yet - "
                "run it on the Optimise tab first"
            )

        mgr = self._manager(cfg, body)

        if book:
            created, errors = mgr.book_solution(path)
            return {"created": len(created), "refused": errors}

        return {"registered": mgr.register_solution(path)}

    def get_report(self, name):
        if name not in REPORTS:
            raise ApiError(404, f"unknown report {name}")

        path = self.base.output_dir / f"{name}_report.md"

        if not path.exists():
            raise ApiError(
                404, f"{name} report has not been generated yet"
            )

        return path.read_text(encoding="utf-8")

def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "IITPRideShare/1.0"

        def log_message(self, fmt, *args):
            print(
                f"[{self.log_date_time_string()}] {fmt % args}"
            )

        def _send(
            self,
            status,
            payload,
            content_type="application/json; charset=utf-8",
            headers=None
        ):
            data = payload if isinstance(payload, bytes) else (
                payload.encode("utf-8")
                if isinstance(payload, str)
                and not content_type.startswith("application/json")
                else json.dumps(
                    payload, default=_json_default
                ).encode("utf-8")
            )

            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")

            if headers:
                for name, value in headers.items():
                    self.send_header(name, value)

            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)

            if not length:
                return {}

            try:
                return json.loads(
                    self.rfile.read(length).decode("utf-8")
                )
            except json.JSONDecodeError as exc:
                raise ApiError(
                    400,
                    f"Request body is not valid JSON: {exc}"
                ) from exc

        def _get_token(self):
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))

            return (
                cookie["session"].value
                if "session" in cookie else None
            )

        def _dispatch(self, method):
            path = unquote(urlparse(self.path).path)

            try:
                if not path.startswith("/api/"):
                    if method != "GET":
                        raise ApiError(405, "method not allowed")
                    return self._static(path)

                body = self._body() if method == "POST" else {}

                # REGISTER
                                # REGISTER
                if path == "/api/auth/register" and method == "POST":
                    user = auth.register(
                        body.get("name", ""),
                        body.get("roll_number", ""),
                        body.get("password", ""),
                        body.get("contact_number", ""),
                        body.get("gender", "")
                    )

                    token = auth.create_session(user["roll_number"])

                    self._send(
                        201,
                        {"user": user},
                        headers={
                            "Set-Cookie": (
                                f"session={token}; HttpOnly; "
                                "SameSite=Lax; Path=/; Max-Age=604800"
                            )
                        }
                    )
                    return

                # LOGIN
                if path == "/api/auth/login" and method == "POST":
                    user = auth.authenticate(
                        body.get("roll_number", ""),
                        body.get("password", "")
                    )

                    token = auth.create_session(user["roll_number"])

                    self._send(
                        200,
                        {"user": user},
                        headers={
                            "Set-Cookie": (
                                f"session={token}; HttpOnly; "
                                "SameSite=Lax; Path=/; Max-Age=604800"
                            )
                        }
                    )
                    return

                # CURRENT USER
                if path == "/api/auth/me" and method == "GET":
                    token = self._get_token()
                    user = (
                        auth.get_session_user(token)
                        if token else None
                    )

                    if not user:
                        self._send(
                            401, {"error": "Not logged in"}
                        )
                        return

                    self._send(200, {"user": user})
                    return

                # LOGOUT
                if path == "/api/auth/logout" and method == "POST":
                    token = self._get_token()

                    if token:
                        auth.delete_session(token)

                    self._send(
                        200,
                        {"message": "Logged out successfully"},
                        headers={
                            "Set-Cookie": (
                                "session=; HttpOnly; "
                                "SameSite=Lax; Path=/; Max-Age=0"
                            )
                        }
                    )
                    return

                # Student-specific routes always derive identity from the HttpOnly session.
                student_paths = ("/api/student/", "/api/my-upcoming", "/api/my-rides", "/api/requests")
                user = None
                if path.startswith(student_paths):
                    token = self._get_token()
                    user = auth.get_session_user(token) if token else None
                    if not user:
                        raise ApiError(401, "Please log in to continue.")

                # EXISTING API ROUTES
                parts = path.strip("/").split("/")[1:]

                with LOCK:
                    result = self._route(method, parts, body, user)

                if isinstance(result, str):
                    return self._send(
                        200,
                        result,
                        "text/markdown; charset=utf-8"
                    )

                self._send(200, result)

            except auth.AuthError as exc:
                self._send(400, {"error": str(exc)})

            except ApiError as exc:
                self._send(exc.status, {"error": str(exc)})

            except BookingError as exc:
                self._send(409, {"error": str(exc)})

            except DataValidationError as exc:
                self._send(
                    422,
                    {
                        "error": "Input data cannot be used",
                        "details": exc.messages
                    }
                )

            except Exception as exc:
                self._send(
                    500,
                    {"error": f"{type(exc).__name__}: {exc}"}
                )

        def _route(self, method, parts, body, user=None):
            match method, parts:
                case "GET", ["my-rides"]:
                    return api.student_my_rides(user)

                case "GET", ["my-upcoming"]:
                    return api.student_upcoming(user)

                case "POST", ["my-upcoming", participation_id, "cancel"]:
                    return api.cancel_student_participation(participation_id, user)

                case "POST", ["student", "join-requests", join_id, action] if action in ("accept", "reject"):
                    return api.owner_join_action(join_id, action, user)

                case "POST", ["student", "search"]:
                    body = {**body, "student_name": user["name"], "roll_number": user["roll_number"],
                            "contact_number": user.get("contact_number", ""), "gender": user.get("gender", "")}
                    return api.student_search(body)

                case "POST", ["student", "offers"]:
                    return api.student_post_offer(body, user)

                case "POST", ["student", "offers", ride_id, "cancel"]:
                    return api.student_cancel_offer(ride_id, user)

                case "POST", ["student", "group-requests"]:
                    return api.student_form_group(body, user)

                case "GET", ["student", "group-matches"]:
                    return api.student_group_matches(user)

                case "POST", ["student", "group-requests", target_request_id, "send"]:
                    return api.student_send_group_request(target_request_id, user)

                case "POST", ["student", "group-join-requests", group_join_id, action] if action in ("accept", "reject"):
                    return api.student_group_request_action(group_join_id, action, user)

                case "GET", ["student", "profile", roll_number]:
                    return api.student_profile(roll_number, user)

                case "POST", ["student", "join"]:
                    body = {**body, "request_name": user["name"], "request_contact": user.get("contact_number", "")}
                    return api.student_join(body, user)

                case "GET", ["health"]:
                    return {"ok": True}

                case "GET", ["data"]:
                    return api.get_data(body)

                case "POST", ["requests"]:
                    return api.post_requests(body, user)

                case "DELETE", ["requests", rid]:
                    return api.delete_request(rid)

                case "POST", ["solve"]:
                    return api.post_solve(body)

                case "POST", ["compare"]:
                    return api.post_compare(body)

                case "POST", ["sensitivity"]:
                    return api.post_sensitivity(body)

                case "POST", ["bookings", "state"]:
                    return api.get_bookings(body)

                case "POST", ["bookings"]:
                    return api.post_booking(body)

                case "POST", ["bookings", "register"]:
                    return api.post_register(body, book=False)

                case "POST", ["bookings", "book-solution"]:
                    return api.post_register(body, book=True)

                case "POST", [
                    "bookings",
                    bid,
                    ("confirm" | "cancel") as action
                ]:
                    return api.booking_action(bid, action, body)

                case "GET", ["reports", name]:
                    return api.get_report(name)

            raise ApiError(
                404,
                f"no API route {method} /api/{'/'.join(parts)}"
            )

        def _static(self, path):
            rel = (
                "index.html"
                if path in ("", "/")
                else path.lstrip("/")
            )

            target = (FRONTEND / rel).resolve()

            if (
                FRONTEND.resolve() not in target.parents
                or not target.is_file()
            ):
                raise ApiError(404, "not found")

            ctype = (
                STATIC_TYPES.get(target.suffix)
                or mimetypes.guess_type(target.name)[0]
                or "application/octet-stream"
            )

            data = target.read_bytes()
            if target.name == "index.html":
                # Strip a UTF-8 BOM and leading whitespace before serving HTML.
                data = data.removeprefix(b"\xef\xbb\xbf").lstrip()
                if data[:15].lower() == b"<!doctype html>":
                    data = b"<!doctype html>" + data[15:]
            self._send(200, data, ctype)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def make_server(
    cfg: Config,
    host: str = "127.0.0.1",
    port: int = 8000
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(
        (host, port),
        make_handler(Api(cfg))
    )


def serve(
    cfg: Config,
    host: str | None = None,
    port: int | None = None
) -> None:
    host = host or "0.0.0.0"
    port = port or int(os.environ.get("PORT", "8000"))

    httpd = make_server(cfg, host, port)

    print(
        f"IITP OptiRide running at "
        f"http://{host}:{port} (Ctrl+C to stop)"
    )

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
