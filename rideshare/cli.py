"""Command-line interface:  python -m rideshare <command> [options]

Commands: validate | greedy | ip | compare | sensitivity | all | serve | bookings ...
Run  python -m rideshare <command> --help  for details.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import service
from .bookings import BookingError, BookingManager
from .config import Config
from .data_loader import DataValidationError


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", type=Path, help="folder with the three CSV files")
    common.add_argument("--output-dir", type=Path, help="folder for reports")
    common.add_argument("--time-tolerance", type=int, default=0, metavar="MIN",
                        help="widen every departure window by MIN minutes each side (default 0)")
    common.add_argument("--gender-policy", choices=["strict", "lenient"], default="strict",
                        help="handling of missing gender/preference data (default strict)")
    common.add_argument("--no-gender-matching", action="store_true",
                        help="ignore gender preferences entirely")
    common.add_argument("--objective", choices=["lexicographic", "penalty"], default="lexicographic")
    common.add_argument("--penalty", type=float, help="penalty per unserved request (penalty mode)")
    common.add_argument("--time-limit", type=int, default=60, help="solver time limit in seconds")
    common.add_argument("--deadline-hours", type=float, default=24.0,
                        help="cancellation deadline before departure (default 24)")
    common.add_argument("--bookings-file", type=Path, help="booking store (JSON)")

    p = argparse.ArgumentParser(prog="python -m rideshare", description="IITP RideShare backend")
    sub = p.add_subparsers(dest="command", required=True)
    for name, text in (("validate", "validate the input CSV files"),
                       ("greedy", "run the Greedy algorithm"),
                       ("ip", "run the Integer Programming model"),
                       ("compare", "compare Greedy and IP"),
                       ("sensitivity", "run the sensitivity analysis"),
                       ("all", "validate, greedy, ip, compare and sensitivity")):
        sub.add_parser(name, parents=[common], help=text)
    srv = sub.add_parser("serve", parents=[common], help="start the web UI and JSON API")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)

    b = sub.add_parser("bookings", help="manage bookings").add_subparsers(dest="action", required=True)
    clock = argparse.ArgumentParser(add_help=False)
    clock.add_argument("--now", help="pretend current time, 'YYYY-MM-DD HH:MM' (for demos/tests)")
    b.add_parser("register", parents=[common, clock],
                 help="make the new cabs of a saved solution bookable").add_argument(
        "--solution", type=Path, required=True)
    b.add_parser("book-solution", parents=[common, clock],
                 help="create PENDING bookings for every assignment of a solution").add_argument(
        "--solution", type=Path, required=True)
    c = b.add_parser("create", parents=[common, clock], help="create a booking")
    c.add_argument("--request", required=True)
    c.add_argument("--ride", required=True)
    b.add_parser("confirm", parents=[common, clock]).add_argument("--booking", required=True)
    b.add_parser("cancel", parents=[common, clock]).add_argument("--booking", required=True)
    b.add_parser("list", parents=[common, clock]).add_argument(
        "--status", choices=["PENDING", "CONFIRMED", "CANCELLED"])
    b.add_parser("capacity", parents=[common, clock]).add_argument("--ride", required=True)
    return p


def config_from(args) -> Config:
    cfg = Config(time_tolerance_minutes=args.time_tolerance,
                 gender_matching=not args.no_gender_matching,
                 missing_gender_policy=args.gender_policy, objective_mode=args.objective,
                 unserved_penalty=args.penalty, solver_time_limit_seconds=args.time_limit,
                 cancellation_deadline_hours=args.deadline_hours)
    changes = {}
    if args.data_dir:
        changes["data_dir"] = args.data_dir
    if args.output_dir:
        changes["output_dir"] = args.output_dir
        changes["bookings_file"] = args.output_dir / "bookings.json"
    if args.bookings_file:
        changes["bookings_file"] = args.bookings_file
    return cfg.with_(**changes) if changes else cfg


def _print_solution(name, cfg):
    ds, sol, errors, paths = service.solve(cfg, name)
    print(service.summary_line(sol, ds, errors))
    for e in errors:
        print(f"  VERIFICATION ERROR: {e}")
    print(f"  report: {paths['report']}")
    return not errors


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = config_from(args)
        if args.command == "serve":
            from .server import serve
            serve(cfg, args.host, args.port)
            return 0
        if args.command == "bookings":
            return _bookings(args, cfg)
        ok = True
        if args.command in ("validate", "all"):
            ds, path = service.validate(cfg)
            errs = sum(1 for i in ds.issues if i.severity == "ERROR")
            warns = sum(1 for i in ds.issues if i.severity == "WARNING")
            print(f"Validation: {len(ds.requests)} requests, {len(ds.rides)} booked rides, "
                  f"{len(ds.vehicles)} vehicle options; {errs} error(s), {warns} warning(s)")
            for i in ds.issues:
                print(f"  {i.severity:7} {i.file} row {i.row if i.row is not None else '-'} "
                      f"{i.record_id or ''} [{i.column}] {i.message}")
            print(f"  report: {path}")
        if args.command in ("greedy", "all"):
            ok &= _print_solution("greedy", cfg)
        if args.command in ("ip", "all"):
            ok &= _print_solution("ip", cfg)
        if args.command in ("compare", "all"):
            ds, (g, ge), (i, ie), path = service.compare(cfg)
            print(service.summary_line(g, ds, ge))
            print(service.summary_line(i, ds, ie))
            print(f"  Cost difference (Greedy - IP): Rs {g.new_cab_cost - i.new_cab_cost:,.2f}")
            print(f"  comparison report: {path}")
            ok &= not ge and not ie
        if args.command in ("sensitivity", "all"):
            result, path = service.sensitivity(cfg)
            print(f"Sensitivity: {len(result['rows'])} what-if scenarios re-solved; "
                  f"LP relaxation status {result['lp']['status']}")
            for r in result["rows"]:
                print(f"  {r['scenario']:40} {r['status']:9} cost Rs {r['cost_after']:>10,.2f} "
                      f"served {r['served_after']}/{r['requests_total']}")
            print(f"  report: {path}")
        return 0 if ok else 2
    except DataValidationError as exc:
        print("Input data cannot be used:", file=sys.stderr)
        for m in exc.messages:
            print(f"  - {m}", file=sys.stderr)
        return 1


def _bookings(args, cfg) -> int:
    ds = service.load(cfg)
    clock = (lambda: datetime.strptime(args.now, "%Y-%m-%d %H:%M")) if args.now else None
    mgr = BookingManager(ds, cfg, clock=clock)
    try:
        if args.action == "register":
            added = mgr.register_solution(args.solution)
            print(f"Registered {len(added)} new cab(s) from {args.solution}")
        elif args.action == "book-solution":
            created, errors = mgr.book_solution(args.solution)
            print(f"Created {len(created)} PENDING booking(s); {len(errors)} refused")
            for e in errors:
                print(f"  refused: {e}")
        elif args.action == "create":
            print(json.dumps(mgr.create_booking(args.request, args.ride), indent=2))
        elif args.action == "confirm":
            print(json.dumps(mgr.confirm_booking(args.booking), indent=2))
        elif args.action == "cancel":
            print(json.dumps(mgr.cancel_booking(args.booking), indent=2))
        elif args.action == "list":
            for b in mgr.list_bookings(args.status):
                print(f"{b['booking_id']}  {b['status']:9}  {b['request_id']} -> {b['ride_id']:12} "
                      f"share Rs {b['fare_share_at_booking']} ({b['fare_status']})  created {b['created_at']}")
        elif args.action == "capacity":
            print(json.dumps(mgr.capacity(args.ride), indent=2))
        return 0
    except BookingError as exc:
        print(f"Booking error: {exc}", file=sys.stderr)
        return 3
