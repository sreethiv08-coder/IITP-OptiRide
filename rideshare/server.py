"""Web UI server and JSON API, built only on Python's standard library.

    python -m rideshare serve -> http://127.0.0.1:8000

Serves the static frontend and JSON API.
"""
from __future__ import annotations

import json
import mimetypes
import threading
from dataclasses import replace
from datetime import datetime
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

FRONTEND = PROJECT_ROOT / "frontend"
REPORTS = {"validation", "greedy", "ip", "comparison", "sensitivity"}
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml"
}
LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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

    def student_post_offer(self, body):
        required = [
            "student_name", "roll_number", "contact_number",
            "pickup_location", "destination", "travel_date",
            "departure_time", "vehicle_type", "gender",
            "gender_preference"
        ]

        if any(not str(body.get(k, "")).strip() for k in required):
            raise ApiError(400, "Please fill all required fields.")

        if body.get("cab_booked") != "yes":
            raise ApiError(
                400, "You must confirm that the cab is already booked."
            )

        try:
            capacity = int(body["passenger_capacity"])
            seats = int(body["available_seats"])
            luggage = int(body["luggage_capacity"])
            fare = Decimal(str(body["total_fare"]))
            departure = datetime.strptime(
                body["travel_date"] + " " + body["departure_time"],
                "%Y-%m-%d %H:%M"
            )
        except (KeyError, ValueError, ArithmeticError):
            raise ApiError(400, "Please enter valid ride details.")

        if (
            capacity < 1 or seats < 1 or seats > capacity
            or luggage < 0 or not fare.is_finite() or fare <= 0
        ):
            raise ApiError(
                400, "Please check the capacity, seats, luggage and fare."
            )

        if body["gender"].upper() not in ("FEMALE", "MALE", "OTHER"):
            raise ApiError(400, "Invalid gender.")

        if body["gender_preference"].upper() not in ("SAME", "ANY"):
            raise ApiError(400, "Invalid gender preference.")

        rides = self._student_read("student_ride_offers.json")

        ride = {
            "ride_id": f"STU-RIDE-{len(rides)+1:04d}",
            "student_name": body["student_name"].strip(),
            "roll_number": body["roll_number"].strip(),
            "contact_number": body["contact_number"].strip(),
            "pickup_location": body["pickup_location"].strip(),
            "destination": body["destination"].strip(),
            "departure_time": departure.strftime("%Y-%m-%d %H:%M"),
            "vehicle_type": body["vehicle_type"].strip(),
            "passenger_capacity": capacity,
            "available_seats": seats,
            "luggage_capacity": luggage,
            "total_fare": float(fare),
            "gender": body["gender"].upper(),
            "gender_preference": body["gender_preference"].upper(),
            "status": "OPEN"
        }

        rides.append(ride)
        self._student_write("student_ride_offers.json", rides)

        return {"ride": {"ride_id": ride["ride_id"]}}

    def student_search(self, body):
        required = [
            "student_name", "roll_number", "contact_number",
            "pickup_location", "destination", "travel_date",
            "travel_time", "gender", "gender_preference"
        ]

        if any(not str(body.get(k, "")).strip() for k in required):
            raise ApiError(400, "Please fill all required fields.")

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

            if ride["luggage_capacity"] < luggage:
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

            matches.append({
                "ride_id": ride["ride_id"],
                "student_name": ride["student_name"],
                "pickup_location": ride["pickup_location"],
                "destination": ride["destination"],
                "departure_time": ride["departure_time"],
                "vehicle_type": ride["vehicle_type"],
                "available_seats": ride["available_seats"],
                "luggage_capacity": ride["luggage_capacity"],
                "estimated_share_per_person": round(
                    ride["total_fare"] / ride["passenger_capacity"], 2
                )
            })

        return {"matches": matches}

    def student_join(self, body):
        ride_id = str(body.get("ride_id", "")).strip()
        roll = str(body.get("request_roll_number", "")).strip()

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
            and j["status"] in ("PENDING", "CONFIRMED")
        )

        if passengers < 1 or luggage < 0:
            raise ApiError(400, "Invalid passenger or luggage count.")

        if (
            ride["available_seats"] - reserved < passengers
            or ride["luggage_capacity"] < luggage
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

    def post_requests(self, body):
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

                # EXISTING API ROUTES
                parts = path.strip("/").split("/")[1:]

                with LOCK:
                    result = self._route(method, parts, body)

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

        def _route(self, method, parts, body):
            match method, parts:
                case "POST", ["student", "search"]:
                    return api.student_search(body)

                case "POST", ["student", "offers"]:
                    return api.student_post_offer(body)

                case "POST", ["student", "join"]:
                    return api.student_join(body)

                case "GET", ["health"]:
                    return {"ok": True}

                case "GET", ["data"]:
                    return api.get_data(body)

                case "POST", ["requests"]:
                    return api.post_requests(body)

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

            self._send(200, target.read_bytes(), ctype)

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
    host: str = "127.0.0.1",
    port: int = 8000
) -> None:
    httpd = make_server(cfg, host, port)

    print(
        f"IITP RideShare UI running at "
        f"http://{host}:{port} (Ctrl+C to stop)"
    )

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
