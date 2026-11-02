"""Input validation, fare splitting, verification and the provided CSV dataset."""
from decimal import Decimal

import pytest

from helpers import RIDE_HEADER, TRIP_HEADER, make_dataset, req, ride, write
from rideshare.config import Config
from rideshare.data_loader import DataValidationError, load_dataset
from rideshare.fares import savings_summary, split_fare
from rideshare.greedy import run_greedy
from rideshare.ip_model import run_ip
from rideshare.request_manager import RequestBook, submit_request
from rideshare.verification import verify_solution


# ------------------------------------------------------------- validation
def test_missing_file_is_fatal(tmp_path):
    with pytest.raises(DataValidationError, match="Missing input file"):
        load_dataset(tmp_path)


def test_missing_required_column_is_fatal(tmp_path):
    header = [c for c in TRIP_HEADER if c != "latest_departure"]
    with pytest.raises(DataValidationError, match="missing required column.*latest_departure"):
        make_dataset(tmp_path, [], trip_header=header)


def test_invalid_records_are_reported_not_dropped(tmp_path):
    rows = [req("A"), req("A"),                                          # duplicate id
            req("B", start="10/11/2026 10:00"),                          # bad date-time
            req("C", pax=-1), req("D", lug=-2),                          # negative counts
            req("E", start="2026-11-10 12:00", end="2026-11-10 11:00"),  # earliest > latest
            req("F", gender="Robot"), req("G", pref="Nobody"),           # invalid gender values
            req("H", solo="-5")]                                         # invalid fare
    rides = [ride("R1"), ride("R1"), ride("R2", cap=2, booked=3), ride("R3", lugcap=1, booked_lug=2),
             ride("R4", fare="abc"), ride("R5", cap=0)]
    ds, cfg = make_dataset(tmp_path, rows, rides)
    assert [r.request_id for r in ds.requests] == ["A"]
    assert set(k.split("@")[0] for k in ds.invalid_requests) == set("ABCDEFGH")
    assert [r.ride_id for r in ds.rides] == ["R1"]
    assert set(k.split("@")[0] for k in ds.invalid_rides) == {"R1", "R2", "R3", "R4", "R5"}
    messages = " ".join(i.message for i in ds.issues)
    for fragment in ("duplicate request_id", "invalid date-time", "below the minimum",
                     "later than latest", "invalid value 'Robot'", "fare must be a positive",
                     "exceeds capacity", "exceeds luggage capacity", "duplicate ride_id"):
        assert fragment in messages
    for algo in (run_greedy, run_ip):
        sol = algo(ds, cfg)
        assert verify_solution(sol, ds, cfg) == []
        assert sum(1 for u in sol.unmatched if u.reason_code == "INVALID_RECORD") == len(ds.invalid_requests)


def test_absent_booker_gender_columns_warned(tmp_path):
    header = RIDE_HEADER[:-2]
    ds, _ = make_dataset(tmp_path, [req("A")], [ride("R1")[:-2]], ride_header=header)
    assert not ds.ride_gender_columns_present
    assert ds.rides[0].booker_gender is None
    assert any("booker_gender" in i.column for i in ds.issues if i.severity == "WARNING")


def test_submit_request_validates_and_accepts_any_destination(tmp_path):
    ds, _ = make_dataset(tmp_path, [req("A")])
    book = RequestBook.from_dataset(ds)
    r = submit_request(book, request_id="N1", student_name="New", pickup_location="IIT Patna Campus",
                       destination="Purnia", earliest=ds.requests[0].earliest_departure,
                       latest=ds.requests[0].latest_departure, passengers=1, luggage=0)
    assert r.destination == "Purnia" and "N1" in book
    with pytest.raises(ValueError, match="already exists"):
        submit_request(book, request_id="A", student_name="x", pickup_location="a", destination="b",
                       earliest=r.earliest_departure, latest=r.latest_departure, passengers=1, luggage=0)
    with pytest.raises(ValueError, match="earliest departure is later"):
        submit_request(book, request_id="N2", student_name="x", pickup_location="a", destination="b",
                       earliest=r.latest_departure, latest=r.earliest_departure, passengers=1, luggage=0)


# ------------------------------------------------------------- fares
def test_split_fare_sums_exactly():
    shares = split_fare(Decimal("1000"), {"a": 1, "b": 1, "c": 1})
    assert sum(shares.values()) == Decimal("1000.00")
    assert sorted(shares.values()) == [Decimal("333.33"), Decimal("333.33"), Decimal("333.34")]
    weighted = split_fare(Decimal("2250"), {"booker": 1, "x": 3})
    assert weighted == {"booker": Decimal("562.50"), "x": Decimal("1687.50")}
    for total in ("1", "999.99", "2700", "0.05"):
        s = split_fare(Decimal(total), {"a": 3, "b": 2, "c": 2, "d": 1})
        assert sum(s.values()) == Decimal(total)


def test_savings_reporting(tmp_path):
    ds, _ = make_dataset(tmp_path, [req("A", solo="500"), req("B")])
    a, b = ds.requests
    assert savings_summary(a, Decimal("300"))["savings"] == Decimal("200.00")
    assert "Pays" in savings_summary(a, Decimal("650"))["verdict"]
    assert "cannot be calculated" in savings_summary(b, Decimal("300"))["verdict"]


# ------------------------------------------------------------- verification
def test_verification_detects_tampered_solution(tmp_path):
    ds, cfg = make_dataset(tmp_path, [req("A", pax=2), req("B", pax=2)], [ride("R1", cap=4, booked=1)])
    sol = run_ip(ds, cfg)
    assert verify_solution(sol, ds, cfg) == []
    plan = sol.rides[0]
    plan.request_ids = ["A", "B"]                       # 1 + 2 + 2 > 4 seats
    plan.fare_shares["B"] = Decimal("1.00")             # shares no longer add up
    errors = verify_solution(sol, ds, cfg)
    assert any("exceed capacity" in e for e in errors)
    assert any("fare shares add up" in e for e in errors)
    assert any("assigned to 2 cabs" in e or "both assigned and reported" in e for e in errors)


# ------------------------------------------------------------- provided dataset
@pytest.fixture(scope="module")
def provided():
    cfg = Config()
    return load_dataset(cfg.data_dir), cfg


def test_provided_dataset_loads(provided):
    ds, _ = provided
    assert (len(ds.requests), len(ds.rides), len(ds.vehicles)) == (100, 25, 30)
    assert not [i for i in ds.issues if i.severity == "ERROR"]
    assert not ds.ride_gender_columns_present


@pytest.mark.parametrize("tolerance,policy", [(0, "strict"), (60, "lenient"), (120, "lenient")])
def test_provided_dataset_both_algorithms_verified(provided, tolerance, policy):
    ds, base = provided
    cfg = base.with_(time_tolerance_minutes=tolerance, missing_gender_policy=policy)
    g, i = run_greedy(ds, cfg), run_ip(ds, cfg)
    assert verify_solution(g, ds, cfg) == [] and verify_solution(i, ds, cfg) == []
    assert i.status == "OPTIMAL"
    assert len(i.served_ids) >= len(g.served_ids)
    assert i.new_cab_cost <= g.new_cab_cost
