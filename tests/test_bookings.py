"""Booking and cancellation management (spec section 15, items 13-17)."""
import json

import pytest

from helpers import Clock, make_dataset, req, ride
from rideshare.bookings import BookingError, BookingManager
from rideshare.ip_model import run_ip
from rideshare.reporting import export_solution
from rideshare.verification import verify_solution

DEPARTURE = "2026-11-10 10:30"


@pytest.fixture
def setup(tmp_path):
    ds, cfg = make_dataset(tmp_path, [req("A", pax=2, lug=1), req("B"), req("C", pax=2),
                                      req("F", gender="Female", pref="Same")],
                           [ride("R1", dep=DEPARTURE, cap=4, lugcap=2, booked=1, booked_lug=0)])
    clock = Clock("2026-11-01 09:00")
    return ds, cfg, clock, BookingManager(ds, cfg, clock=clock)


def test_13_duplicate_booking_rejected(setup):
    _, _, _, mgr = setup
    b = mgr.create_booking("A", "R1")
    assert b["status"] == "PENDING" and b["fare_share_at_booking"] == "600.00"
    with pytest.raises(BookingError, match="Duplicate booking"):
        mgr.create_booking("A", "R1")


def test_14_confirm_pending_booking(setup):
    _, _, clock, mgr = setup
    b = mgr.create_booking("A", "R1")
    clock.set("2026-11-02 12:00")
    assert mgr.confirm_booking(b["booking_id"])["confirmed_at"] == "2026-11-02 12:00:00"
    with pytest.raises(BookingError, match="Only PENDING"):
        mgr.confirm_booking(b["booking_id"])


def test_15_cancel_within_deadline(setup):
    _, _, clock, mgr = setup
    b = mgr.create_booking("A", "R1")
    mgr.confirm_booking(b["booking_id"])
    clock.set("2026-11-09 10:30")               # exactly 24 h before departure: still allowed
    assert mgr.cancel_booking(b["booking_id"])["status"] == "CANCELLED"
    with pytest.raises(BookingError, match="already cancelled"):
        mgr.cancel_booking(b["booking_id"])


def test_16_cancel_after_deadline(setup):
    _, _, clock, mgr = setup
    b = mgr.create_booking("A", "R1")
    clock.set("2026-11-09 10:31")               # one minute inside the 24 h window
    with pytest.raises(BookingError, match="Cancellation deadline passed"):
        mgr.cancel_booking(b["booking_id"])
    assert mgr.list_bookings()[0]["status"] == "PENDING"


def test_16b_deadline_is_configurable(setup):
    ds, cfg, clock, _ = setup
    mgr = BookingManager(ds, cfg.with_(cancellation_deadline_hours=2), clock=clock)
    b = mgr.create_booking("A", "R1")
    clock.set("2026-11-10 08:00")
    assert mgr.cancel_booking(b["booking_id"])["status"] == "CANCELLED"


def test_17_capacity_restored_after_cancellation(setup):
    _, _, _, mgr = setup
    a = mgr.create_booking("A", "R1")
    cap = mgr.capacity("R1")
    assert (cap["seats_remaining"], cap["luggage_remaining"]) == (1, 1)
    with pytest.raises(BookingError, match="Insufficient seats"):
        mgr.create_booking("C", "R1")
    mgr.cancel_booking(a["booking_id"])
    cap = mgr.capacity("R1")
    assert (cap["seats_remaining"], cap["luggage_remaining"]) == (3, 2)
    assert mgr.create_booking("C", "R1")["status"] == "PENDING"


def test_bookings_persist_across_restart(setup):
    ds, cfg, clock, mgr = setup
    b = mgr.create_booking("A", "R1")
    mgr.confirm_booking(b["booking_id"])
    reloaded = BookingManager(ds, cfg, clock=clock)
    assert reloaded.list_bookings(status="CONFIRMED")[0]["booking_id"] == b["booking_id"]
    assert reloaded.capacity("R1")["seats_remaining"] == 1
    assert reloaded.create_booking("B", "R1")["booking_id"] == "BKG-0002"


def test_invalid_operations_give_clear_errors(setup):
    _, _, clock, mgr = setup
    with pytest.raises(BookingError, match="Unknown request"):
        mgr.create_booking("ZZZ", "R1")
    with pytest.raises(BookingError, match="Unknown ride"):
        mgr.create_booking("A", "NOPE")
    with pytest.raises(BookingError, match="Unknown booking"):
        mgr.confirm_booking("BKG-9999")
    with pytest.raises(BookingError, match="Gender preference incompatibility"):
        mgr.create_booking("F", "R1")           # booker is male, F wants same gender
    clock.set("2026-11-10 11:00")
    with pytest.raises(BookingError, match="departed"):
        mgr.create_booking("B", "R1")


def test_book_new_cabs_from_solution(tmp_path):
    ds, cfg = make_dataset(tmp_path, [req("A"), req("B")])
    sol = run_ip(ds, cfg)
    errors = verify_solution(sol, ds, cfg)
    paths = export_solution(sol, ds, cfg, errors, cfg.output_dir, "ip")
    mgr = BookingManager(ds, cfg, clock=Clock("2026-11-01 09:00"))
    created, refused = mgr.book_solution(paths["json"])
    assert refused == [] and len(created) == 2
    assert {b["fare_status"] for b in created} == {"ESTIMATED"}
    ride_id = sol.new_rides[0].ride_id
    assert sum(float(v) for v in mgr.fare_breakdown(ride_id).values()) == float(sol.new_rides[0].total_fare)
    data = json.loads(paths["json"].read_text())
    data["verified"] = False
    paths["json"].write_text(json.dumps(data))
    with pytest.raises(BookingError, match="did not pass verification"):
        mgr.register_solution(paths["json"])
