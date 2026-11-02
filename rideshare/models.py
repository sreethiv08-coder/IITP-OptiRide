"""Domain objects shared by every module.

Money is always ``decimal.Decimal`` (never float) so fare shares add up exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


def location_key(name: str) -> str:
    """Normalised form used to compare locations (case and spacing insensitive)."""
    return " ".join(name.split()).casefold()


@dataclass(frozen=True)
class Party:
    """Anyone who occupies a cab: a requesting student or the original booker."""
    label: str
    gender: str | None       # MALE / FEMALE / OTHER, or None if unknown
    preference: str | None   # ANY / SAME / MALE / FEMALE / OTHER, or None if unknown


@dataclass(frozen=True)
class TripRequest:
    request_id: str
    student_name: str
    pickup_location: str
    destination: str
    earliest_departure: datetime
    latest_departure: datetime
    passengers: int
    luggage: int
    gender: str | None
    gender_preference: str | None
    solo_fare: Decimal | None
    roll_number: str = ""
    ride_status: str = ""

    @property
    def route(self) -> tuple[str, str]:
        return (location_key(self.pickup_location), location_key(self.destination))

    @property
    def party(self) -> Party:
        return Party(self.request_id, self.gender, self.gender_preference)


@dataclass(frozen=True)
class ExistingRide:
    ride_id: str
    booked_by: str
    pickup_location: str
    destination: str
    departure_time: datetime
    vehicle_type: str
    total_fare: Decimal
    passenger_capacity: int
    luggage_capacity: int
    booked_passengers: int
    booked_luggage: int
    booker_gender: str | None = None
    booker_gender_preference: str | None = None

    @property
    def route(self) -> tuple[str, str]:
        return (location_key(self.pickup_location), location_key(self.destination))

    @property
    def remaining_seats(self) -> int:
        return self.passenger_capacity - self.booked_passengers

    @property
    def remaining_luggage(self) -> int:
        return self.luggage_capacity - self.booked_luggage

    @property
    def booker_party(self) -> Party:
        return Party(f"BOOKER:{self.booked_by}", self.booker_gender, self.booker_gender_preference)


@dataclass(frozen=True)
class VehicleOption:
    vehicle_type: str
    destination: str
    passenger_capacity: int
    luggage_capacity: int
    total_fare: Decimal

    @property
    def destination_key(self) -> str:
        return location_key(self.destination)


@dataclass(frozen=True)
class ValidationIssue:
    severity: str          # ERROR (record excluded) or WARNING (record kept)
    file: str
    row: int | None        # 1-based data row number (header is row 0); None = whole file
    record_id: str
    column: str
    message: str


@dataclass(frozen=True)
class Dataset:
    requests: tuple[TripRequest, ...]
    rides: tuple[ExistingRide, ...]
    vehicles: tuple[VehicleOption, ...]
    issues: tuple[ValidationIssue, ...] = ()
    # Records that failed validation: id -> list of messages. They are never
    # dropped silently; they are reported as unmatched with reason INVALID_RECORD.
    invalid_requests: dict[str, tuple[str, ...]] = field(default_factory=dict)
    invalid_rides: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Booker gender columns are optional in available_rides.csv.
    ride_gender_columns_present: bool = False

    def request(self, request_id: str) -> TripRequest:
        for r in self.requests:
            if r.request_id == request_id:
                return r
        raise KeyError(request_id)

    def ride(self, ride_id: str) -> ExistingRide:
        for r in self.rides:
            if r.ride_id == ride_id:
                return r
        raise KeyError(ride_id)


# ---------------------------------------------------------------------------
# Solution objects produced by both the Greedy algorithm and the IP model
# ---------------------------------------------------------------------------

EXISTING = "EXISTING"
NEW = "NEW"
CONFIRMED_FARE = "CONFIRMED"   # fare of a cab that is already booked
ESTIMATED_FARE = "ESTIMATED"   # fare taken from vehicle_options.csv


@dataclass
class RidePlan:
    """One cab in a solution, with the requests assigned to it."""
    ride_id: str
    kind: str                       # EXISTING or NEW
    pickup_location: str
    destination: str
    departure_time: datetime
    vehicle_type: str
    passenger_capacity: int
    luggage_capacity: int
    total_fare: Decimal
    fare_status: str                # CONFIRMED or ESTIMATED
    base_passengers: int = 0        # already booked (existing rides only)
    base_luggage: int = 0
    booker: str | None = None
    request_ids: list[str] = field(default_factory=list)
    # Key = request_id, or "BOOKER:<name>" for the existing booker's share.
    fare_shares: dict[str, Decimal] = field(default_factory=dict)
    gender_unverified: bool = False


@dataclass
class Unmatched:
    request_id: str
    reason_code: str
    detail: str


@dataclass
class Solution:
    algorithm: str                  # "Greedy" or "Integer Programming"
    rides: list[RidePlan]
    unmatched: list[Unmatched]
    status: str                     # HEURISTIC, OPTIMAL, FEASIBLE, INFEASIBLE, FAILED
    status_detail: str = ""
    objective_value: float | None = None
    runtime_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)
    # Why each request did not join an existing booked cab (Workflow 1),
    # kept even when it was later served by a new cab: id -> (code, detail).
    existing_cab_reasons: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def existing_used(self) -> list[RidePlan]:
        return [r for r in self.rides if r.kind == EXISTING and r.request_ids]

    @property
    def new_rides(self) -> list[RidePlan]:
        return [r for r in self.rides if r.kind == NEW]

    @property
    def served_ids(self) -> list[str]:
        return [rid for ride in self.rides for rid in ride.request_ids]

    @property
    def new_cab_cost(self) -> Decimal:
        return sum((r.total_fare for r in self.new_rides), Decimal("0"))
