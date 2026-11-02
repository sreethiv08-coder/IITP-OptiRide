
"""Validation for fixed pickup and destination options."""

ALLOWED_LOCATIONS = frozenset({
    "IIT Patna",
    "Dhanapur",
    "Patna Airport",
    "Patna Railway Station",
    "Patna Junction Railway Station",
    "Ara Railway Station",
    "Bihta Railway Station",
})


def validate_route_locations(pickup, destination):
    pickup = str(pickup or "").strip()
    destination = str(destination or "").strip()

    if pickup not in ALLOWED_LOCATIONS:
        raise ValueError(
            "Please select a valid pickup location from the provided options."
        )

    if destination not in ALLOWED_LOCATIONS:
        raise ValueError(
            "Please select a valid destination from the provided options."
        )

    if pickup == destination:
        raise ValueError(
            "Pickup location and destination cannot be the same."
        )
