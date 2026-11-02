"""Workflow 2: form new shared cabs from requests not placed in a booked cab.

This module holds the greedy grouping heuristic and the helper that turns a
group + vehicle into a priced RidePlan (also used by the IP model).
"""
from __future__ import annotations

from dataclasses import dataclass

from .compatibility import common_window, group_gender_check
from .config import Config
from .fares import split_fare
from .models import (ESTIMATED_FARE, NEW, RidePlan, TripRequest, Unmatched,
                     VehicleOption)
from .vehicle_selection import VehicleCatalog


def departure_for(members: list[TripRequest], cfg: Config):
    """Earliest time every member can leave.

    Prefers a time inside everyone's *stated* window; only falls back to the
    tolerance-widened windows when the stated windows have no common point.
    """
    exact = common_window(members, cfg.with_(time_tolerance_minutes=0))
    window = exact or common_window(members, cfg)
    if window is None:
        raise ValueError("members have no common departure time")
    return window[0]


def build_new_plan(ride_id: str, members: list[TripRequest], vehicle: VehicleOption,
                   cfg: Config, gender_unverified: bool = False) -> RidePlan:
    members = sorted(members, key=lambda r: r.request_id)
    return RidePlan(
        ride_id=ride_id, kind=NEW, pickup_location=members[0].pickup_location,
        destination=members[0].destination, departure_time=departure_for(members, cfg),
        vehicle_type=vehicle.vehicle_type, passenger_capacity=vehicle.passenger_capacity,
        luggage_capacity=vehicle.luggage_capacity, total_fare=vehicle.total_fare,
        fare_status=ESTIMATED_FARE, request_ids=[m.request_id for m in members],
        fare_shares=split_fare(vehicle.total_fare, {m.request_id: m.passengers for m in members}),
        gender_unverified=gender_unverified)


def unserviceable_reason(req: TripRequest, catalog: VehicleCatalog) -> Unmatched | None:
    """Why a request cannot get a new cab even on its own, or None if it can."""
    if not catalog.has_destination(req.destination):
        return Unmatched(req.request_id, "NO_VEHICLE_INFO",
                         f"'{req.destination}' is not in vehicle_options.csv: the trip is "
                         f"allowed, but vehicle and fare must be confirmed manually")
    if catalog.cheapest_fitting(req.destination, req.passengers, req.luggage) is None:
        largest = max(catalog.options(req.destination),
                      key=lambda v: (v.passenger_capacity, v.luggage_capacity))
        return Unmatched(req.request_id, "NO_VEHICLE_LARGE_ENOUGH",
                         f"needs {req.passengers} seats/{req.luggage} bags; largest listed "
                         f"vehicle is {largest.vehicle_type} "
                         f"({largest.passenger_capacity}/{largest.luggage_capacity})")
    return None


@dataclass
class _OpenGroup:
    members: list[TripRequest]
    vehicle: VehicleOption
    unverified: bool = False

    @property
    def passengers(self) -> int:
        return sum(m.passengers for m in self.members)

    @property
    def luggage(self) -> int:
        return sum(m.luggage for m in self.members)


def greedy_new_rides(requests, catalog: VehicleCatalog, cfg: Config,
                     id_prefix: str = "GRD-NEW"):
    """Greedy grouping by minimum additional cost.

    Requests are taken in chronological order of their earliest departure.
    For each one, every option is priced:
      * open a new cab alone        -> cost = cheapest vehicle that fits it;
      * join an open group          -> cost = (cheapest vehicle for the enlarged
                                        group) - (current vehicle fare),
        allowed only if the common window stays non-empty and gender rules hold.
    The cheapest option wins; on a tie, joining a group wins (fewer cabs).
    Decisions are never revisited, which is why this is a heuristic.
    """
    groups: list[_OpenGroup] = []
    unmatched: list[Unmatched] = []
    for req in sorted(requests, key=lambda r: (r.earliest_departure, r.latest_departure,
                                               r.request_id)):
        problem = unserviceable_reason(req, catalog)
        if problem:
            unmatched.append(problem)
            continue
        alone = catalog.cheapest_fitting(req.destination, req.passengers, req.luggage)
        best = ((alone.total_fare, 1, 0), None, alone, False)
        for idx, g in enumerate(groups):
            if g.members[0].route != req.route:
                continue
            members = g.members + [req]
            if common_window(members, cfg) is None:
                continue
            gc = group_gender_check([m.party for m in members], cfg)
            if not gc.ok:
                continue
            vehicle = catalog.cheapest_fitting(req.destination, g.passengers + req.passengers,
                                               g.luggage + req.luggage)
            if vehicle is None:
                continue
            key = (vehicle.total_fare - g.vehicle.total_fare, 0, idx)
            if key < best[0]:
                best = (key, g, vehicle, gc.unverified)
        _, group, vehicle, unverified = best
        if group is None:
            groups.append(_OpenGroup([req], vehicle))
        else:
            group.members.append(req)
            group.vehicle = vehicle
            group.unverified = group.unverified or unverified

    plans = [build_new_plan("", g.members, g.vehicle, cfg, g.unverified) for g in groups]
    return number_new_rides(plans, id_prefix), unmatched


def number_new_rides(plans: list[RidePlan], prefix: str) -> list[RidePlan]:
    plans.sort(key=lambda p: (p.departure_time, p.destination, p.request_ids))
    for i, p in enumerate(plans, start=1):
        p.ride_id = f"{prefix}-{i:03d}"
    return plans
