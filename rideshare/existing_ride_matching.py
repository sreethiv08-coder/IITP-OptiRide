"""Workflow 1: let students join cabs that are already booked.

``static_join_check`` answers "could this request join this cab if it were the
only joiner?" in the order the specification lists the checks (route, time,
seats, luggage, gender). The greedy matcher and the IP model both use it to
decide which (request, ride) pairs are candidates; interactions between
several joiners (shared capacity, joiner-vs-joiner gender) are handled by the
algorithms themselves.
"""
from __future__ import annotations

from dataclasses import dataclass

from .compatibility import (REASONS, departure_compatible, gender_check,
                            group_gender_check)
from .config import Config
from .fares import split_fare
from .models import (CONFIRMED_FARE, EXISTING, ExistingRide, RidePlan,
                     TripRequest)

CHECK_ORDER = ["NO_RIDE_ON_ROUTE", "TIME_INCOMPATIBLE", "INSUFFICIENT_SEATS",
               "INSUFFICIENT_LUGGAGE", "GENDER_INCOMPATIBLE"]


@dataclass(frozen=True)
class JoinCheck:
    ok: bool
    reason: str | None = None
    detail: str = ""
    unverified: bool = False
    stage: int = len(CHECK_ORDER)   # how far the request got through the checks


def static_join_check(req: TripRequest, ride: ExistingRide, cfg: Config) -> JoinCheck:
    if req.route != ride.route:
        return JoinCheck(False, "NO_RIDE_ON_ROUTE", "different pickup or destination", stage=0)
    if not departure_compatible(req, ride.departure_time, cfg):
        return JoinCheck(False, "TIME_INCOMPATIBLE",
                         f"{ride.ride_id} departs {ride.departure_time:%Y-%m-%d %H:%M}, outside "
                         f"{req.earliest_departure:%Y-%m-%d %H:%M}-{req.latest_departure:%H:%M}",
                         stage=1)
    if req.passengers > ride.remaining_seats:
        return JoinCheck(False, "INSUFFICIENT_SEATS",
                         f"{ride.ride_id} has {ride.remaining_seats} seat(s) left, "
                         f"request needs {req.passengers}", stage=2)
    if req.luggage > ride.remaining_luggage:
        return JoinCheck(False, "INSUFFICIENT_LUGGAGE",
                         f"{ride.ride_id} has room for {ride.remaining_luggage} bag(s), "
                         f"request has {req.luggage}", stage=3)
    g = gender_check(req.party, ride.booker_party, cfg)
    if not g.ok:
        return JoinCheck(False, g.reason, f"{ride.ride_id}: {g.detail}", stage=4)
    return JoinCheck(True, unverified=g.unverified)


def candidate_rides(req: TripRequest, rides, cfg: Config) -> list[ExistingRide]:
    return [ride for ride in rides if static_join_check(req, ride, cfg).ok]


def diagnose(req: TripRequest, rides, cfg: Config) -> tuple[str, str]:
    """Most informative reason why ``req`` cannot join any booked cab on its own.

    Among the cabs on the same route, the reason from the cab that passed the
    most checks is reported (e.g. "insufficient seats" beats "wrong time").
    """
    on_route = [ride for ride in rides if ride.route == req.route]
    if not on_route:
        return "NO_RIDE_ON_ROUTE", (f"no booked cab from {req.pickup_location} "
                                    f"to {req.destination}")
    checks = [static_join_check(req, ride, cfg) for ride in on_route]
    failed = [c for c in checks if not c.ok]
    if len(failed) < len(checks):
        raise ValueError("diagnose() called for a request that has a candidate cab")
    best_stage = max(c.stage for c in failed)
    best = [c for c in failed if c.stage == best_stage]
    detail = "; ".join(c.detail for c in best[:3])
    if len(best) > 3:
        detail += f"; and {len(best) - 3} more cab(s)"
    return best[0].reason, detail


def greedy_match_existing(requests, rides, cfg: Config):
    """Greedy assignment of requests to booked cabs.

    Selection rule:
      1. Requests with the FEWEST candidate cabs go first (most constrained
         first), then larger parties, then earlier windows, then request id.
      2. Each request takes the candidate cab that still has room, is
         gender-compatible with the joiners already placed, and leaves the
         FEWEST seats free afterwards (best fit), keeping large cabs available
         for large parties.
    Returns (joiners by ride id, reasons for requests left over, unverified ride ids).
    """
    rides_by_id = {ride.ride_id: ride for ride in rides}
    candidates = {r.request_id: candidate_rides(r, rides, cfg) for r in requests}
    joiners: dict[str, list[TripRequest]] = {ride.ride_id: [] for ride in rides}
    used_seats = {ride.ride_id: 0 for ride in rides}
    used_luggage = {ride.ride_id: 0 for ride in rides}
    reasons: dict[str, tuple[str, str]] = {}
    unverified: set[str] = set()

    order = sorted(requests, key=lambda r: (len(candidates[r.request_id]) or 10**9,
                                            -r.passengers, -r.luggage,
                                            r.earliest_departure, r.request_id))
    for req in order:
        cands = candidates[req.request_id]
        if not cands:
            reasons[req.request_id] = diagnose(req, rides, cfg)
            continue
        options, blocked = [], []
        for ride in cands:
            seats_left = ride.remaining_seats - used_seats[ride.ride_id] - req.passengers
            lug_left = ride.remaining_luggage - used_luggage[ride.ride_id] - req.luggage
            if seats_left < 0 or lug_left < 0:
                blocked.append(("CAPACITY_TAKEN", f"{ride.ride_id} filled by other joiners"))
                continue
            parties = [ride.booker_party, req.party] + [j.party for j in joiners[ride.ride_id]]
            g = group_gender_check(parties, cfg)
            if not g.ok:
                blocked.append((g.reason, f"{ride.ride_id}: {g.detail} (with other joiners)"))
                continue
            options.append(((seats_left, lug_left, ride.departure_time, ride.ride_id), ride, g))
        if not options:
            reasons[req.request_id] = (blocked[0][0], "; ".join(b[1] for b in blocked))
            continue
        _, ride, g = min(options, key=lambda o: o[0])
        joiners[ride.ride_id].append(req)
        used_seats[ride.ride_id] += req.passengers
        used_luggage[ride.ride_id] += req.luggage
        if g.unverified:
            unverified.add(ride.ride_id)
    return {k: v for k, v in joiners.items() if v}, reasons, unverified


def build_existing_plan(ride: ExistingRide, joiners: list[TripRequest],
                        gender_unverified: bool = False) -> RidePlan:
    """Re-split the booked cab's (confirmed) fare over booker party + joiners."""
    weights = {ride.booker_party.label: ride.booked_passengers} if ride.booked_passengers else {}
    weights.update({j.request_id: j.passengers for j in joiners})
    return RidePlan(
        ride_id=ride.ride_id, kind=EXISTING, pickup_location=ride.pickup_location,
        destination=ride.destination, departure_time=ride.departure_time,
        vehicle_type=ride.vehicle_type, passenger_capacity=ride.passenger_capacity,
        luggage_capacity=ride.luggage_capacity, total_fare=ride.total_fare,
        fare_status=CONFIRMED_FARE, base_passengers=ride.booked_passengers,
        base_luggage=ride.booked_luggage, booker=ride.booked_by,
        request_ids=[j.request_id for j in joiners],
        fare_shares=split_fare(ride.total_fare, weights),
        gender_unverified=gender_unverified)


def reason_text(code: str) -> str:
    return REASONS.get(code, code)
