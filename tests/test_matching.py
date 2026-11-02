"""Matching, grouping, vehicle and gender scenarios (spec section 15, items 1-12, 18, 19).

Every scenario is solved by BOTH algorithms and every result is independently verified.
"""
import pytest

from helpers import PJ, SMALL_VEHICLES, make_dataset, req, ride
from rideshare.greedy import run_greedy
from rideshare.ip_model import run_ip
from rideshare.verification import verify_solution

ALGOS = [run_greedy, run_ip]


def solve(ds, cfg, algo):
    sol = algo(ds, cfg)
    assert verify_solution(sol, ds, cfg) == [], "solution failed independent verification"
    return sol


def joined(sol):
    return {p.ride_id: sorted(p.request_ids) for p in sol.existing_used}


def reason(sol, rid):
    if rid in sol.existing_cab_reasons:
        return sol.existing_cab_reasons[rid][0]
    return None


def unmatched_code(sol, rid):
    return next((u.reason_code for u in sol.unmatched if u.request_id == rid), None)


@pytest.mark.parametrize("algo", ALGOS)
def test_01_student_joins_existing_cab(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A", pax=2, lug=1)], [ride("R1", booked=1)])
    sol = solve(ds, cfg, algo)
    assert joined(sol) == {"R1": ["A"]}
    plan = sol.existing_used[0]
    assert plan.fare_shares == {"BOOKER:Booker R1": pytest.approx(300), "A": pytest.approx(600)}
    assert sol.new_rides == []


@pytest.mark.parametrize("algo", ALGOS)
def test_02_multiple_students_join_same_cab(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A"), req("B"), req("C")],
                           [ride("R1", cap=4, booked=1, lugcap=2)])
    sol = solve(ds, cfg, algo)
    assert joined(sol) == {"R1": ["A", "B", "C"]}
    p = sol.existing_used[0]
    assert sum(p.fare_shares.values()) == p.total_fare


@pytest.mark.parametrize("algo", ALGOS)
def test_03_students_grouped_into_new_shared_cab(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A", pax=2, solo="400"), req("B", pax=1, solo="250",
                                                                      start="2026-11-10 10:30")])
    sol = solve(ds, cfg, algo)
    assert len(sol.new_rides) == 1
    cab = sol.new_rides[0]
    assert cab.request_ids == ["A", "B"] and cab.vehicle_type == "Auto"
    assert cab.fare_shares == {"A": pytest.approx(200), "B": pytest.approx(100)}
    assert cab.departure_time.strftime("%H:%M") == "10:30"   # common window starts at 10:30


@pytest.mark.parametrize("algo", ALGOS)
def test_04_insufficient_passenger_capacity(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A", pax=3)], [ride("R1", cap=4, booked=2)])
    sol = solve(ds, cfg, algo)
    assert joined(sol) == {}
    assert reason(sol, "A") == "INSUFFICIENT_SEATS"


@pytest.mark.parametrize("algo", ALGOS)
def test_05_insufficient_luggage_capacity(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A", lug=2)], [ride("R1", lugcap=2, booked_lug=1)])
    sol = solve(ds, cfg, algo)
    assert joined(sol) == {}
    assert reason(sol, "A") == "INSUFFICIENT_LUGGAGE"


@pytest.mark.parametrize("algo", ALGOS)
def test_06_incompatible_departure_windows(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A", start="2026-11-10 08:00", end="2026-11-10 09:00"),
                                      req("B", start="2026-11-10 09:30", end="2026-11-10 10:00")],
                           [ride("R1", dep="2026-11-10 12:00")])
    sol = solve(ds, cfg, algo)
    assert reason(sol, "A") == "TIME_INCOMPATIBLE"
    assert len(sol.new_rides) == 2   # no common time -> cannot share


@pytest.mark.parametrize("algo", ALGOS)
def test_06b_time_tolerance_enables_sharing(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A", start="2026-11-10 08:00", end="2026-11-10 09:00"),
                                      req("B", start="2026-11-10 09:30", end="2026-11-10 10:00")])
    sol = solve(ds, cfg.with_(time_tolerance_minutes=15), algo)
    assert len(sol.new_rides) == 1


@pytest.mark.parametrize("algo", ALGOS)
def test_07_different_pickup_locations_cannot_share(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A"), req("B", pickup="Bihta Station")],
                           [ride("R1")])
    sol = solve(ds, cfg, algo)
    assert joined(sol) == {"R1": ["A"]}
    assert reason(sol, "B") == "NO_RIDE_ON_ROUTE"
    for cab in sol.new_rides:
        assert cab.request_ids == ["B"]


@pytest.mark.parametrize("algo", ALGOS)
def test_08_different_destinations_cannot_share(tmp_path, algo):
    vehicles = SMALL_VEHICLES + [["Auto", "Gaya", 3, 1, 450]]
    ds, cfg = make_dataset(tmp_path, [req("A"), req("B", dest="Gaya")], vehicles=vehicles)
    sol = solve(ds, cfg, algo)
    assert sorted(len(c.request_ids) for c in sol.new_rides) == [1, 1]


@pytest.mark.parametrize("algo", ALGOS)
def test_09_destination_absent_from_vehicle_options(tmp_path, algo):
    # Not rejected: it may still join a booked cab, and otherwise needs manual confirmation.
    ds, cfg = make_dataset(tmp_path, [req("A", dest="Bhagalpur"), req("B", dest="Bhagalpur",
                                                                      start="2026-11-12 10:00", end="2026-11-12 11:00")],
                           [ride("R1", dest="Bhagalpur", dep="2026-11-10 10:30")])
    sol = solve(ds, cfg, algo)
    assert joined(sol) == {"R1": ["A"]}
    assert unmatched_code(sol, "B") == "NO_VEHICLE_INFO"
    assert sol.new_rides == []   # no fare was invented


@pytest.mark.parametrize("algo", ALGOS)
def test_10_no_suitable_vehicle(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A", pax=8)])
    sol = solve(ds, cfg, algo)
    assert unmatched_code(sol, "A") == "NO_VEHICLE_LARGE_ENOUGH"


@pytest.mark.parametrize("algo", ALGOS)
def test_11_compatible_gender_preferences(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A", gender="Female", pref="Same"),
                                      req("B", gender="Female", pref="Female")],
                           [ride("R1", gender="Female", pref="Any")])
    sol = solve(ds, cfg, algo)
    assert joined(sol) == {"R1": ["A", "B"]}


@pytest.mark.parametrize("algo", ALGOS)
def test_12_incompatible_gender_preferences(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A", gender="Female", pref="Same"),
                                      req("B", gender="Male", pref="Any")])
    sol = solve(ds, cfg, algo)
    assert len(sol.new_rides) == 2   # cannot share although route and time match
    ds2, cfg2 = make_dataset(tmp_path, [req("A", gender="Female", pref="Female")],
                             [ride("R1", gender="Male", pref="Any")])
    sol2 = solve(ds2, cfg2, algo)
    assert reason(sol2, "A") == "GENDER_INCOMPATIBLE"


@pytest.mark.parametrize("algo", ALGOS)
def test_12b_missing_gender_strict_vs_lenient(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A")], [ride("R1", gender="", pref="")])
    strict = solve(ds, cfg, algo)
    assert reason(strict, "A") == "GENDER_INFO_MISSING"
    lenient_cfg = cfg.with_(missing_gender_policy="lenient")
    lenient = solve(ds, lenient_cfg, algo)
    assert joined(lenient) == {"R1": ["A"]}
    assert lenient.existing_used[0].gender_unverified
    off = solve(ds, cfg.with_(gender_matching=False), algo)
    assert joined(off) == {"R1": ["A"]}


def test_18_greedy_and_ip_differ(tmp_path):
    """Greedy groups A+B into a Sedan (marginal +200 beats a new Auto at 300), then
    cannot fit C, so C and D take another Auto: 500 + 300 = 800. The IP pairs each
    2-passenger request with a 1-passenger one in two Autos: 600."""
    reqs = [req("A", pax=2, start="2026-11-10 10:00"), req("B", pax=2, start="2026-11-10 10:05"),
            req("C", pax=1, start="2026-11-10 10:10"), req("D", pax=1, start="2026-11-10 10:15")]
    ds, cfg = make_dataset(tmp_path, reqs, vehicles=SMALL_VEHICLES)
    g, i = solve(ds, cfg, run_greedy), solve(ds, cfg, run_ip)
    assert g.new_cab_cost == 800 and i.new_cab_cost == 600
    assert i.status == "OPTIMAL"
    # Optimal: two Autos, each pairing a 2-passenger request with a 1-passenger one
    # ({A,C}+{B,D} and {A,D}+{B,C} are equally optimal).
    assert [c.vehicle_type for c in i.new_rides] == ["Auto", "Auto"]
    for cab in i.new_rides:
        assert sorted(ds.request(r).passengers for r in cab.request_ids) == [1, 2]


def test_18b_ip_chooses_combination_not_input_order(tmp_path):
    """Booked cab with 3 free seats, no new-cab fares known for the destination.
    Greedy (largest party first) takes X (2 seats) + one other -> 2 served.
    IP serves Y, Z, W -> 3 served."""
    reqs = [req("X", dest="Bhagalpur", pax=2), req("Y", dest="Bhagalpur"),
            req("Z", dest="Bhagalpur"), req("W", dest="Bhagalpur")]
    ds, cfg = make_dataset(tmp_path, reqs, [ride("R1", dest="Bhagalpur", cap=4, booked=1)])
    g, i = solve(ds, cfg, run_greedy), solve(ds, cfg, run_ip)
    assert len(g.served_ids) == 2
    assert sorted(i.served_ids) == ["W", "Y", "Z"]
    assert unmatched_code(i, "X") == "CAPACITY_TAKEN"


@pytest.mark.parametrize("algo", ALGOS)
def test_19_no_feasible_assignment(tmp_path, algo):
    ds, cfg = make_dataset(tmp_path, [req("A", dest="Siwan"), req("B", pax=20)])
    sol = solve(ds, cfg, algo)
    assert sol.rides == []
    assert {u.request_id: u.reason_code for u in sol.unmatched} == {
        "A": "NO_VEHICLE_INFO", "B": "NO_VEHICLE_LARGE_ENOUGH"}
    if algo is run_ip:
        assert sol.status == "OPTIMAL"   # "serve nobody" is the proven optimum


def test_low_penalty_distorts_objective(tmp_path):
    ds, cfg = make_dataset(tmp_path, [req("A")], vehicles=SMALL_VEHICLES)
    low = run_ip(ds, cfg.with_(objective_mode="penalty", unserved_penalty=100))
    assert unmatched_code(low, "A") == "NOT_SELECTED"
    safe = run_ip(ds, cfg.with_(objective_mode="penalty"))
    lexi = run_ip(ds, cfg)
    assert len(safe.served_ids) == len(lexi.served_ids) == 1
    assert safe.new_cab_cost == lexi.new_cab_cost


def test_vehicle_selected_by_capacity_and_cost(tmp_path):
    # 1 passenger with 3 bags: Auto (1 bag) and Sedan (2 bags) are too small -> SUV.
    ds, cfg = make_dataset(tmp_path, [req("A", lug=3)])
    for algo in ALGOS:
        assert solve(ds, cfg, algo).new_rides[0].vehicle_type == "SUV"
