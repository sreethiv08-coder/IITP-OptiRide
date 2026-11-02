"""Independent verification of a Solution against the raw input data.

The checks are re-implemented here from the problem rules rather than reusing
the algorithms' own feasibility code, so a bug in an algorithm cannot hide
itself. The only shared piece is the gender acceptance rule, which is the
definition of the policy (compatibility.gender_check).
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from decimal import Decimal
from itertools import combinations

from .compatibility import gender_check
from .config import Config
from .models import EXISTING, NEW, Dataset, Solution, location_key


def verify_solution(solution: Solution, dataset: Dataset, cfg: Config) -> list[str]:
    """Return a list of violations (empty list = solution is valid)."""
    errors: list[str] = []
    reqs = {r.request_id: r for r in dataset.requests}
    rides = {r.ride_id: r for r in dataset.rides}
    tol = timedelta(minutes=cfg.time_tolerance_minutes)

    # 1. No request assigned twice; every request assigned or reported.
    assigned = Counter(rid for plan in solution.rides for rid in plan.request_ids)
    for rid, n in assigned.items():
        if n > 1:
            errors.append(f"{rid} is assigned to {n} cabs")
        if rid not in reqs:
            errors.append(f"{rid} is assigned but is not a valid request")
    reported = Counter(u.request_id for u in solution.unmatched)
    for rid, n in reported.items():
        if n > 1:
            errors.append(f"{rid} is reported unmatched {n} times")
        if rid in assigned:
            errors.append(f"{rid} is both assigned and reported unmatched")
    for rid in reqs:
        if rid not in assigned and rid not in reported:
            errors.append(f"{rid} is neither assigned nor reported as unmatched")
    for rid in dataset.invalid_requests:
        if rid not in reported:
            errors.append(f"invalid record {rid} is not reported")

    seen_ride_ids = Counter(p.ride_id for p in solution.rides)
    for ride_id, n in seen_ride_ids.items():
        if n > 1:
            errors.append(f"cab {ride_id} appears {n} times in the solution")

    for plan in solution.rides:
        members = [reqs[i] for i in plan.request_ids if i in reqs]
        where = f"cab {plan.ride_id}"
        if plan.kind == EXISTING:
            ride = rides.get(plan.ride_id)
            if ride is None:
                errors.append(f"{where} is not a known booked cab")
                continue
            cap, lug_cap = ride.passenger_capacity, ride.luggage_capacity
            base_pax, base_lug = ride.booked_passengers, ride.booked_luggage
            route = (location_key(ride.pickup_location), location_key(ride.destination))
            departure = ride.departure_time
            parties = [ride.booker_party]
            if plan.total_fare != ride.total_fare:
                errors.append(f"{where}: fare {plan.total_fare} differs from booked fare {ride.total_fare}")
            for r in members:
                if not (r.earliest_departure - tol <= departure <= r.latest_departure + tol):
                    errors.append(f"{where}: departs {departure} outside window of {r.request_id}")
        elif plan.kind == NEW:
            if not members:
                errors.append(f"{where} is a new cab with no passengers")
                continue
            option = next((v for v in dataset.vehicles
                           if v.vehicle_type == plan.vehicle_type
                           and location_key(v.destination) == location_key(plan.destination)), None)
            if option is None:
                errors.append(f"{where}: vehicle {plan.vehicle_type} is not listed for {plan.destination}")
                continue
            if (option.passenger_capacity, option.luggage_capacity, option.total_fare) != \
                    (plan.passenger_capacity, plan.luggage_capacity, plan.total_fare):
                errors.append(f"{where}: vehicle data does not match vehicle_options.csv")
            cap, lug_cap, base_pax, base_lug = (option.passenger_capacity,
                                                option.luggage_capacity, 0, 0)
            route = (location_key(plan.pickup_location), location_key(plan.destination))
            departure = plan.departure_time
            parties = []
            for r in members:
                if not (r.earliest_departure - tol <= departure <= r.latest_departure + tol):
                    errors.append(f"{where}: chosen departure {departure} outside window of {r.request_id}")
        else:
            errors.append(f"{where}: unknown cab kind {plan.kind}")
            continue

        pax = base_pax + sum(r.passengers for r in members)
        lug = base_lug + sum(r.luggage for r in members)
        if pax > cap:
            errors.append(f"{where}: {pax} passengers exceed capacity {cap}")
        if lug > lug_cap:
            errors.append(f"{where}: {lug} bags exceed luggage capacity {lug_cap}")
        for r in members:
            if r.route != route:
                errors.append(f"{where}: {r.request_id} has a different pickup/destination")
        parties += [r.party for r in members]
        for a, b in combinations(parties, 2):
            g = gender_check(a, b, cfg)
            if not g.ok:
                errors.append(f"{where}: gender rule violated ({g.detail})")

        share_total = sum(plan.fare_shares.values(), Decimal("0"))
        if share_total != plan.total_fare:
            errors.append(f"{where}: fare shares add up to {share_total}, not {plan.total_fare}")
        for r in members:
            if r.request_id not in plan.fare_shares:
                errors.append(f"{where}: no fare share for {r.request_id}")
        if any(s.as_tuple().exponent < -2 for s in plan.fare_shares.values()):
            errors.append(f"{where}: fare share with more than two decimals")
    return errors
