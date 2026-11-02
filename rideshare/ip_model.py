
"""Integer Programming model of the whole ride-sharing problem (PuLP + CBC).

Both workflows are decided in ONE model, so the solver can trade off joining a
booked cab against forming a new one. Full formulation: docs/IP_MODEL.md.

Sets
    I            valid student requests
    R            booked cabs (available_rides.csv)
    K            potential new cabs; one per request k ("k leads the cab").
                 A request may only join a new cab led by an EARLIER request of
                 the same route (symmetry breaking: every group is represented
                 exactly once, by its earliest member)
    V(k)         vehicle options for the destination of k

Binary variables
    x[i,r] = 1   request i joins booked cab r            (only compatible pairs exist)
    z[i,k] = 1   request i rides in new cab k            (z[k,k] = 1: cab k is created)
    y[k,v] = 1   vehicle v is selected for new cab k
    u[i]   = 1   request i stays unserved

Objective (lexicographic, default)
    stage 1:  min  sum_i u[i]
    stage 2:  min  sum_k sum_v fare[v] * y[k,v]
              subject to sum_i u[i] <= optimum of stage 1

Objective (penalty mode)
    min  sum fare[v] y[k,v] + P * sum u[i],
    with P > any achievable total fare.

Fares of already-booked cabs are sunk (paid whether or not anyone joins), so
the objective counts only the fare of NEW cabs: the additional money required.

Vehicles without a known fare are excluded from new-cab cost optimisation.
For example, an Auto without a driver quotation is not assigned an invented fare.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

import pulp

from .compatibility import gender_check, group_gender_check, requests_can_share
from .config import Config
from .existing_ride_matching import (
    build_existing_plan,
    diagnose,
    static_join_check,
)
from .greedy import invalid_unmatched
from .models import Dataset, Solution, TripRequest, Unmatched
from .new_ride_grouping import (
    build_new_plan,
    number_new_rides,
    unserviceable_reason,
)
from .vehicle_selection import VehicleCatalog


STATUS_MAP = {
    1: "OPTIMAL",
    2: "FEASIBLE",
    -1: "INFEASIBLE",
    -2: "FAILED",
    0: "FAILED",
}


def _name(*parts) -> str:
    return re.sub(r"\W", "_", "_".join(str(p) for p in parts))


@dataclass
class RideShareModel:
    prob: pulp.LpProblem
    requests: dict[str, TripRequest]
    x: dict[tuple[str, str], pulp.LpVariable] = field(default_factory=dict)
    z: dict[tuple[str, str], pulp.LpVariable] = field(default_factory=dict)
    y: dict[tuple[str, int], pulp.LpVariable] = field(default_factory=dict)
    u: dict[str, pulp.LpVariable] = field(default_factory=dict)
    vehicles: dict[str, list] = field(default_factory=dict)
    members: dict[str, list[str]] = field(default_factory=dict)
    cost: pulp.LpAffineExpression | None = None
    unserved: pulp.LpAffineExpression | None = None
    penalty: float = 0.0

    @property
    def size(self) -> str:
        return (
            f"{len(self.prob.variables())} variables "
            f"(x={len(self.x)}, z={len(self.z)}, "
            f"y={len(self.y)}, u={len(self.u)}), "
            f"{len(self.prob.constraints)} constraints"
        )


def _priced_options(catalog: VehicleCatalog, destination: str) -> list:
    """Return only vehicles with a known fare."""
    return catalog.priced_options(destination)


def safe_penalty(model: RideShareModel) -> float:
    """Calculate a penalty that preserves the lexicographic objective.

    Every new cab uses at most one vehicle. Therefore, the maximum possible
    total new-cab fare is bounded by the sum of the most expensive priced
    vehicle option for every potential cab.

    P = C_max + 1 ensures that leaving one additional request unserved costs
    more than any possible fare saving.
    """
    c_max = sum(
        float(max(option.total_fare for option in options))
        for options in model.vehicles.values()
        if options
    )
    return c_max + 1.0


def build_model(
    dataset: Dataset,
    cfg: Config,
    relax: bool = False,
) -> RideShareModel:
    cat = pulp.LpContinuous if relax else pulp.LpBinary

    # In the LP relaxation the upper bound 1 is left implicit.
    var = lambda name: pulp.LpVariable(
        name,
        lowBound=0,
        upBound=None if relax else 1,
        cat=cat,
    )

    prob = pulp.LpProblem("IITP_RideShare", pulp.LpMinimize)
    reqs = {r.request_id: r for r in dataset.requests}
    m = RideShareModel(prob, reqs)
    catalog = VehicleCatalog(dataset.vehicles)

    # x[i,r]: only create variables for compatible booked-cab pairs.
    for ride in dataset.rides:
        for req in dataset.requests:
            if static_join_check(req, ride, cfg).ok:
                m.x[
                    req.request_id,
                    ride.ride_id,
                ] = var(_name("x", req.request_id, ride.ride_id))

    # z[i,k], y[k,v]: potential new cabs.
    # Only vehicles with known fares are included in the optimisation.
    by_route = defaultdict(list)

    for req in dataset.requests:
        by_route[req.route].append(req)

    for route_reqs in by_route.values():
        route_reqs.sort(
            key=lambda r: (
                r.earliest_departure,
                r.latest_departure,
                r.request_id,
            )
        )

        for pos, leader in enumerate(route_reqs):
            if unserviceable_reason(leader, catalog):
                continue

            # Exclude unknown fares instead of treating them as zero.
            options = _priced_options(catalog, leader.destination)

            if not options:
                continue

            k = leader.request_id
            m.vehicles[k] = options

            m.members[k] = [k] + [
                r.request_id
                for r in route_reqs[pos + 1:]
                if not unserviceable_reason(r, catalog)
                and requests_can_share(leader, r, cfg)
            ]

            for i in m.members[k]:
                m.z[i, k] = var(_name("z", i, k))

            for v_idx, _ in enumerate(m.vehicles[k]):
                m.y[k, v_idx] = var(_name("y", k, v_idx))

    # Every request has an unserved variable.
    for i in reqs:
        m.u[i] = var(_name("u", i))

    # (1) + (11): every request is served exactly once or left unserved.
    for i in reqs:
        prob += (
            pulp.lpSum(
                v for (ii, _), v in m.x.items() if ii == i
            )
            + pulp.lpSum(
                v for (ii, _), v in m.z.items() if ii == i
            )
            + m.u[i]
            == 1
        ), _name("assign", i)

    # (3) + (4): remaining seats and luggage of booked cabs.
    for ride in dataset.rides:
        cand = [
            i for (i, r) in m.x
            if r == ride.ride_id
        ]

        if not cand:
            continue

        prob += (
            pulp.lpSum(
                reqs[i].passengers * m.x[i, ride.ride_id]
                for i in cand
            )
            <= ride.remaining_seats
        ), _name("seats", ride.ride_id)

        prob += (
            pulp.lpSum(
                reqs[i].luggage * m.x[i, ride.ride_id]
                for i in cand
            )
            <= ride.remaining_luggage
        ), _name("luggage", ride.ride_id)

        # (8): gender compatibility between joiners of the same booked cab.
        for a, b in combinations(cand, 2):
            if not gender_check(
                reqs[a].party,
                reqs[b].party,
                cfg,
            ).ok:
                prob += (
                    m.x[a, ride.ride_id]
                    + m.x[b, ride.ride_id]
                    <= 1
                ), _name("xconf", ride.ride_id, a, b)

    # Constraints for newly formed cabs.
    for k, members in m.members.items():
        opts = m.vehicles[k]

        # A request can only join a new cab if its leader creates it.
        for i in members[1:]:
            prob += (
                m.z[i, k] <= m.z[k, k]
            ), _name("link", k, i)

        # A created cab selects exactly one vehicle.
        prob += (
            pulp.lpSum(
                m.y[k, v]
                for v in range(len(opts))
            )
            == m.z[k, k]
        ), _name("vehicle", k)

        # (3): passenger capacity.
        prob += (
            pulp.lpSum(
                reqs[i].passengers * m.z[i, k]
                for i in members
            )
            <= pulp.lpSum(
                option.passenger_capacity * m.y[k, v]
                for v, option in enumerate(opts)
            )
        ), _name("nseats", k)

        # (4): luggage capacity.
        prob += (
            pulp.lpSum(
                reqs[i].luggage * m.z[i, k]
                for i in members
            )
            <= pulp.lpSum(
                option.luggage_capacity * m.y[k, v]
                for v, option in enumerate(opts)
            )
        ), _name("nluggage", k)

        # (5) + (8): incompatible members cannot share a cab.
        for a, b in combinations(members[1:], 2):
            if not requests_can_share(
                reqs[a],
                reqs[b],
                cfg,
            ):
                prob += (
                    m.z[a, k] + m.z[b, k] <= 1
                ), _name("zconf", k, a, b)

    # The objective counts only fares of newly created cabs.
    m.cost = pulp.lpSum(
        float(option.total_fare) * m.y[k, v]
        for k, options in m.vehicles.items()
        for v, option in enumerate(options)
    )

    m.unserved = pulp.lpSum(m.u.values())
    m.penalty = (
        cfg.unserved_penalty
        if cfg.unserved_penalty
        else safe_penalty(m)
    )

    return m


def _solve(m: RideShareModel, cfg: Config) -> str:
    m.prob.solve(
        pulp.PULP_CBC_CMD(
            msg=False,
            timeLimit=cfg.solver_time_limit_seconds,
        )
    )
    return STATUS_MAP.get(m.prob.sol_status, "FAILED")


def _val(v) -> float:
    return v.value() or 0.0


def run_ip(dataset: Dataset, cfg: Config) -> Solution:
    started = time.perf_counter()
    m = build_model(dataset, cfg)
    notes = [f"Model size: {m.size}"]

    if cfg.objective_mode == "lexicographic":
        # Stage 1: maximise the number of requests served.
        m.prob.setObjective(m.unserved)
        status1 = _solve(m, cfg)

        notes.append(
            f"Stage 1 (maximise requests served): {status1}"
        )

        if status1 not in ("OPTIMAL", "FEASIBLE"):
            return _failed(
                dataset,
                status1,
                "stage 1 did not produce a solution",
                notes,
                started,
            )

        best_unserved = round(pulp.value(m.unserved))

        # Stage 2: minimise fare without serving fewer requests.
        m.prob += (
            m.unserved <= best_unserved,
            "lexicographic_served_floor",
        )
        m.prob.setObjective(m.cost)
        status = _solve(m, cfg)

        notes.append(
            f"Stage 2 (minimise cost with at most "
            f"{best_unserved} unserved): {status}"
        )

        if status1 == "FEASIBLE" and status == "OPTIMAL":
            status = "FEASIBLE"

    else:
        m.prob.setObjective(
            m.cost + m.penalty * m.unserved
        )
        status = _solve(m, cfg)

        notes.append(
            f"Penalty objective, P = {m.penalty:,.2f} "
            f"per unserved request: {status}"
        )

    if status not in ("OPTIMAL", "FEASIBLE"):
        return _failed(
            dataset,
            status,
            "solver did not produce a solution",
            notes,
            started,
        )

    solution = _extract(
        m,
        dataset,
        cfg,
        status,
        notes,
    )

    solution.objective_value = pulp.value(m.prob.objective)
    solution.runtime_seconds = time.perf_counter() - started

    return solution


def _failed(
    dataset,
    status,
    detail,
    notes,
    started,
) -> Solution:
    unmatched = [
        Unmatched(
            r.request_id,
            "NOT_SELECTED",
            f"IP solver status {status}",
        )
        for r in dataset.requests
    ]

    return Solution(
        "Integer Programming",
        [],
        unmatched + invalid_unmatched(dataset),
        status,
        detail,
        None,
        time.perf_counter() - started,
        notes,
    )


def _extract(
    m: RideShareModel,
    dataset: Dataset,
    cfg: Config,
    status: str,
    notes: list[str],
) -> Solution:
    reqs = m.requests
    rides_by_id = {
        r.ride_id: r
        for r in dataset.rides
    }

    catalog = VehicleCatalog(dataset.vehicles)
    plans = []
    joiners = defaultdict(list)

    # Extract students assigned to existing booked cabs.
    for (i, r), v in m.x.items():
        if _val(v) > 0.5:
            joiners[r].append(reqs[i])

    for r, js in sorted(joiners.items()):
        ride = rides_by_id[r]

        g = group_gender_check(
            [ride.booker_party] + [j.party for j in js],
            cfg,
        )

        plans.append(
            build_existing_plan(
                ride,
                js,
                g.unverified,
            )
        )

    # Extract newly formed groups.
    new_plans = []

    for k, members in m.members.items():
        if _val(m.z[k, k]) <= 0.5:
            continue

        chosen = [
            reqs[i]
            for i in members
            if _val(m.z[i, k]) > 0.5
        ]

        v_idx = next(
            v
            for v in range(len(m.vehicles[k]))
            if _val(m.y[k, v]) > 0.5
        )

        g = group_gender_check(
            [c.party for c in chosen],
            cfg,
        )

        new_plans.append(
            build_new_plan(
                "",
                chosen,
                m.vehicles[k][v_idx],
                cfg,
                g.unverified,
            )
        )

    number_new_rides(new_plans, "IP-NEW")

    unmatched = []
    w1_reasons = {}

    joined = {
        j.request_id
        for js in joiners.values()
        for j in js
    }

    for i, req in reqs.items():
        if i not in joined:
            if any(ii == i for (ii, _) in m.x):
                w1_reasons[i] = (
                    "CAPACITY_TAKEN",
                    "a compatible booked cab exists but the "
                    "optimiser placed the request elsewhere",
                )
            else:
                w1_reasons[i] = diagnose(
                    req,
                    dataset.rides,
                    cfg,
                )

        if _val(m.u[i]) > 0.5:
            problem = unserviceable_reason(req, catalog)

            has_booked_option = any(
                ii == i
                for (ii, _) in m.x
            )

            if problem and not has_booked_option:
                unmatched.append(problem)

            elif problem:
                unmatched.append(
                    Unmatched(
                        i,
                        "CAPACITY_TAKEN",
                        "compatible booked cab(s) were needed for "
                        "other requests; no new cab possible: "
                        f"{problem.detail}",
                    )
                )

            else:
                unmatched.append(
                    Unmatched(
                        i,
                        "NOT_SELECTED",
                        f"left unserved: serving it costs more than "
                        f"the penalty P = {m.penalty:,.2f}",
                    )
                )

    return Solution(
        "Integer Programming",
        plans + new_plans,
        unmatched + invalid_unmatched(dataset),
        status,
        f"CBC via PuLP {pulp.__version__}, "
        f"objective mode: {cfg.objective_mode}",
        notes=notes,
        existing_cab_reasons=w1_reasons,
    )

def solve_lp_relaxation(
    dataset: Dataset,
    cfg: Config,
) -> dict:
    """LP relaxation (0 <= vars <= 1, penalty objective) with dual values.

    The penalty objective is used because duals of a lexicographic second
    stage would also reflect the artificial served-floor constraint.
    """
    m = build_model(
        dataset,
        cfg,
        relax=True,
    )

    m.prob.setObjective(
        m.cost + m.penalty * m.unserved
    )

    m.prob.solve(
        pulp.PULP_CBC_CMD(
            msg=False,
            timeLimit=cfg.solver_time_limit_seconds,
        )
    )

    status = pulp.LpStatus[m.prob.status]
    duals = {
        name: constraint.pi
        for name, constraint in m.prob.constraints.items()
    }

    return {
        "status": status,
        "objective": pulp.value(m.prob.objective),
        "penalty": m.penalty,
        "cost": pulp.value(m.cost),
        "unserved": pulp.value(m.unserved),
        "duals": duals,
        "size": m.size,
    }


def penalty_objective_of(
    solution: Solution,
    penalty: float,
) -> float:
    unserved = sum(
        1
        for item in solution.unmatched
        if item.reason_code != "INVALID_RECORD"
    )

    return float(solution.new_cab_cost) + penalty * unserved
