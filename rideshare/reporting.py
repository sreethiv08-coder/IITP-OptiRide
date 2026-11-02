
"""Result reporting: metrics, Markdown reports, CSV/JSON exports."""
from __future__ import annotations

import csv
import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .compatibility import REASONS
from .config import Config
from .fares import savings_summary
from .models import EXISTING, Dataset, Solution

GREEDY_DISCLAIMER = (
    "The Greedy algorithm is a heuristic: it makes locally best choices and "
    "never revisits them, so it does not guarantee a globally optimal solution."
)

UNKNOWN_FARE_TEXT = "Driver quotation required"


def _decimal_or_zero(value) -> Decimal:
    """Convert a monetary value to Decimal, treating missing values as zero for totals."""
    if value is None:
        return Decimal("0")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return Decimal("0")
        return amount
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def money(value) -> str:
    """Format money without ever presenting an unknown fare as zero."""
    if value is None:
        return UNKNOWN_FARE_TEXT

    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return UNKNOWN_FARE_TEXT
        return f"Rs {amount:,.2f}"
    except (InvalidOperation, ValueError, TypeError):
        return UNKNOWN_FARE_TEXT


def _fare_json(value):
    """Return a JSON-safe monetary value; unknown fares remain null."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return None
        return str(amount)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fare_csv(value):
    """Return an empty CSV cell for an unknown fare."""
    return "" if value is None else _fare_json(value) or ""


def metrics(solution: Solution, dataset: Dataset) -> dict:
    reqs = {r.request_id: r for r in dataset.requests}
    used = [p for p in solution.rides if p.request_ids]

    utilisation = [
        (
            p.base_passengers
            + sum(reqs[i].passengers for i in p.request_ids if i in reqs)
        ) / p.passenger_capacity
        for p in used
        if p.passenger_capacity > 0
    ]

    paid_by_requests = sum(
        (
            _decimal_or_zero(share)
            for p in used
            for key, share in p.fare_shares.items()
            if key in reqs
        ),
        Decimal("0"),
    )

    unmatched_valid = [
        u for u in solution.unmatched
        if u.reason_code != "INVALID_RECORD"
    ]

    fare_of_all_cabs_used = sum(
        (_decimal_or_zero(p.total_fare) for p in used),
        Decimal("0"),
    )

    return {
        "status": solution.status,
        "new_cab_cost": _decimal_or_zero(solution.new_cab_cost),
        "paid_by_requests": paid_by_requests,
        "fare_of_all_cabs_used": fare_of_all_cabs_used,
        "requests_served": len(solution.served_ids),
        "passengers_served": sum(
            reqs[i].passengers for i in solution.served_ids if i in reqs
        ),
        "requests_unmatched": len(unmatched_valid),
        "invalid_records": len(solution.unmatched) - len(unmatched_valid),
        "existing_cabs_used": len(solution.existing_used),
        "new_cabs": len(solution.new_rides),
        "total_cabs": len(used),
        "avg_utilisation": (
            sum(utilisation) / len(utilisation) if utilisation else 0.0
        ),
        "vehicle_types": dict(
            sorted(Counter(p.vehicle_type for p in solution.new_rides).items())
        ),
        "shared_new_cabs": sum(
            1 for p in solution.new_rides if len(p.request_ids) > 1
        ),
        "runtime_seconds": solution.runtime_seconds,
    }


def _verification_block(errors: list[str]) -> list[str]:
    if not errors:
        return [
            "**Verification: PASSED** - every rule in section 13 was re-checked "
            "independently against the input data.",
            "",
        ]

    return (
        ["**Verification: FAILED - this solution must NOT be treated as valid.**", ""]
        + [f"- {error}" for error in errors]
        + [""]
    )


def solution_markdown(
    solution: Solution,
    dataset: Dataset,
    cfg: Config,
    errors: list[str],
) -> str:
    reqs = {r.request_id: r for r in dataset.requests}
    m = metrics(solution, dataset)

    out = [f"# {solution.algorithm} solution report", ""]

    if solution.algorithm == "Greedy":
        out += [f"> {GREEDY_DISCLAIMER}", ""]

    out += _verification_block(errors)

    out += [
        "## Settings",
        "",
        f"- Time tolerance: {cfg.time_tolerance_minutes} min",
        f"- Gender matching: {'on' if cfg.gender_matching else 'off'}, "
        f"missing-information policy: {cfg.missing_gender_policy}",
        f"- Solver status: **{solution.status}** - {solution.status_detail}",
    ]

    out += [f"- {note}" for note in solution.notes]

    out += [
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Requests served | {m['requests_served']} "
        f"({m['passengers_served']} passengers) |",
        f"| Requests left unmatched | {m['requests_unmatched']} |",
        f"| Invalid records (excluded, reported below) | {m['invalid_records']} |",
        f"| Existing booked cabs utilised | {m['existing_cabs_used']} |",
        f"| New cabs created | {m['new_cabs']} "
        f"(of which shared: {m['shared_new_cabs']}) |",
        f"| Vehicle types selected for new cabs | "
        f"{', '.join(f'{k} x{v}' for k, v in m['vehicle_types'].items()) or 'none'} |",
        f"| **Total transportation cost** (fare of new cabs) | "
        f"**{money(m['new_cab_cost'])}** |",
        f"| Paid by requesting students (new cabs + shares of booked cabs) | "
        f"{money(m['paid_by_requests'])} |",
        f"| Average seat utilisation of cabs used | "
        f"{m['avg_utilisation']:.1%} |",
        "",
        "Booked cabs are already paid for, so their fare is not an additional cost; "
        "joining students take over part of it from the booker (see shares below).",
        "",
    ]

    out += ["## Existing booked cabs utilised", ""]

    if not solution.existing_used:
        out += ["None.", ""]

    for p in solution.existing_used:
        pax = p.base_passengers + sum(
            reqs[i].passengers for i in p.request_ids if i in reqs
        )
        lug = p.base_luggage + sum(
            reqs[i].luggage for i in p.request_ids if i in reqs
        )

        out += [
            f"### {p.ride_id} - {p.vehicle_type} to {p.destination}, "
            f"{p.departure_time:%Y-%m-%d %H:%M} (booked by {p.booker})",
            "",
            f"Fare {money(p.total_fare)} ({p.fare_status}). "
            f"Seats {pax}/{p.passenger_capacity} "
            f"(remaining {p.passenger_capacity - pax}), "
            f"luggage {lug}/{p.luggage_capacity} "
            f"(remaining {p.luggage_capacity - lug})."
            + (
                " **Gender preferences unverified (lenient policy).**"
                if p.gender_unverified else ""
            ),
            "",
        ] + _share_table(p, reqs) + [""]

    out += ["## New cabs created", ""]

    if not solution.new_rides:
        out += ["None.", ""]
    else:
        out += [
            "| Cab | Destination | Departure | Vehicle | Seats | Bags | Fare | Requests |",
            "|---|---|---|---|---|---|---|---|",
        ]

        for p in solution.new_rides:
            pax = sum(reqs[i].passengers for i in p.request_ids if i in reqs)
            lug = sum(reqs[i].luggage for i in p.request_ids if i in reqs)

            out.append(
                f"| {p.ride_id} | {p.destination} | "
                f"{p.departure_time:%Y-%m-%d %H:%M} | "
                f"{p.vehicle_type} | {pax}/{p.passenger_capacity} | "
                f"{lug}/{p.luggage_capacity} | "
                f"{money(p.total_fare)} ({p.fare_status}) | "
                f"{', '.join(p.request_ids)} |"
            )

        out += [
            "",
            "### Individual fare shares (new cabs)",
            "",
            "| Request | Student | Cab | Passengers | Share | Solo fare | Savings |",
            "|---|---|---|---|---|---|---|",
        ]

        for p in solution.new_rides:
            for i in p.request_ids:
                if i not in reqs:
                    continue

                share = p.fare_shares.get(i)
                s = savings_summary(reqs[i], share)

                out.append(
                    f"| {i} | {reqs[i].student_name} | {p.ride_id} | "
                    f"{reqs[i].passengers} | {money(share)} | "
                    f"{money(s['solo_fare'])} | {s['verdict']} |"
                )

        out.append("")

    out += ["## Requests left unmatched", ""]

    if not solution.unmatched:
        out += ["None - every valid request was served.", ""]
    else:
        out += [
            "| Request | Reason | Detail |",
            "|---|---|---|",
        ]

        for u in sorted(solution.unmatched, key=lambda u: u.request_id):
            out.append(
                f"| {u.request_id} | "
                f"{REASONS.get(u.reason_code, u.reason_code)} | {u.detail} |"
            )

        out.append("")

    out += [
        "## Why requests did not join a booked cab (Workflow 1)",
        "",
        "| Reason | Requests |",
        "|---|---|",
    ]

    by_reason = Counter(
        code for code, _ in solution.existing_cab_reasons.values()
    )

    for code, count in by_reason.most_common():
        out.append(f"| {REASONS.get(code, code)} | {count} |")

    out += [
        "",
        "Per-request detail is in the `*_workflow1_reasons.csv` export.",
        "",
    ]

    return "\n".join(out)


def _share_table(p, reqs) -> list[str]:
    rows = [
        "| Party | Passengers | Share | Solo fare | Savings |",
        "|---|---|---|---|---|",
    ]

    for key, share in p.fare_shares.items():
        if key in reqs:
            s = savings_summary(reqs[key], share)

            rows.append(
                f"| {key} ({reqs[key].student_name}) | "
                f"{reqs[key].passengers} | {money(share)} | "
                f"{money(s['solo_fare'])} | {s['verdict']} |"
            )
        else:
            rows.append(
                f"| {key} (original booker) | {p.base_passengers} | "
                f"{money(share)} | - | "
                f"was paying {money(p.total_fare)} alone |"
            )

    return rows


def export_solution(
    solution: Solution,
    dataset: Dataset,
    cfg: Config,
    errors: list[str],
    out_dir: Path,
    stem: str,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)

    reqs = {r.request_id: r for r in dataset.requests}

    paths = {
        "report": out_dir / f"{stem}_report.md",
        "assignments": out_dir / f"{stem}_assignments.csv",
        "unmatched": out_dir / f"{stem}_unmatched.csv",
        "workflow1": out_dir / f"{stem}_workflow1_reasons.csv",
        "json": out_dir / f"{stem}_solution.json",
    }

    paths["report"].write_text(
        solution_markdown(solution, dataset, cfg, errors),
        encoding="utf-8",
    )

    with paths["assignments"].open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        w = csv.writer(fh)

        w.writerow([
            "request_id",
            "student_name",
            "ride_id",
            "ride_kind",
            "vehicle_type",
            "destination",
            "departure_time",
            "passengers",
            "luggage",
            "total_fare",
            "fare_status",
            "fare_share",
            "solo_fare",
            "savings",
            "savings_note",
        ])

        for p in solution.rides:
            for i in p.request_ids:
                if i not in reqs:
                    continue

                share = p.fare_shares.get(i)
                s = savings_summary(reqs[i], share)

                w.writerow([
                    i,
                    reqs[i].student_name,
                    p.ride_id,
                    p.kind,
                    p.vehicle_type,
                    p.destination,
                    f"{p.departure_time:%Y-%m-%d %H:%M}",
                    reqs[i].passengers,
                    reqs[i].luggage,
                    _fare_csv(p.total_fare),
                    p.fare_status,
                    _fare_csv(share),
                    _fare_csv(s["solo_fare"]),
                    _fare_csv(s["savings"]),
                    s["verdict"],
                ])

    with paths["unmatched"].open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        w = csv.writer(fh)
        w.writerow(["request_id", "reason_code", "reason", "detail"])

        for u in solution.unmatched:
            w.writerow([
                u.request_id,
                u.reason_code,
                REASONS.get(u.reason_code, ""),
                u.detail,
            ])

    with paths["workflow1"].open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        w = csv.writer(fh)
        w.writerow(["request_id", "reason_code", "reason", "detail"])

        for rid, (code, detail) in sorted(
            solution.existing_cab_reasons.items()
        ):
            w.writerow([
                rid,
                code,
                REASONS.get(code, ""),
                detail,
            ])

    paths["json"].write_text(
        json.dumps(solution_to_json(solution, errors), indent=2),
        encoding="utf-8",
    )

    return paths


def solution_to_json(solution: Solution, errors: list[str]) -> dict:
    return {
        "algorithm": solution.algorithm,
        "status": solution.status,
        "verified": not errors,
        "verification_errors": errors,
        "rides": [
            {
                "ride_id": p.ride_id,
                "kind": p.kind,
                "pickup_location": p.pickup_location,
                "destination": p.destination,
                "departure_time": p.departure_time.isoformat(),
                "vehicle_type": p.vehicle_type,
                "passenger_capacity": p.passenger_capacity,
                "luggage_capacity": p.luggage_capacity,
                "total_fare": _fare_json(p.total_fare),
                "fare_display": money(p.total_fare),
                "fare_status": p.fare_status,
                "request_ids": p.request_ids,
                "fare_shares": {
                    key: _fare_json(value)
                    for key, value in p.fare_shares.items()
                },
            }
            for p in solution.rides
        ],
        "unmatched": [
            {
                "request_id": u.request_id,
                "reason_code": u.reason_code,
                "detail": u.detail,
            }
            for u in solution.unmatched
        ],
    }


def validation_markdown(dataset: Dataset) -> str:
    errors = [
        issue for issue in dataset.issues
        if issue.severity == "ERROR"
    ]
    warnings = [
        issue for issue in dataset.issues
        if issue.severity == "WARNING"
    ]

    out = [
        "# Input data validation report",
        "",
        f"- Valid trip requests: {len(dataset.requests)}; "
        f"invalid: {len(dataset.invalid_requests)}",
        f"- Valid booked rides: {len(dataset.rides)}; "
        f"invalid: {len(dataset.invalid_rides)}",
        f"- Valid vehicle options: {len(dataset.vehicles)}",
        f"- Errors: {len(errors)} (records excluded and reported); "
        f"warnings: {len(warnings)}",
        "",
    ]

    for title, items in (("Errors", errors), ("Warnings", warnings)):
        out += [f"## {title}", ""]

        if not items:
            out += ["None.", ""]
            continue

        out += [
            "| File | Row | Record | Column | Message |",
            "|---|---|---|---|---|",
        ]

        out += [
            f"| {issue.file} | "
            f"{issue.row if issue.row is not None else '-'} | "
            f"{issue.record_id or '-'} | "
            f"{issue.column} | {issue.message} |"
            for issue in items
        ]

        out.append("")

    return "\n".join(out)


def _signature(solution: Solution) -> set:
    return {
        (
            p.ride_id if p.kind == EXISTING else "NEW",
            p.vehicle_type,
            frozenset(p.request_ids),
        )
        for p in solution.rides
        if p.request_ids
    }


def comparison_markdown(
    greedy: Solution,
    ip: Solution,
    dataset: Dataset,
    greedy_errors: list[str],
    ip_errors: list[str],
) -> str:
    g = metrics(greedy, dataset)
    i = metrics(ip, dataset)

    rows = [
        ("Solver status", g["status"], i["status"]),
        (
            "Total transportation cost (new cabs)",
            money(g["new_cab_cost"]),
            money(i["new_cab_cost"]),
        ),
        (
            "Paid by requesting students",
            money(g["paid_by_requests"]),
            money(i["paid_by_requests"]),
        ),
        ("Number of requests served", g["requests_served"], i["requests_served"]),
        (
            "Number of passengers served",
            g["passengers_served"],
            i["passengers_served"],
        ),
        (
            "Number of unmatched requests",
            g["requests_unmatched"],
            i["requests_unmatched"],
        ),
        (
            "Number of existing cabs utilised",
            g["existing_cabs_used"],
            i["existing_cabs_used"],
        ),
        ("Number of new cabs created", g["new_cabs"], i["new_cabs"]),
        ("Total number of cabs used", g["total_cabs"], i["total_cabs"]),
        (
            "Average vehicle (seat) utilisation",
            f"{g['avg_utilisation']:.1%}",
            f"{i['avg_utilisation']:.1%}",
        ),
        (
            "Verification",
            "PASSED" if not greedy_errors else "FAILED",
            "PASSED" if not ip_errors else "FAILED",
        ),
        (
            "Runtime",
            f"{g['runtime_seconds']:.3f} s",
            f"{i['runtime_seconds']:.3f} s",
        ),
    ]

    out = [
        "# Greedy vs Integer Programming",
        "",
        "| Metric | Greedy | Integer Programming |",
        "|---|---|---|",
    ]

    out += [f"| {a} | {b} | {c} |" for a, b, c in rows]

    diff = g["new_cab_cost"] - i["new_cab_cost"]
    served_diff = i["requests_served"] - g["requests_served"]

    out += [
        "",
        "## Differences",
        "",
        f"- Cost difference (Greedy - IP): {money(diff)}",
    ]

    if g["new_cab_cost"] > 0:
        out.append(
            f"- Percentage cost saving of IP relative to Greedy: "
            f"{float(diff / g['new_cab_cost']):.2%}"
        )
    else:
        out.append(
            "- Percentage saving not meaningful (Greedy cost is zero)"
        )

    out.append(
        f"- Requests served (IP - Greedy): {served_diff:+d}"
    )

    same = _signature(greedy) == _signature(ip)

    out += ["", "## Interpretation", ""]

    if ip.status not in ("OPTIMAL", "FEASIBLE"):
        out.append(
            f"The IP solver did not return a usable solution "
            f"(status {ip.status}); no optimality comparison can be made."
        )
    elif same:
        out.append(
            "Both algorithms produced **the same assignment** "
            "(same cabs, vehicles and groups). On this input the greedy "
            "choices happen to be optimal; this is not true in general "
            "(see test `test_greedy_and_ip_differ`)."
        )
    elif diff == 0 and served_diff == 0:
        out.append(
            "Both algorithms reach the same cost and number served with "
            "different groupings - several optimal solutions exist."
        )
    elif served_diff > 0 or diff > 0:
        out.append(
            "The IP solution is better on this input. Greedy commits to the "
            "cheapest option for each request in turn and never reconsiders; "
            "the IP model evaluates all assignments simultaneously and can "
            "accept a locally worse choice (e.g. a bigger vehicle now) to "
            "enable cheaper groups later."
        )
    else:
        out.append(
            "Greedy appears better than the IP result. This can only happen "
            "if the IP was not solved to optimality "
            f"(status {ip.status}) or under different settings."
        )

    out += [
        "",
        f"IP status **{ip.status}** means: "
        + {
            "OPTIMAL": "the solver proved no better solution exists for the model.",
            "FEASIBLE": (
                "a valid solution was found but optimality was not proven "
                "(time limit)."
            ),
            "INFEASIBLE": "no solution satisfies all constraints.",
            "FAILED": "the solver did not finish successfully.",
        }.get(ip.status, ip.status),
        "",
        f"> {GREEDY_DISCLAIMER}",
        "",
    ]

    return "\n".join(out)
