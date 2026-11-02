"""Greedy baseline: Workflow 1 (join booked cabs) followed by Workflow 2 (new cabs).

This is a HEURISTIC. Each step takes the locally best decision and never
revisits it, so the result is feasible but not guaranteed to be globally
optimal. The IP model (ip_model.py) solves the same problem exactly.
"""
from __future__ import annotations

import time

from .config import Config
from .existing_ride_matching import build_existing_plan, greedy_match_existing
from .models import Dataset, Solution, Unmatched
from .new_ride_grouping import greedy_new_rides
from .vehicle_selection import VehicleCatalog


def invalid_unmatched(dataset: Dataset) -> list[Unmatched]:
    return [Unmatched(rid, "INVALID_RECORD", "; ".join(msgs))
            for rid, msgs in sorted(dataset.invalid_requests.items())]


def run_greedy(dataset: Dataset, cfg: Config) -> Solution:
    started = time.perf_counter()
    rides_by_id = {r.ride_id: r for r in dataset.rides}

    joiners, w1_reasons, unverified = greedy_match_existing(dataset.requests, dataset.rides, cfg)
    plans = [build_existing_plan(rides_by_id[rid], js, rid in unverified)
             for rid, js in sorted(joiners.items())]

    leftover = [r for r in dataset.requests if r.request_id in w1_reasons]
    new_plans, unmatched = greedy_new_rides(leftover, VehicleCatalog(dataset.vehicles), cfg)

    solution = Solution(
        algorithm="Greedy", rides=plans + new_plans,
        unmatched=unmatched + invalid_unmatched(dataset), status="HEURISTIC",
        status_detail="Greedy heuristic - feasible, no optimality guarantee",
        runtime_seconds=time.perf_counter() - started,
        existing_cab_reasons=w1_reasons)
    solution.objective_value = float(solution.new_cab_cost)
    return solution
