
"""Workflow 2: form new shared cabs from requests not placed in a booked cab.

This module holds the greedy grouping heuristic and the helper that turns a
group + vehicle into a priced RidePlan (also used by the IP model).

Vehicles without a known fare are not included in fare optimisation.
They require manual confirmation or a driver quotation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .compatibility import common_window, group_gender_check
from .config import Config
from .fares import split_fare
from .models import (
    ESTIMATED_FARE,
    NEW,
    RidePlan,
    TripRequest,
    Unmatched,
    VehicleOption,
)
from .vehicle_selection import VehicleCatalog


def departure_for(members: list[TripRequest], cfg: Config):
    """Earliest time every member can leave.

    Prefers a time inside everyone's stated window; only falls back to the
    tolerance-widened windows when the stated windows have no common point.
    """
    exact = common_window(
        members,
        cfg.with_(time_tolerance_minutes=0),
    )
    window = exact or common_window(members, cfg)

    if window is None:
        raise ValueError("members have no common departure time")

    return window[0]


def build_new_plan(
    ride_id: str,
    members: list[TripRequest],
    vehicle: VehicleOption,
    cfg: Config,
    gender_unverified: bool = False,
) -> RidePlan:
    """Create a priced ride plan from a group and selected vehicle.

    An unknown fare cannot be used to calculate fare shares. Such vehicles
    must receive a driver quotation before a RidePlan can be created.
    """
    if vehicle.total_fare is None:
        raise ValueError(
            f"{vehicle.vehicle_type} has no confirmed fare. "
            "A manual quotation is required before creating this ride plan."
        )

    members = sorted(
        members,
        key=lambda r: r.request_id,
    )

    return RidePlan(
        ride_id=ride_id,
        kind=NEW,
        pickup_location=members[0].pickup_location,
        destination=members[0].destination,
        departure_time=departure_for(members, cfg),
        vehicle_type=vehicle.vehicle_type,
        passenger_capacity=vehicle.passenger_capacity,
        luggage_capacity=vehicle.luggage_capacity,
        total_fare=vehicle.total_fare,
        fare_status=ESTIMATED_FARE,
        request_ids=[
            member.request_id
            for member in members
        ],
        fare_shares=split_fare(
            vehicle.total_fare,
            {
                member.request_id: member.passengers
                for member in members
            },
        ),
        gender_unverified=gender_unverified,
    )


def unserviceable_reason(
    req: TripRequest,
    catalog: VehicleCatalog,
) -> Unmatched | None:
    """Explain why a request cannot currently get a priced new cab.

    Unknown-fare vehicles remain available for manual confirmation, but
    they cannot be selected by the fare-minimising algorithms.
    """
    if not catalog.has_destination(req.destination):
        return Unmatched(
            req.request_id,
            "NO_VEHICLE_INFO",
            f"'{req.destination}' is not in vehicle_options.csv: "
            "the trip is allowed, but vehicle and fare must be "
            "confirmed manually.",
        )

    priced = catalog.priced_options(req.destination)
    unpriced = catalog.unpriced_options(req.destination)

    # No usable vehicle/fare information is available for this destination.
    # Keep the established public reason code used by the matching workflow.
    if not priced:
        return Unmatched(
            req.request_id,
            "NO_VEHICLE_INFO",
            f"No priced vehicle information is available for '{req.destination}'. "
            "The trip is allowed, but vehicle and fare must be confirmed manually.",
        )

    # At least one priced vehicle can carry this request.
    if catalog.cheapest_fitting(
        req.destination,
        req.passengers,
        req.luggage,
    ) is not None:
        return None

    # A vehicle without a known fare may have enough capacity.
    manual_fit = [
        vehicle
        for vehicle in unpriced
        if (
            vehicle.passenger_capacity >= req.passengers
            and vehicle.luggage_capacity >= req.luggage
        )
    ]

    if manual_fit:
        largest_manual = manual_fit[0]

        return Unmatched(
            req.request_id,
            "NO_PRICED_VEHICLE",
            f"The priced vehicles at '{req.destination}' cannot "
            f"accommodate {req.passengers} passengers and "
            f"{req.luggage} bags. {largest_manual.vehicle_type} "
            "may fit, but its fare requires manual confirmation "
            "or a driver quotation.",
        )

    # Even the largest listed vehicle cannot accommodate the request.
    largest = max(
        catalog.options(req.destination),
        key=lambda vehicle: (
            vehicle.passenger_capacity,
            vehicle.luggage_capacity,
        ),
    )

    return Unmatched(
        req.request_id,
        "NO_VEHICLE_LARGE_ENOUGH",
        f"Needs {req.passengers} seats/{req.luggage} bags; "
        f"largest listed vehicle is {largest.vehicle_type} "
        f"({largest.passenger_capacity}/{largest.luggage_capacity}).",
    )


@dataclass
class _OpenGroup:
    members: list[TripRequest]
    vehicle: VehicleOption
    unverified: bool = False

    @property
    def passengers(self) -> int:
        return sum(
            member.passengers
            for member in self.members
        )

    @property
    def luggage(self) -> int:
        return sum(
            member.luggage
            for member in self.members
        )


def greedy_new_rides(
    requests,
    catalog: VehicleCatalog,
    cfg: Config,
    id_prefix: str = "GRD-NEW",
):
    """Greedy grouping by minimum additional cost.

    Requests are taken in chronological order of their earliest departure.

    For each request:
      * opening a new cab alone costs the cheapest priced vehicle that fits;
      * joining an open group costs the difference between the new group's
        cheapest fitting vehicle and the group's current vehicle fare.

    A group can only be joined if its common departure window remains
    non-empty and gender rules are satisfied.

    Unknown-fare vehicles are excluded from all cost comparisons.
    On a tie, joining an existing group wins (fewer cabs).

    Decisions are never revisited, so this remains a heuristic.
    """
    groups: list[_OpenGroup] = []
    unmatched: list[Unmatched] = []

    ordered_requests = sorted(
        requests,
        key=lambda request: (
            request.earliest_departure,
            request.latest_departure,
            request.request_id,
        ),
    )

    for req in ordered_requests:
        problem = unserviceable_reason(req, catalog)

        if problem:
            unmatched.append(problem)
            continue

        alone = catalog.cheapest_fitting(
            req.destination,
            req.passengers,
            req.luggage,
        )

        # unserviceable_reason() guarantees a priced fitting vehicle exists.
        if alone is None or alone.total_fare is None:
            unmatched.append(
                Unmatched(
                    req.request_id,
                    "NO_PRICED_VEHICLE",
                    "No priced vehicle can accommodate this request. "
                    "Manual fare confirmation is required.",
                )
            )
            continue

        best = (
            (alone.total_fare, 1, 0),
            None,
            alone,
            False,
        )

        for idx, group in enumerate(groups):
            if group.members[0].route != req.route:
                continue

            members = group.members + [req]

            if common_window(members, cfg) is None:
                continue

            gender_check_result = group_gender_check(
                [member.party for member in members],
                cfg,
            )

            if not gender_check_result.ok:
                continue

            vehicle = catalog.cheapest_fitting(
                req.destination,
                group.passengers + req.passengers,
                group.luggage + req.luggage,
            )

            if vehicle is None or vehicle.total_fare is None:
                continue

            additional_cost = (
                vehicle.total_fare - group.vehicle.total_fare
            )

            key = (
                additional_cost,
                0,
                idx,
            )

            if key < best[0]:
                best = (
                    key,
                    group,
                    vehicle,
                    gender_check_result.unverified,
                )

        _, group, vehicle, unverified = best

        if group is None:
            groups.append(
                _OpenGroup(
                    [req],
                    vehicle,
                )
            )
        else:
            group.members.append(req)
            group.vehicle = vehicle
            group.unverified = (
                group.unverified or unverified
            )

    plans = [
        build_new_plan(
            "",
            group.members,
            group.vehicle,
            cfg,
            group.unverified,
        )
        for group in groups
    ]

    return (
        number_new_rides(plans, id_prefix),
        unmatched,
    )


def number_new_rides(
    plans: list[RidePlan],
    prefix: str,
) -> list[RidePlan]:
    """Assign deterministic IDs to newly formed rides."""
    plans.sort(
        key=lambda plan: (
            plan.departure_time,
            plan.destination,
            plan.request_ids,
        )
    )

    for i, plan in enumerate(plans, start=1):
        plan.ride_id = f"{prefix}-{i:03d}"

    return plans
