"""Sensitivity analysis.

Two different things are reported, and they are kept clearly apart:

A. LP-relaxation dual values ("shadow prices"). The binary variables are relaxed
   to 0 <= v <= 1 and the LP is solved with the penalty objective. The dual of a
   constraint is the rate at which the LP optimum changes per unit change of its
   right-hand side, valid only within the LP's basis range. They describe the
   RELAXED problem; they do NOT give the exact sensitivity of the integer optimum.

B. What-if analysis. Input parameters are modified and the INTEGER model is
   re-solved from scratch. These numbers are exact for the integer problem.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from decimal import Decimal

from .config import Config
from .ip_model import penalty_objective_of, run_ip, solve_lp_relaxation
from .models import Dataset
from .reporting import metrics, money
from .verification import verify_solution


def _scale_fares(ds: Dataset, factor: str) -> Dataset:
    f = Decimal(factor)
    return replace(ds, vehicles=tuple(replace(v, total_fare=(v.total_fare * f).quantize(Decimal("0.01")))
                                      for v in ds.vehicles))


def _fare_of(ds: Dataset, vehicle_type: str, factor: str) -> Dataset:
    f = Decimal(factor)
    return replace(ds, vehicles=tuple(
        replace(v, total_fare=(v.total_fare * f).quantize(Decimal("0.01")))
        if v.vehicle_type == vehicle_type else v for v in ds.vehicles))


def _seats(ds: Dataset, delta: int) -> Dataset:
    return replace(ds, vehicles=tuple(replace(v, passenger_capacity=max(1, v.passenger_capacity + delta))
                                      for v in ds.vehicles))


def _bags(ds: Dataset, delta: int) -> Dataset:
    return replace(ds, vehicles=tuple(replace(v, luggage_capacity=max(0, v.luggage_capacity + delta))
                                      for v in ds.vehicles))


def _first_requests(ds: Dataset, fraction: float) -> Dataset:
    n = max(1, round(len(ds.requests) * fraction))
    return replace(ds, requests=tuple(sorted(ds.requests, key=lambda r: r.request_id)[:n]))


def scenarios(base: Dataset, cfg: Config):
    """(group, description, original value, modified value, dataset, config)."""
    yield ("Vehicle fares", "All vehicle fares -20%", "x1.00", "x0.80", _scale_fares(base, "0.8"), cfg)
    yield ("Vehicle fares", "All vehicle fares +20%", "x1.00", "x1.20", _scale_fares(base, "1.2"), cfg)
    yield ("Vehicle fares", "Auto fares +50%", "Auto x1.00", "Auto x1.50", _fare_of(base, "Auto", "1.5"), cfg)
    yield ("Vehicle fares", "MPV fares -40%", "MPV x1.00", "MPV x0.60", _fare_of(base, "MPV", "0.6"), cfg)
    yield ("Passenger capacity", "Every vehicle -1 seat", "listed", "-1 (min 1)", _seats(base, -1), cfg)
    yield ("Passenger capacity", "Every vehicle +1 seat", "listed", "+1", _seats(base, 1), cfg)
    yield ("Luggage capacity", "Every vehicle -1 bag", "listed", "-1 (min 0)", _bags(base, -1), cfg)
    yield ("Luggage capacity", "Every vehicle +1 bag", "listed", "+1", _bags(base, 1), cfg)
    for frac in (0.25, 0.5, 0.75):
        n = max(1, round(len(base.requests) * frac))
        yield ("Number of requests", f"First {n} requests only", str(len(base.requests)), str(n),
               _first_requests(base, frac), cfg)
    for p in (200.0, 500.0, 1000.0):
        yield ("Unserved penalty", f"Penalty objective, P = {p:g}", "lexicographic", f"P = {p:g}",
               base, cfg.with_(objective_mode="penalty", unserved_penalty=p))
    yield ("Unserved penalty", "Penalty objective, P = safe value", "lexicographic", "P = safe (auto)",
           base, cfg.with_(objective_mode="penalty", unserved_penalty=None))
    for tol in (30, 60, 120):
        yield ("Time tolerance (extra)", f"Windows widened by +/-{tol} min",
               f"{cfg.time_tolerance_minutes} min", f"{tol} min", base,
               cfg.with_(time_tolerance_minutes=tol))


def run_sensitivity(dataset: Dataset, cfg: Config) -> dict:
    base_sol = run_ip(dataset, cfg)
    base_m = metrics(base_sol, dataset)
    rows = []
    for group, desc, orig, mod, ds, c in scenarios(dataset, cfg):
        sol = run_ip(ds, c)
        m = metrics(sol, ds)
        errors = verify_solution(sol, ds, c)
        rows.append({"group": group, "scenario": desc, "original": orig, "modified": mod,
                     "status": sol.status, "verified": not errors,
                     "cost_before": base_m["new_cab_cost"], "cost_after": m["new_cab_cost"],
                     "served_before": base_m["requests_served"], "served_after": m["requests_served"],
                     "requests_total": len(ds.requests),
                     "cabs_before": base_m["total_cabs"], "cabs_after": m["total_cabs"],
                     "existing_before": base_m["existing_cabs_used"],
                     "existing_after": m["existing_cabs_used"],
                     "vehicles_before": base_m["vehicle_types"], "vehicles_after": m["vehicle_types"],
                     "interpretation": _interpret(group, base_m, m, len(ds.requests))})
    lp = solve_lp_relaxation(dataset, cfg)
    ip_pen = penalty_objective_of(base_sol, lp["penalty"])
    return {"base": base_m, "base_status": base_sol.status, "rows": rows, "lp": lp,
            "ip_penalty_objective": ip_pen}


def _interpret(group, before, after, n_requests) -> str:
    dc = after["new_cab_cost"] - before["new_cab_cost"]
    ds = after["requests_served"] - before["requests_served"]
    vt_change = before["vehicle_types"] != after["vehicle_types"]
    if group == "Number of requests":
        return (f"With {n_requests} requests the cost is {money(after['new_cab_cost'])}; "
                f"{after['shared_new_cabs']} new cab(s) are shared. Fewer requests means fewer "
                f"sharing opportunities, so cost falls roughly in proportion.")
    if group == "Unserved penalty":
        extra = after["requests_unmatched"] - before["requests_unmatched"]
        if extra > 0:
            return (f"P is below the fare of some cabs, so the optimiser leaves {extra} more "
                    f"request(s) unserved than the lexicographic model: the penalty DISTORTS the "
                    f"objective. P must exceed any achievable cost saving.")
        return "Penalty is large enough: same requests served and same cost as the lexicographic objective."
    parts = [f"cost {'+' if dc >= 0 else '-'}{money(abs(dc))}"]
    if ds:
        parts.append(f"{ds:+d} request(s) served")
    if after["existing_cabs_used"] != before["existing_cabs_used"]:
        parts.append(f"booked cabs used {before['existing_cabs_used']} -> {after['existing_cabs_used']}")
    if vt_change:
        parts.append("vehicle mix changed")
    else:
        parts.append("vehicle mix unchanged")
    return "; ".join(parts) + "."


def sensitivity_markdown(result: dict) -> str:
    b = result["base"]
    out = ["# Sensitivity analysis report", "",
           f"Base case (IP, status {result['base_status']}): cost {money(b['new_cab_cost'])}, "
           f"{b['requests_served']} requests served, {b['total_cabs']} cabs, "
           f"vehicles: {', '.join(f'{k} x{v}' for k, v in b['vehicle_types'].items())}.", "",
           "## Part B - What-if analysis (integer model re-solved for each change)", "",
           "These results are exact for the integer problem.", "",
           "| Parameter | Scenario | Original | Modified | Status | Cost before | Cost after | "
           "Served before | Served after | Cabs before -> after | Interpretation |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in result["rows"]:
        status = r["status"] + ("" if r["verified"] else " (VERIFICATION FAILED)")
        out.append(f"| {r['group']} | {r['scenario']} | {r['original']} | {r['modified']} | {status} | "
                   f"{money(r['cost_before'])} | {money(r['cost_after'])} | {r['served_before']} | "
                   f"{r['served_after']}/{r['requests_total']} | {r['cabs_before']} -> {r['cabs_after']} | "
                   f"{r['interpretation']} |")
    out += ["", "### Vehicle usage per scenario", "", "| Scenario | Vehicles before | Vehicles after |",
            "|---|---|---|"]
    fmt = lambda d: ", ".join(f"{k} x{v}" for k, v in d.items()) or "none"
    for r in result["rows"]:
        out.append(f"| {r['scenario']} | {fmt(r['vehicles_before'])} | {fmt(r['vehicles_after'])} |")

    lp = result["lp"]
    duals = lp["duals"]
    assign = {k[len("assign_"):]: v for k, v in duals.items() if k.startswith("assign_") and v is not None}
    seats = {k: v for k, v in duals.items() if k.startswith(("seats_", "luggage_")) and v}
    out += ["", "## Part A - LP relaxation and shadow prices", "",
            "> Shadow prices below come from the **LP relaxation** (binaries replaced by continuous variables >= 0; the bound 1 is implied by the assignment equalities). "
            "They describe the relaxed problem only and are valid for small changes; they do **not** "
            "directly give the sensitivity of the integer optimum. Part B is the exact integer answer.", "",
            f"- LP status: {lp['status']}; model size: {lp['size']}",
            f"- Penalty used in the relaxed objective: P = {lp['penalty']:,.2f}",
            f"- LP optimum (cost + P x unserved): {lp['objective']:,.2f} "
            f"(cost part {lp['cost']:,.2f}, unserved {lp['unserved']:.3f})",
            f"- IP optimum of the same objective: {result['ip_penalty_objective']:,.2f}",
            f"- Integrality gap: {result['ip_penalty_objective'] - lp['objective']:,.2f} "
            f"({(result['ip_penalty_objective'] - lp['objective']) / result['ip_penalty_objective']:.2%} "
            "of the IP optimum)" if result["ip_penalty_objective"] else "- Integrality gap: n/a", "",
            "### Assignment constraints (one per request)", "",
            "Dual of `sum x + sum z + u = 1`: the increase of the relaxed optimum if request *i* had "
            "to be served 'one more unit' - the marginal cost of serving that request. A value "
            "equal to P only occurs for a request that cannot be served at all.", ""]
    values = sorted(assign.items(), key=lambda kv: -kv[1])
    if values:
        dist = Counter(round(v, 2) for _, v in values)
        out += ["| Shadow price (Rs) | Number of requests |", "|---|---|"]
        out += [f"| {price:,.2f} | {n} |" for price, n in sorted(dist.items(), reverse=True)]
        out += ["", "Highest five: " + ", ".join(f"{k} ({v:,.2f})" for k, v in values[:5]),
                "Lowest five: " + ", ".join(f"{k} ({v:,.2f})" for k, v in values[-5:]), ""]
    out += ["### Booked-cab seat and luggage constraints", ""]
    if seats:
        out += ["| Constraint | Shadow price |", "|---|---|"]
        out += [f"| {k} | {v:,.2f} |" for k, v in sorted(seats.items())]
        out += ["", "A negative value is the cost reduction per extra seat/bag on that booked cab."]
    else:
        out += ["All zero or absent: no booked-cab capacity constraint is binding in the LP (on the "
                "provided data no request is compatible with any booked cab at the base settings), "
                "so one more seat on a booked cab would not change the relaxed optimum."]
    out.append("")
    return "\n".join(out)
