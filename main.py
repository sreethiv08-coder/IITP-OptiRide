"""IITP RideShare - baseline backend (Python standard library only).

Two separate workflows:

  Workflow 1  A cab is already booked.  Find other students' requests that can
              share it without exceeding its remaining seats or luggage space.
  Workflow 2  Cabs are not booked yet.  Group compatible requests, then pick the
              cheapest vehicle (from vehicle_options.csv) that fits each group.

Run (PowerShell, inside the OTProject_RideShare folder):

    python main.py                 # both workflows
    python main.py --workflow 1    # only Workflow 1
    python main.py --workflow 2    # only Workflow 2

CONSTRAINTS ENFORCED (hard, checked again by verify_* functions)
  * seats:   booked + assigned passengers <= passenger capacity
  * luggage: booked + assigned bags       <= luggage capacity
  * route:   same pickup_location and destination (case/space-insensitive)
  * time:    Workflow 1 - ride departure lies inside the request window
             Workflow 2 - all requests in a group share a common time window
  * every request is assigned to at most one ride / group
  * Workflow 2 only picks a vehicle whose seats AND luggage fit the group

HEURISTIC DECISIONS (not guaranteed optimal)
  * Workflow 1: rides are processed one at a time (earliest departure first).
    For each single ride the best subset is found by exact search when there
    are <= EXHAUSTIVE_LIMIT compatible requests, but the sequence of rides is
    greedy, so the overall assignment is not globally optimal.
  * Workflow 2: requests are inserted greedily (largest first) into the group
    where the cheapest-vehicle fare rises least.  This is first-fit style
    bin packing, a heuristic.  Within a finished group the vehicle choice is
    exact (cheapest feasible vehicle).

FARE RULES (assumptions)
  * Fares are the sample numbers in the CSV files, treated as INR.
  * A cab's total fare is split in proportion to passengers (equal price per
    seat), in whole paise; leftover paise go to the largest fractional
    remainders, ties by listing order, so shares add up exactly to the total.
  * "Savings" = solo_fare - fare share, shown ONLY when the request has a
    valid solo_fare in trip_requests.csv.  Otherwise it prints N/A.  Solo
    fares are never guessed.

TIME COMPLEXITY  (R requests, K rides, V vehicle options, c = compatible
requests for one ride)
  * Reading/validation:        O(R + K + V)
  * Workflow 1 per ride:       O(R) filtering + O(2^c * c) exact search when
                               c <= 16 (else O(c log c) greedy), so
                               O(K * (R + 2^16 * 16)) in the worst case
  * Workflow 2:                O(R log R) sorting, then each request tries each
                               open group in its bucket and scans V vehicles:
                               O(R * G * V) <= O(R^2 * V) worst case

LIMITATIONS
  * No travel-time / detour model: pickups are matched by exact name only.
  * Fare split is per-passenger, not per-distance.
  * Workflow 2 heuristic can miss cheaper groupings an IP model would find.
  * Workflow 1 processes rides sequentially, so an earlier ride can take a
    request that a later ride would have served better.
"""

import argparse
import csv
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Configuration and CSV schemas
# --------------------------------------------------------------------------

REQUEST_FILE = "trip_requests.csv"
RIDES_FILE = "available_rides.csv"
VEHICLES_FILE = "vehicle_options.csv"

DATETIME_FORMAT = "%Y-%m-%d %H:%M"
CURRENCY = "INR"
EXHAUSTIVE_LIMIT = 16

REQUEST_REQUIRED = [
    "request_id", "student_name", "pickup_location", "destination",
    "earliest_departure", "latest_departure", "passengers", "luggage",
]
REQUEST_OPTIONAL = ["solo_fare"]
RIDE_REQUIRED = [
    "ride_id", "booked_by", "pickup_location", "destination",
    "departure_time", "vehicle_type", "total_fare", "passenger_capacity",
    "luggage_capacity", "booked_passengers", "booked_luggage",
]
VEHICLE_REQUIRED = [
    "vehicle_type", "destination", "passenger_capacity",
    "luggage_capacity", "total_fare",
]


class DataError(Exception):
    """Raised when input files are missing, empty or contain invalid records."""

    def __init__(self, messages: Sequence[str]):
        self.messages = list(messages)
        super().__init__("; ".join(self.messages))


class InternalError(Exception):
    """Raised if a verification check fails (would indicate a bug)."""


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

def norm(text: str) -> str:
    """Normalise a place/vehicle name for comparison."""
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class Request:
    request_id: str
    student_name: str
    pickup: str
    destination: str
    earliest: datetime
    latest: datetime
    passengers: int
    luggage: int
    solo_fare: Optional[Decimal]

    @property
    def pickup_key(self) -> str:
        return norm(self.pickup)

    @property
    def destination_key(self) -> str:
        return norm(self.destination)


@dataclass(frozen=True)
class Ride:
    ride_id: str
    booked_by: str
    pickup: str
    destination: str
    departure: datetime
    vehicle_type: str
    total_fare: Decimal
    passenger_capacity: int
    luggage_capacity: int
    booked_passengers: int
    booked_luggage: int

    @property
    def pickup_key(self) -> str:
        return norm(self.pickup)

    @property
    def destination_key(self) -> str:
        return norm(self.destination)


@dataclass(frozen=True)
class Vehicle:
    vehicle_type: str
    destination: str
    passenger_capacity: int
    luggage_capacity: int
    total_fare: Decimal

    @property
    def destination_key(self) -> str:
        return norm(self.destination)


@dataclass
class RideMatch:
    ride: Ride
    seats_before: int
    luggage_before: int
    riders: List[Request]
    method: str


@dataclass
class Workflow1Result:
    matches: List[RideMatch]
    unassigned: List[Request]
    remaining: Dict[str, Tuple[int, int]]


@dataclass
class TripGroup:
    requests: List[Request]
    vehicle: Vehicle
    window_start: datetime
    window_end: datetime
    group_id: str = ""

    @property
    def passengers(self) -> int:
        return sum(r.passengers for r in self.requests)

    @property
    def luggage(self) -> int:
        return sum(r.luggage for r in self.requests)


@dataclass
class Workflow2Result:
    groups: List[TripGroup]
    unmatched: List[Tuple[Request, str]]


# --------------------------------------------------------------------------
# Reading and validating CSV files
# --------------------------------------------------------------------------

def read_table(path: str, required: Sequence[str],
               optional: Sequence[str] = ()) -> List[Tuple[int, Dict[str, str]]]:
    """Read a CSV file into (line_number, row_dict) pairs.

    Raises DataError for missing/empty files, missing columns, duplicate
    headers, header-only files and rows with the wrong number of fields.
    """
    label = os.path.basename(path)
    expected_header = ",".join(list(required) + list(optional))
    if not os.path.isfile(path):
        raise DataError([f"{label}: file not found: {path}"])
    raw_rows: List[Tuple[int, List[str]]] = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            for row in reader:
                if row:  # only completely blank lines are skipped
                    raw_rows.append((reader.line_num, row))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise DataError([f"{label}: cannot read file ({exc})"])
    if not raw_rows:
        raise DataError([f"{label}: file is empty (expected header: {expected_header})"])

    header = [h.strip() for h in raw_rows[0][1]]
    problems: List[str] = []
    duplicated = sorted({h for h in header if header.count(h) > 1})
    if duplicated:
        problems.append(f"{label}: duplicate column name(s): {', '.join(duplicated)}")
    missing = [c for c in required if c not in header]
    if missing:
        problems.append(f"{label}: missing required column(s): {', '.join(missing)} "
                        f"(expected header: {expected_header})")
    if problems:
        raise DataError(problems)

    data_rows = raw_rows[1:]
    if not data_rows:
        raise DataError([f"{label}: contains a header but no data rows"])

    rows: List[Tuple[int, Dict[str, str]]] = []
    for line_no, cells in data_rows:
        if len(cells) != len(header):
            problems.append(f"{label} line {line_no}: expected {len(header)} fields "
                            f"but found {len(cells)}")
            continue
        rows.append((line_no, {h: c.strip() for h, c in zip(header, cells)}))
    if problems:
        raise DataError(problems)
    return rows


class RowParser:
    """Parses fields of one CSV row and collects every problem found."""

    INT_PATTERN = re.compile(r"[+-]?[0-9]+", re.ASCII)
    MONEY_PATTERN = re.compile(r"[0-9]+(\.[0-9]{1,2})?", re.ASCII)

    def __init__(self, row: Dict[str, str]):
        self.row = row
        self.problems: List[str] = []

    def text(self, column: str) -> str:
        value = self.row.get(column, "")
        if not value:
            self.problems.append(f"{column} is empty")
        return value

    def integer(self, column: str, minimum: int) -> int:
        raw = self.row.get(column, "")
        if not self.INT_PATTERN.fullmatch(raw):
            self.problems.append(f"{column} must be a whole number, got '{raw}'")
            return 0
        value = int(raw)
        if value < minimum:
            self.problems.append(f"{column} must be >= {minimum}, got {value}")
        return value

    def money(self, column: str, optional: bool = False) -> Optional[Decimal]:
        raw = self.row.get(column, "")
        if raw == "" and optional:
            return None
        if not self.MONEY_PATTERN.fullmatch(raw) or Decimal(raw) <= 0:
            self.problems.append(f"{column} must be a positive amount with at most "
                                 f"2 decimal places (e.g. 1100.00), got '{raw}'")
            return None if optional else Decimal(0)
        return Decimal(raw)

    def moment(self, column: str) -> Optional[datetime]:
        raw = self.row.get(column, "")
        try:
            return datetime.strptime(raw, DATETIME_FORMAT)
        except ValueError:
            self.problems.append(f"{column} must be a valid date-time like "
                                 f"2026-11-05 06:30, got '{raw}'")
            return None

    def raise_if_problems(self) -> None:
        if self.problems:
            raise ValueError("; ".join(self.problems))


def build_request(row: Dict[str, str]) -> Request:
    p = RowParser(row)
    request_id = p.text("request_id")
    name = p.text("student_name")
    pickup = p.text("pickup_location")
    destination = p.text("destination")
    earliest = p.moment("earliest_departure")
    latest = p.moment("latest_departure")
    passengers = p.integer("passengers", 1)
    luggage = p.integer("luggage", 0)
    solo_fare = p.money("solo_fare", optional=True)
    if earliest and latest and earliest > latest:
        p.problems.append("earliest_departure is after latest_departure")
    p.raise_if_problems()
    return Request(request_id, name, pickup, destination, earliest, latest,
                   passengers, luggage, solo_fare)


def build_ride(row: Dict[str, str]) -> Ride:
    p = RowParser(row)
    ride_id = p.text("ride_id")
    booked_by = p.text("booked_by")
    pickup = p.text("pickup_location")
    destination = p.text("destination")
    departure = p.moment("departure_time")
    vehicle_type = p.text("vehicle_type")
    total_fare = p.money("total_fare")
    seats = p.integer("passenger_capacity", 1)
    bags = p.integer("luggage_capacity", 0)
    booked_p = p.integer("booked_passengers", 1)
    booked_l = p.integer("booked_luggage", 0)
    if not p.problems:
        if booked_p > seats:
            p.problems.append(f"booked_passengers ({booked_p}) exceeds "
                              f"passenger_capacity ({seats})")
        if booked_l > bags:
            p.problems.append(f"booked_luggage ({booked_l}) exceeds "
                              f"luggage_capacity ({bags})")
    p.raise_if_problems()
    return Ride(ride_id, booked_by, pickup, destination, departure, vehicle_type,
                total_fare, seats, bags, booked_p, booked_l)


def build_vehicle(row: Dict[str, str]) -> Vehicle:
    p = RowParser(row)
    vehicle_type = p.text("vehicle_type")
    destination = p.text("destination")
    seats = p.integer("passenger_capacity", 1)
    bags = p.integer("luggage_capacity", 0)
    fare = p.money("total_fare")
    p.raise_if_problems()
    return Vehicle(vehicle_type, destination, seats, bags, fare)


def build_records(rows: List[Tuple[int, Dict[str, str]]], builder: Callable,
                  label: str, identity: Callable, id_name: str) -> list:
    """Build records from rows; report every invalid row and duplicate ID."""
    records, errors, first_seen = [], [], {}
    for line_no, row in rows:
        try:
            record = builder(row)
        except ValueError as exc:
            errors.append(f"{label} line {line_no}: {exc}")
            continue
        key, shown = identity(record)
        if key in first_seen:
            errors.append(f"{label} line {line_no}: duplicate {id_name} '{shown}' "
                          f"(first seen on line {first_seen[key]})")
            continue
        first_seen[key] = line_no
        records.append(record)
    if errors:
        raise DataError(errors)
    return records


def load_requests(path: str) -> List[Request]:
    rows = read_table(path, REQUEST_REQUIRED, REQUEST_OPTIONAL)
    return build_records(rows, build_request, os.path.basename(path),
                         lambda r: (norm(r.request_id), r.request_id), "request_id")


def load_rides(path: str) -> List[Ride]:
    rows = read_table(path, RIDE_REQUIRED)
    return build_records(rows, build_ride, os.path.basename(path),
                         lambda r: (norm(r.ride_id), r.ride_id), "ride_id")


def load_vehicles(path: str) -> List[Vehicle]:
    rows = read_table(path, VEHICLE_REQUIRED)
    return build_records(
        rows, build_vehicle, os.path.basename(path),
        lambda v: ((norm(v.vehicle_type), v.destination_key),
                   f"{v.vehicle_type} / {v.destination}"),
        "vehicle_type+destination")


# --------------------------------------------------------------------------
# Compatibility checks
# --------------------------------------------------------------------------

def ride_incompatibility(req: Request, ride: Ride, seats_left: int,
                         luggage_left: int) -> Optional[str]:
    """Return None if `req` can join `ride`, otherwise the reason it cannot."""
    if req.pickup_key != ride.pickup_key or req.destination_key != ride.destination_key:
        return f"route differs (ride goes {ride.pickup} -> {ride.destination})"
    if not (req.earliest <= ride.departure <= req.latest):
        return (f"ride departs {fmt_dt(ride.departure)}, outside the request window "
                f"{fmt_dt(req.earliest)} to {fmt_dt(req.latest)}")
    if req.passengers > seats_left:
        return f"needs {req.passengers} seat(s) but only {seats_left} left"
    if req.luggage > luggage_left:
        return f"needs {req.luggage} bag(s) of space but only {luggage_left} left"
    return None


def windows_overlap(start: datetime, end: datetime, req: Request) -> bool:
    return max(start, req.earliest) <= min(end, req.latest)


def feasible_vehicles(vehicles: Sequence[Vehicle], destination_key: str,
                      passengers: int, luggage: int) -> List[Vehicle]:
    return [v for v in vehicles
            if v.destination_key == destination_key
            and v.passenger_capacity >= passengers
            and v.luggage_capacity >= luggage]


# --------------------------------------------------------------------------
# Cost calculation
# --------------------------------------------------------------------------

def split_fare(total_fare: Decimal, passenger_counts: Sequence[int]) -> List[Decimal]:
    """Split a fare in proportion to passengers, exactly, in whole paise.

    Leftover paise go to the largest fractional remainders (ties: earlier
    position).  The returned shares always sum to total_fare.
    """
    total_paise = int(total_fare * 100)
    total_pax = sum(passenger_counts)
    base = [total_paise * c // total_pax for c in passenger_counts]
    remainder = [(total_paise * c) % total_pax for c in passenger_counts]
    leftover = total_paise - sum(base)
    order = sorted(range(len(passenger_counts)), key=lambda i: (-remainder[i], i))
    for i in order[:leftover]:
        base[i] += 1
    return [Decimal(b) / 100 for b in base]


def describe_saving(solo_fare: Optional[Decimal], share: Decimal) -> str:
    """Savings text; only claims a saving when a solo fare is on file."""
    if solo_fare is None:
        return "N/A (no solo fare provided)"
    diff = solo_fare - share
    if diff > 0:
        return f"saves {CURRENCY} {diff:.2f}"
    if diff == 0:
        return "no saving"
    return f"costs {CURRENCY} {-diff:.2f} MORE than solo fare"


# --------------------------------------------------------------------------
# Workflow 1: fill an already-booked cab
# --------------------------------------------------------------------------

def select_best_subset(candidates: Sequence[Request], seats: int,
                       luggage: int) -> Tuple[List[Request], str]:
    """Choose which compatible requests to put in ONE cab.

    Up to EXHAUSTIVE_LIMIT candidates: exact search maximising total passengers,
    then number of requests, then the lexicographically smallest request IDs.
    More candidates: greedy fallback (largest first) - NOT guaranteed optimal.
    """
    ordered = sorted(candidates, key=lambda r: r.request_id)
    if len(ordered) > EXHAUSTIVE_LIMIT:
        chosen: List[Request] = []
        used_p = used_l = 0
        for req in sorted(ordered, key=lambda r: (-r.passengers, -r.luggage, r.request_id)):
            if used_p + req.passengers <= seats and used_l + req.luggage <= luggage:
                chosen.append(req)
                used_p += req.passengers
                used_l += req.luggage
        return sorted(chosen, key=lambda r: r.request_id), "greedy"

    best_key = (0, 0)
    best_ids: List[str] = []
    best_set: List[Request] = []

    def search(index: int, chosen: List[Request], pax: int, bags: int) -> None:
        nonlocal best_key, best_ids, best_set
        if index == len(ordered):
            key = (pax, len(chosen))
            ids = [r.request_id for r in chosen]
            if key > best_key or (key == best_key and ids < best_ids):
                best_key, best_ids, best_set = key, ids, list(chosen)
            return
        req = ordered[index]
        if pax + req.passengers <= seats and bags + req.luggage <= luggage:
            chosen.append(req)
            search(index + 1, chosen, pax + req.passengers, bags + req.luggage)
            chosen.pop()
        search(index + 1, chosen, pax, bags)

    search(0, [], 0, 0)
    return best_set, "exact"


def match_existing_rides(requests: Sequence[Request],
                         rides: Sequence[Ride]) -> Workflow1Result:
    """Baseline (heuristic) matcher for Workflow 1.  See module docstring."""
    ordered_rides = sorted(rides, key=lambda r: (r.departure, r.ride_id))
    ordered_requests = sorted(requests, key=lambda r: r.request_id)
    assigned = set()
    matches: List[RideMatch] = []
    remaining: Dict[str, Tuple[int, int]] = {}
    for ride in ordered_rides:
        seats = ride.passenger_capacity - ride.booked_passengers
        bags = ride.luggage_capacity - ride.booked_luggage
        candidates = [r for r in ordered_requests
                      if r.request_id not in assigned
                      and ride_incompatibility(r, ride, seats, bags) is None]
        chosen, method = select_best_subset(candidates, seats, bags)
        for req in chosen:
            assigned.add(req.request_id)
        remaining[ride.ride_id] = (seats - sum(r.passengers for r in chosen),
                                   bags - sum(r.luggage for r in chosen))
        matches.append(RideMatch(ride, seats, bags, chosen, method))
    unassigned = [r for r in ordered_requests if r.request_id not in assigned]
    return Workflow1Result(matches, unassigned, remaining)


def explain_unassigned(req: Request, rides: Sequence[Ride],
                       remaining: Dict[str, Tuple[int, int]]) -> str:
    same_route = [r for r in rides
                  if r.pickup_key == req.pickup_key
                  and r.destination_key == req.destination_key]
    if not same_route:
        return (f"no booked ride goes from {req.pickup} to {req.destination}")
    reasons = []
    for ride in sorted(same_route, key=lambda r: (r.departure, r.ride_id)):
        seats, bags = remaining[ride.ride_id]
        reason = ride_incompatibility(req, ride, seats, bags)
        if reason is None:  # cannot happen after matching, kept for safety
            reason = "was eligible but not selected"
        elif reason.startswith("needs"):
            reason += " after other requests were assigned"
        reasons.append(f"{ride.ride_id}: {reason}")
    return "; ".join(reasons)


# --------------------------------------------------------------------------
# Workflow 2: group first, then choose a vehicle
# --------------------------------------------------------------------------

def cheapest_vehicle(vehicles: Sequence[Vehicle], destination_key: str,
                     passengers: int, luggage: int) -> Optional[Vehicle]:
    options = feasible_vehicles(vehicles, destination_key, passengers, luggage)
    if not options:
        return None
    return min(options, key=lambda v: (v.total_fare, norm(v.vehicle_type)))


def solo_infeasibility_reason(req: Request, vehicles: Sequence[Vehicle]) -> Optional[str]:
    """Why a request cannot be served even alone (None if it can)."""
    for_destination = [v for v in vehicles if v.destination_key == req.destination_key]
    if not for_destination:
        return f"{VEHICLES_FILE} lists no vehicle for destination '{req.destination}'"
    if cheapest_vehicle(vehicles, req.destination_key, req.passengers, req.luggage) is None:
        max_seats = max(v.passenger_capacity for v in for_destination)
        max_bags = max(v.luggage_capacity for v in for_destination)
        return (f"no vehicle for '{req.destination}' fits {req.passengers} passenger(s) "
                f"and {req.luggage} bag(s) (max seats {max_seats}, max luggage {max_bags})")
    return None


def group_and_select_vehicles(requests: Sequence[Request],
                              vehicles: Sequence[Vehicle]) -> Workflow2Result:
    """Baseline (heuristic) grouping for Workflow 2.  See module docstring."""
    unmatched: List[Tuple[Request, str]] = []
    buckets: Dict[Tuple[str, str], List[Request]] = {}
    for req in sorted(requests, key=lambda r: r.request_id):
        reason = solo_infeasibility_reason(req, vehicles)
        if reason:
            unmatched.append((req, reason))
        else:
            buckets.setdefault((req.pickup_key, req.destination_key), []).append(req)

    groups: List[TripGroup] = []
    for key in sorted(buckets):
        destination_key = key[1]
        drafts: List[TripGroup] = []
        ordered = sorted(buckets[key],
                         key=lambda r: (-r.passengers, -r.luggage, r.latest, r.request_id))
        for req in ordered:
            alone = cheapest_vehicle(vehicles, destination_key, req.passengers, req.luggage)
            best: Optional[Tuple[Decimal, int, Vehicle]] = None
            for index, group in enumerate(drafts):
                if not windows_overlap(group.window_start, group.window_end, req):
                    continue
                vehicle = cheapest_vehicle(vehicles, destination_key,
                                           group.passengers + req.passengers,
                                           group.luggage + req.luggage)
                if vehicle is None:
                    continue
                increase = vehicle.total_fare - group.vehicle.total_fare
                if best is None or (increase, index) < (best[0], best[1]):
                    best = (increase, index, vehicle)
            if best is not None and best[0] <= alone.total_fare:
                group = drafts[best[1]]
                group.requests.append(req)
                group.vehicle = best[2]
                group.window_start = max(group.window_start, req.earliest)
                group.window_end = min(group.window_end, req.latest)
            else:
                drafts.append(TripGroup([req], alone, req.earliest, req.latest))
        groups.extend(drafts)

    for number, group in enumerate(groups, start=1):
        group.group_id = f"G{number:02d}"
        group.requests.sort(key=lambda r: r.request_id)
    return Workflow2Result(groups, sorted(unmatched, key=lambda item: item[0].request_id))


# --------------------------------------------------------------------------
# Solver registry: swap these two entries for OR-Tools IP models later
# --------------------------------------------------------------------------

SOLVERS = {
    "existing_rides": match_existing_rides,
    "new_groups": group_and_select_vehicles,
}


# --------------------------------------------------------------------------
# Verification of hard constraints (independent of the solver used)
# --------------------------------------------------------------------------

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InternalError(message)


def verify_workflow1(result: Workflow1Result, requests: Sequence[Request]) -> None:
    seen = set()
    for match in result.matches:
        ride = match.ride
        pax = ride.booked_passengers + sum(r.passengers for r in match.riders)
        bags = ride.booked_luggage + sum(r.luggage for r in match.riders)
        _require(pax <= ride.passenger_capacity, f"{ride.ride_id}: seat capacity exceeded")
        _require(bags <= ride.luggage_capacity, f"{ride.ride_id}: luggage capacity exceeded")
        for req in match.riders:
            _require(req.request_id not in seen, f"{req.request_id} assigned twice")
            seen.add(req.request_id)
            _require(req.pickup_key == ride.pickup_key
                     and req.destination_key == ride.destination_key,
                     f"{req.request_id}: route mismatch with {ride.ride_id}")
            _require(req.earliest <= ride.departure <= req.latest,
                     f"{req.request_id}: departure outside window for {ride.ride_id}")
        shares = split_fare(ride.total_fare,
                            [ride.booked_passengers] + [r.passengers for r in match.riders])
        _require(sum(shares) == ride.total_fare, f"{ride.ride_id}: shares do not add up")
    left = {r.request_id for r in result.unassigned}
    _require(not (seen & left), "a request is both assigned and unassigned")
    _require(seen | left == {r.request_id for r in requests}, "a request was lost")


def verify_workflow2(result: Workflow2Result, requests: Sequence[Request],
                     vehicles: Sequence[Vehicle]) -> None:
    seen = set()
    for group in result.groups:
        vehicle = group.vehicle
        _require(group.passengers <= vehicle.passenger_capacity,
                 f"{group.group_id}: seat capacity exceeded")
        _require(group.luggage <= vehicle.luggage_capacity,
                 f"{group.group_id}: luggage capacity exceeded")
        _require(group.window_start <= group.window_end, f"{group.group_id}: empty time window")
        best = cheapest_vehicle(vehicles, vehicle.destination_key,
                                group.passengers, group.luggage)
        _require(best is not None and best.total_fare == vehicle.total_fare,
                 f"{group.group_id}: vehicle is not the cheapest feasible one")
        for req in group.requests:
            _require(req.request_id not in seen, f"{req.request_id} assigned twice")
            seen.add(req.request_id)
            _require(req.destination_key == vehicle.destination_key
                     and req.pickup_key == group.requests[0].pickup_key,
                     f"{req.request_id}: route mismatch in {group.group_id}")
            _require(req.earliest <= group.window_start and group.window_end <= req.latest,
                     f"{req.request_id}: group window outside request window")
        shares = split_fare(vehicle.total_fare, [r.passengers for r in group.requests])
        _require(sum(shares) == vehicle.total_fare, f"{group.group_id}: shares do not add up")
    left = {r.request_id for r, _ in result.unmatched}
    _require(not (seen & left), "a request is both grouped and unmatched")
    _require(seen | left == {r.request_id for r in requests}, "a request was lost")


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------

def fmt_dt(value: datetime) -> str:
    return value.strftime(DATETIME_FORMAT)


def money(value: Decimal) -> str:
    return f"{CURRENCY} {value:.2f}"


def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print("    " + line)
    print("    " + "  ".join("-" * w for w in widths))
    for row in rows:
        print("    " + "  ".join(c.ljust(w) for c, w in zip(row, widths)))


def print_workflow1(result: Workflow1Result, rides: Sequence[Ride]) -> None:
    print("\n" + "=" * 72)
    print("WORKFLOW 1 - passengers for already-booked cabs")
    print("=" * 72)
    for match in result.matches:
        ride = match.ride
        print(f"\nRide {ride.ride_id}: {ride.pickup} -> {ride.destination}, "
              f"departs {fmt_dt(ride.departure)}")
        print(f"  Booked by {ride.booked_by}; vehicle {ride.vehicle_type}; "
              f"total cab fare (already booked): {money(ride.total_fare)}")
        print(f"  Capacity {ride.passenger_capacity} seat(s) / {ride.luggage_capacity} bag(s); "
              f"booked {ride.booked_passengers} / {ride.booked_luggage}; "
              f"free before matching: {match.seats_before} seat(s) / "
              f"{match.luggage_before} bag(s)")
        if not match.riders:
            print("  Selected passengers: none (no compatible request fits)")
            continue
        how = ("exact search over all compatible combinations (max passengers, then "
               "most requests, then lowest IDs)" if match.method == "exact" else
               "greedy fallback, NOT guaranteed optimal")
        print(f"  Selected passengers (method: {how}):")
        for req in match.riders:
            print(f"    - {req.request_id} {req.student_name}: {req.passengers} passenger(s), "
                  f"{req.luggage} bag(s)")
            print(f"      Eligible because: same pickup ({ride.pickup}) and destination "
                  f"({ride.destination}); departure {fmt_dt(ride.departure)} is inside "
                  f"{fmt_dt(req.earliest)} to {fmt_dt(req.latest)}; fits remaining capacity.")
        left_p, left_l = result.remaining[ride.ride_id]
        print(f"  Remaining after matching: {left_p} seat(s) / {left_l} bag(s)")
        shares = split_fare(ride.total_fare,
                            [ride.booked_passengers] + [r.passengers for r in match.riders])
        rows = [[f"{ride.booked_by} (booker)", str(ride.booked_passengers),
                 money(shares[0]), "not on file", "N/A (no solo fare provided)"]]
        for req, share in zip(match.riders, shares[1:]):
            rows.append([f"{req.request_id} {req.student_name}", str(req.passengers),
                         money(share),
                         money(req.solo_fare) if req.solo_fare is not None else "not provided",
                         describe_saving(req.solo_fare, share)])
        print("  Estimated fare shares (total cab fare split per passenger):")
        print_table(["Student", "Pax", "Fare share", "Solo fare", "Estimated saving"], rows)
    print("\nRequests not matched to any booked ride:")
    if not result.unassigned:
        print("  none")
    for req in result.unassigned:
        print(f"  - {req.request_id} {req.student_name}: "
              f"{explain_unassigned(req, rides, result.remaining)}")


def print_workflow2(result: Workflow2Result) -> None:
    print("\n" + "=" * 72)
    print("WORKFLOW 2 - group first, then choose the cheapest feasible cab")
    print("=" * 72)
    total = Decimal(0)
    for group in result.groups:
        vehicle = group.vehicle
        total += vehicle.total_fare
        first = group.requests[0]
        print(f"\nGroup {group.group_id}: {first.pickup} -> {first.destination}")
        print(f"  Proposed departure: {fmt_dt(group.window_start)} "
              f"(common window {fmt_dt(group.window_start)} to {fmt_dt(group.window_end)})")
        print(f"  Vehicle: {vehicle.vehicle_type} ({vehicle.passenger_capacity} seats / "
              f"{vehicle.luggage_capacity} bags) - cheapest feasible option; "
              f"load {group.passengers} passenger(s) / {group.luggage} bag(s)")
        print(f"  Total cab fare: {money(vehicle.total_fare)}")
        shares = split_fare(vehicle.total_fare, [r.passengers for r in group.requests])
        rows = []
        for req, share in zip(group.requests, shares):
            rows.append([f"{req.request_id} {req.student_name}", str(req.passengers),
                         money(share),
                         money(req.solo_fare) if req.solo_fare is not None else "not provided",
                         describe_saving(req.solo_fare, share)])
        print("  Estimated fare shares (total cab fare split per passenger):")
        print_table(["Student", "Pax", "Fare share", "Solo fare", "Estimated saving"], rows)
    print(f"\nCabs proposed: {len(result.groups)}; combined total cab fare: {money(total)}")
    print("\nRequests left unmatched:")
    if not result.unmatched:
        print("  none")
    for req, reason in result.unmatched:
        print(f"  - {req.request_id} {req.student_name}: {reason}")


def print_notes() -> None:
    print("\n" + "-" * 72)
    print("Assumptions and limitations")
    print("-" * 72)
    print(f"* Fares come only from the CSV sample data and are treated as {CURRENCY}.")
    print("* Fare share = total cab fare x (student's passengers / cab passengers),")
    print("  rounded to whole paise so shares add up exactly to the total fare.")
    print("* Savings are shown only when solo_fare is given for that request.")
    print("* Pickup locations must match by name; no distance or detour is modelled.")
    print("* Enforced: seats, luggage, route, time window, one assignment per request.")
    print("* Heuristic: ride order in Workflow 1; first-fit grouping in Workflow 2.")
    print("  Neither is claimed to be optimal; an IP model is the planned upgrade.")


# --------------------------------------------------------------------------
# Command line entry point
# --------------------------------------------------------------------------

def run(data_dir: str, workflow: str) -> None:
    requests = load_requests(os.path.join(data_dir, REQUEST_FILE))
    rides = vehicles = None
    errors: List[str] = []
    if workflow in ("1", "both"):
        try:
            rides = load_rides(os.path.join(data_dir, RIDES_FILE))
        except DataError as exc:
            errors.extend(exc.messages)
    if workflow in ("2", "both"):
        try:
            vehicles = load_vehicles(os.path.join(data_dir, VEHICLES_FILE))
        except DataError as exc:
            errors.extend(exc.messages)
    if errors:
        raise DataError(errors)

    print(f"Loaded {len(requests)} request(s)"
          + (f", {len(rides)} booked ride(s)" if rides is not None else "")
          + (f", {len(vehicles)} vehicle option(s)" if vehicles is not None else ""))

    leftover = requests
    if rides is not None:
        result1 = SOLVERS["existing_rides"](requests, rides)
        verify_workflow1(result1, requests)
        print_workflow1(result1, rides)
        if workflow == "both":
            leftover = result1.unassigned  # each request is used at most once overall
    if vehicles is not None:
        if workflow == "both":
            print(f"\n(Workflow 2 below only uses the {len(leftover)} request(s) "
                  f"that Workflow 1 did not assign.)")
        result2 = SOLVERS["new_groups"](leftover, vehicles)
        verify_workflow2(result2, leftover, vehicles)
        print_workflow2(result2)
    print_notes()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="IITP RideShare baseline backend")
    parser.add_argument("--workflow", choices=["1", "2", "both"], default="both",
                        help="which workflow to run (default: both)")
    parser.add_argument("--data-dir", default=os.path.dirname(os.path.abspath(__file__)),
                        help="folder containing the three CSV files "
                             "(default: the folder of main.py)")
    args = parser.parse_args(argv)
    try:
        run(args.data_dir, args.workflow)
    except DataError as exc:
        print("INPUT ERROR - nothing was processed:", file=sys.stderr)
        for message in exc.messages:
            print(f"  * {message}", file=sys.stderr)
        return 1
    except InternalError as exc:
        print(f"INTERNAL CHECK FAILED (bug): {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())