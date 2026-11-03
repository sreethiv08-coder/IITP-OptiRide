from rideshare.config import Config
from rideshare.server import Api, ApiError


def _api(tmp_path):
    return Api(Config(output_dir=tmp_path))


def _user(name, roll, gender="FEMALE"):
    return {"name": name, "roll_number": roll, "gender": gender}


def _group_body(**overrides):
    body = {
        "pickup_location": "IIT Patna",
        "destination": "Patna Airport",
        "travel_date": "2026-12-02",
        "travel_time": "12:15",
        "passengers": "2",
        "luggage": "1",
        "gender_preference": "ANY",
    }
    body.update(overrides)
    return body


def test_group_requests_match_without_any_cab_offer(tmp_path):
    api = _api(tmp_path)
    first = api.student_form_group(_group_body(), _user("A", "ROLL0001"))
    assert first["student_matches"] == []

    second = api.student_form_group(
        _group_body(travel_time="13:30", passengers="1", luggage="0"),
        _user("B", "ROLL0002", "MALE"),
    )
    assert len(second["student_matches"]) == 1
    assert second["student_matches"][0]["student_name"] == "A"
    assert "contact_number" not in second["student_matches"][0]
    refreshed = api.student_group_matches(_user("A", "ROLL0001"))
    assert len(refreshed["student_matches"]) == 1


def test_group_matching_checks_route_date_and_mutual_gender_preferences(tmp_path):
    api = _api(tmp_path)
    api.student_form_group(
        _group_body(gender_preference="SAME"),
        _user("A", "ROLL0001", "FEMALE"),
    )
    result = api.student_form_group(
        _group_body(travel_time="13:00", gender_preference="ANY"),
        _user("B", "ROLL0002", "MALE"),
    )
    assert result["student_matches"] == []

    result = api.student_form_group(
        _group_body(destination="Bihta Railway Station"),
        _user("C", "ROLL0003", "FEMALE"),
    )
    assert result["student_matches"] == []


def test_group_matching_does_not_match_same_student_and_replaces_active_request(tmp_path):
    api = _api(tmp_path)
    api.student_form_group(_group_body(), _user("A", "ROLL0001"))
    updated = api.student_form_group(
        _group_body(travel_time="13:00"),
        _user("A", "ROLL0001"),
    )
    assert updated["student_matches"] == []
    assert updated["request"]["request_id"] == "GROUP-0002"
    records = api._student_read("student_group_requests.json")
    assert sum(r["status"] == "ACTIVE" for r in records) == 1


def test_owner_can_cancel_offer_and_pending_requests_are_notified(tmp_path):
    api = _api(tmp_path)
    ride = {
        "ride_id": "STU-RIDE-0001",
        "roll_number": "ROLL0001",
        "status": "OPEN",
    }
    api._student_write("student_ride_offers.json", [ride])
    api._student_write("student_join_requests.json", [
        {"join_id": "JOIN-0001", "ride_id": ride["ride_id"],
         "owner_roll_number": "ROLL0001", "request_roll_number": "ROLL0002",
         "status": "PENDING", "passengers": 1, "luggage": 0},
        {"join_id": "JOIN-0002", "ride_id": ride["ride_id"],
         "owner_roll_number": "ROLL0001", "request_roll_number": "ROLL0003",
         "status": "ACCEPTED", "passengers": 1, "luggage": 0},
    ])
    result = api.student_cancel_offer(ride["ride_id"], _user("A", "ROLL0001"))
    assert result["status"] == "CANCELLED"
    assert result["cancelled_pending_requests"] == 2
    saved_ride = api._student_read("student_ride_offers.json")[0]
    assert saved_ride["status"] == "CANCELLED"
    joins = api._student_read("student_join_requests.json")
    assert all(j["status"] == "CANCELLED_BY_OWNER" for j in joins)
    assert all(j["cancellation_reason"] for j in joins)
    assert len(api.student_my_rides(_user("A", "ROLL0001"))["offered_rides"][0]["accepted_passengers"]) == 1


def test_only_offer_owner_can_cancel_and_cancelled_offer_cannot_be_cancelled_again(tmp_path):
    api = _api(tmp_path)
    ride = {"ride_id": "STU-RIDE-0001", "roll_number": "ROLL0001", "status": "OPEN"}
    api._student_write("student_ride_offers.json", [ride])
    try:
        api.student_cancel_offer(ride["ride_id"], _user("B", "ROLL0002"))
        assert False, "non-owner cancellation should fail"
    except ApiError as exc:
        assert exc.status == 403
    api.student_cancel_offer(ride["ride_id"], _user("A", "ROLL0001"))
    try:
        api.student_cancel_offer(ride["ride_id"], _user("A", "ROLL0001"))
        assert False, "repeat cancellation should fail"
    except ApiError as exc:
        assert exc.status == 409


def test_cancelled_offer_is_not_searchable_or_joinable(tmp_path):
    api = _api(tmp_path)
    ride = {
        "ride_id": "STU-RIDE-0001", "roll_number": "ROLL0001",
        "student_name": "A", "pickup_location": "IIT Patna",
        "destination": "Patna Airport", "departure_time": "2026-12-02 12:15",
        "vehicle_type": "Uber Go", "available_seats": 3,
        "passenger_capacity": 4, "luggage_capacity": 3,
        "gender": "FEMALE", "gender_preference": "ANY",
        "status": "OPEN", "total_fare": 900,
    }
    api._student_write("student_ride_offers.json", [ride])
    api.student_cancel_offer(ride["ride_id"], _user("A", "ROLL0001"))
    search = api.student_search({
        "student_name": "B", "roll_number": "ROLL0002",
        "contact_number": "1234567890", "pickup_location": "IIT Patna",
        "destination": "Patna Airport", "travel_date": "2026-12-02",
        "travel_time": "12:15", "gender": "FEMALE",
        "gender_preference": "ANY", "passengers": "1", "luggage": "0",
    })
    assert search["matches"] == []
    try:
        api.student_join({"ride_id": ride["ride_id"], "passengers": 1, "luggage": 0},
                         _user("B", "ROLL0002"))
        assert False, "joining a cancelled offer should fail"
    except ApiError as exc:
        assert exc.status == 404
