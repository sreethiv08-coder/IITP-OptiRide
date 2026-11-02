"""Sensitivity analysis (spec item 20), the CLI, and the web JSON API."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from helpers import SMALL_VEHICLES, make_dataset, req, ride
from rideshare.cli import main
from rideshare.ip_model import solve_lp_relaxation
from rideshare.sensitivity import run_sensitivity, sensitivity_markdown


def _reqs():
    return [req("A", pax=2, start="2026-11-10 10:00"), req("B", pax=2, start="2026-11-10 10:05"),
            req("C", pax=1, start="2026-11-10 10:10"), req("D", pax=1, start="2026-11-10 10:15"),
            req("E", pax=1, lug=1, start="2026-11-11 08:00", end="2026-11-11 09:00")]


def test_20_sensitivity_fares_and_capacities(tmp_path):
    ds, cfg = make_dataset(tmp_path, _reqs(), vehicles=SMALL_VEHICLES)
    result = run_sensitivity(ds, cfg)
    rows = {r["scenario"]: r for r in result["rows"]}
    base = result["base"]["new_cab_cost"]
    assert all(r["verified"] for r in rows.values())
    assert rows["All vehicle fares -20%"]["cost_after"] == base * 8 / 10
    assert rows["All vehicle fares +20%"]["cost_after"] == base * 12 / 10
    assert rows["Every vehicle +1 seat"]["cost_after"] <= base
    assert rows["Every vehicle -1 seat"]["cost_after"] >= base
    # Auto loses its only bag slot, so E (1 bag) needs a Sedan: still served, but dearer.
    assert rows["Every vehicle -1 bag"]["cost_after"] > base
    # P = 200 is below a solo fare (300), so some requests are left unserved: the penalty distorts.
    assert rows["Penalty objective, P = 200"]["served_after"] < result["base"]["requests_served"]
    assert rows["Penalty objective, P = safe value"]["served_after"] == result["base"]["requests_served"]
    md = sensitivity_markdown(result)
    assert "LP relaxation" in md and "do **not**" in md


def test_lp_relaxation_gives_duals(tmp_path):
    ds, cfg = make_dataset(tmp_path, _reqs(), [ride("R1", cap=3, booked=2)], vehicles=SMALL_VEHICLES)
    lp = solve_lp_relaxation(ds, cfg)
    assert lp["status"] == "Optimal"
    assert {f"assign_{r}" for r in "ABCDE"} <= set(lp["duals"])
    assert all(v is not None for k, v in lp["duals"].items() if k.startswith("assign_"))


def test_cli_end_to_end(tmp_path, capsys):
    ds, cfg = make_dataset(tmp_path, _reqs(), vehicles=SMALL_VEHICLES)
    args = ["--data-dir", str(tmp_path), "--output-dir", str(tmp_path / "out")]
    assert main(["compare", *args]) == 0
    out = capsys.readouterr().out
    assert "Cost difference (Greedy - IP): Rs 200.00" in out
    assert (tmp_path / "out" / "comparison_report.md").exists()
    sol = tmp_path / "out" / "ip_solution.json"
    assert main(["bookings", "book-solution", "--solution", str(sol), "--now", "2026-11-01 09:00", *args]) == 0
    assert main(["bookings", "cancel", "--booking", "BKG-0001", "--now", "2026-11-10 09:00", *args]) == 3
    assert "Cancellation deadline passed" in capsys.readouterr().err


@pytest.fixture
def server(tmp_path):
    from rideshare.server import make_server
    ds, cfg = make_dataset(tmp_path, _reqs(), [ride("R1", dep="2026-11-10 10:30", cap=4, booked=1)],
                           vehicles=SMALL_VEHICLES)
    httpd = make_server(cfg, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request) as res:
            text = res.read().decode()
            return res.status, (json.loads(text) if "json" in res.headers["Content-Type"] else text)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_api_full_flow(server):
    assert call(server, "GET", "/")[1].startswith("<!doctype html>")
    status, data = call(server, "GET", "/api/data")
    assert status == 200 and len(data["requests"]) == 5
    status, sol = call(server, "POST", "/api/solve", {"algorithm": "ip", "settings": {}})
    assert status == 200 and sol["verified"] and sol["status"] == "OPTIMAL"
    status, cmp_ = call(server, "POST", "/api/compare", {"settings": {}})
    assert cmp_["greedy"]["verified"] and cmp_["ip"]["verified"]
    status, new = call(server, "POST", "/api/requests", {
        "request_id": "N1", "student_name": "New", "pickup_location": "IIT Patna Campus",
        "destination": "Purnia", "earliest_departure": "2026-11-12T08:00",
        "latest_departure": "2026-11-12T09:00", "passengers": 1, "luggage": 0})
    assert status == 200 and "manual confirmation" in new["note"]
    settings = {"now": "2026-11-01T09:00"}
    status, b = call(server, "POST", "/api/bookings", {"request_id": "C", "ride_id": "R1", "settings": settings})
    assert status == 200 and b["status"] == "PENDING"
    status, err = call(server, "POST", "/api/bookings", {"request_id": "C", "ride_id": "R1", "settings": settings})
    assert status == 409 and "Duplicate booking" in err["error"]
    status, _ = call(server, "POST", f"/api/bookings/{b['booking_id']}/confirm", {"settings": settings})
    assert status == 200
    status, st = call(server, "POST", "/api/bookings/state", {"settings": settings})
    assert next(r for r in st["rides"] if r["ride_id"] == "R1")["seats_remaining"] == 2
    status, _ = call(server, "POST", f"/api/bookings/{b['booking_id']}/cancel", {"settings": settings})
    assert status == 200
    status, err = call(server, "POST", "/api/solve", {"algorithm": "magic"})
    assert status == 400
    assert call(server, "GET", "/../../etc/passwd")[0] == 404
