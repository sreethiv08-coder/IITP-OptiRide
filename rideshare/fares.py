
"""Fare calculation and cost sharing.

Supports:
- Fixed project fares for Uber vehicles.
- Estimated fares for other cars.
- Driver-quoted fares for Auto.
- Actual fares for already-booked cab offers.
- Exact proportional fare splitting using the largest-remainder method.

Unknown Auto fares are never treated as zero and are excluded from
minimum-cost comparisons.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Optional

from .models import TripRequest

PAISA = Decimal("0.01")


# --------------------------------------------------
# VEHICLE FARE CONFIGURATION
# --------------------------------------------------

VEHICLE_FARES = {
    "Uber Go": {
        "fare": Decimal("965.00"),
        "fare_status": "FIXED",
        "capacity": 4,
    },
    "Go Non AC": {
        "fare": Decimal("842.00"),
        "fare_status": "FIXED",
        "capacity": 4,
    },
    "Premier": {
        "fare": Decimal("1203.00"),
        "fare_status": "FIXED",
        "capacity": 4,
    },
    "Uber XL": {
        "fare": Decimal("1884.00"),
        "fare_status": "FIXED",
        "capacity": 6,
    },
    "Auto": {
        "fare": None,
        "fare_status": "DRIVER_QUOTATION",
        "capacity": None,
    },
    "Other 4 Seater Car": {
        "fare": Decimal("1200.00"),
        "fare_status": "ESTIMATED",
        "capacity": 4,
    },
    "Other 6 Seater Car": {
        "fare": Decimal("1800.00"),
        "fare_status": "ESTIMATED",
        "capacity": 6,
    },
}


# --------------------------------------------------
# VEHICLE CAPACITY
# --------------------------------------------------

VEHICLE_CAPACITIES = {
    "Uber Go": (4,),
    "Go Non AC": (4,),
    "Premier": (4,),
    "Uber XL": (6,),
    "Auto": (4, 5),
    "Other 4 Seater Car": (4,),
    "Other 6 Seater Car": (6,),
}


def validate_vehicle_capacity(
    vehicle_type: str,
    passenger_capacity: int,
) -> None:
    """Validate passenger capacity for the selected vehicle."""

    if vehicle_type not in VEHICLE_CAPACITIES:
        raise ValueError("Invalid vehicle type.")

    if (
        not isinstance(passenger_capacity, int)
        or isinstance(passenger_capacity, bool)
    ):
        raise ValueError("Passenger capacity must be a whole number.")

    allowed = VEHICLE_CAPACITIES[vehicle_type]

    if passenger_capacity not in allowed:
        if vehicle_type == "Auto":
            raise ValueError("Auto passenger capacity must be 4 or 5.")

        raise ValueError(
            f"{vehicle_type} must have a passenger capacity of {allowed[0]}."
        )


# --------------------------------------------------
# FARE INFORMATION
# --------------------------------------------------

def _validated_fare(value, description: str = "Fare") -> Decimal:
    """Convert and validate a fare as a positive Decimal."""

    try:
        fare = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{description} must be a valid amount.") from None

    if not fare.is_finite() or fare <= 0:
        raise ValueError(f"{description} must be greater than zero.")

    return fare.quantize(PAISA)


def get_vehicle_fare(
    vehicle_type: str,
    actual_fare: Optional[Decimal] = None,
) -> dict:
    """Return the fare and fare status for a vehicle.

    For newly formed groups, use the configured fixed or estimated fare.

    For an already-booked cab or a quoted Auto, supply actual_fare.

    An Auto without a quotation retains total_fare=None.
    """

    if vehicle_type not in VEHICLE_FARES:
        raise ValueError("Invalid vehicle type.")

    config = VEHICLE_FARES[vehicle_type]

    if actual_fare is not None:
        fare = _validated_fare(actual_fare, "Actual fare")

        return {
            "vehicle_type": vehicle_type,
            "total_fare": fare,
            "fare_status": "ACTUAL",
            "is_priced": True,
        }

    configured_fare = config["fare"]

    return {
        "vehicle_type": vehicle_type,
        "total_fare": configured_fare,
        "fare_status": config["fare_status"],
        "is_priced": configured_fare is not None,
    }


def get_new_group_fare(vehicle_type: str) -> Optional[Decimal]:
    """Return the configured fare for a newly formed group.

    Returns None for Auto because its fare depends on the driver's quotation.
    """

    if vehicle_type not in VEHICLE_FARES:
        raise ValueError("Invalid vehicle type.")

    return VEHICLE_FARES[vehicle_type]["fare"]


def get_fare_status(vehicle_type: str) -> str:
    """Return FIXED, ESTIMATED or DRIVER_QUOTATION."""

    if vehicle_type not in VEHICLE_FARES:
        raise ValueError("Invalid vehicle type.")

    return VEHICLE_FARES[vehicle_type]["fare_status"]


def is_priced_vehicle(vehicle_type: str) -> bool:
    """Whether a vehicle has a configured fare for optimisation."""

    if vehicle_type not in VEHICLE_FARES:
        raise ValueError("Invalid vehicle type.")

    return VEHICLE_FARES[vehicle_type]["fare"] is not None


def get_cost_comparable_vehicles() -> dict[str, Decimal]:
    """Return only vehicles with known configured fares.

    Unknown Auto fares are excluded from minimum-cost comparisons.
    """

    return {
        vehicle: config["fare"]
        for vehicle, config in VEHICLE_FARES.items()
        if config["fare"] is not None
    }


# --------------------------------------------------
# FARE SPLITTING
# --------------------------------------------------

def split_fare(
    total: Optional[Decimal],
    weights: dict[str, int],
) -> dict[str, Decimal]:
    """Split total fare proportionally between passenger groups.

    The largest-remainder method ensures that all shares add up
    exactly to the total fare in whole paise.

    Raises ValueError if the fare is unknown.
    """

    if total is None:
        raise ValueError(
            "Cannot calculate fare shares because the total fare is unknown."
        )

    total = _validated_fare(total, "Total fare")

    if not weights:
        return {}

    if any(
        not isinstance(weight, int)
        or isinstance(weight, bool)
        or weight <= 0
        for weight in weights.values()
    ):
        raise ValueError(
            "Every party must represent at least one passenger."
        )

    total_paise = int(
        (total / PAISA).to_integral_value()
    )

    weight_sum = sum(weights.values())

    exact = {
        key: Decimal(total_paise) * weight / weight_sum
        for key, weight in weights.items()
    }

    floors = {
        key: int(
            value.to_integral_value(rounding=ROUND_FLOOR)
        )
        for key, value in exact.items()
    }

    leftover = total_paise - sum(floors.values())

    # Largest fractional part first.
    # Key order breaks ties deterministically.
    order = sorted(
        weights,
        key=lambda key: (
            -(exact[key] - floors[key]),
            key,
        ),
    )

    for key in order[:leftover]:
        floors[key] += 1

    return {
        key: Decimal(paise) * PAISA
        for key, paise in floors.items()
    }


def calculate_per_person_share(
    total_fare: Optional[Decimal],
    passenger_count: int,
) -> Optional[Decimal]:
    """Calculate equal per-person fare share.

    Returns None when the total fare is unknown.
    """

    if total_fare is None:
        return None

    if (
        not isinstance(passenger_count, int)
        or isinstance(passenger_count, bool)
        or passenger_count <= 0
    ):
        raise ValueError("Passenger count must be a positive integer.")

    total = _validated_fare(total_fare, "Total fare")

    return (total / passenger_count).quantize(PAISA)


# --------------------------------------------------
# SAVINGS COMPARISON
# --------------------------------------------------

def savings_summary(
    request: TripRequest,
    share: Optional[Decimal],
) -> dict:
    """Compare shared fare with the student's solo estimate."""

    if share is None:
        return {
            "solo_fare": request.solo_fare,
            "shared_fare": None,
            "savings": None,
            "verdict": "Savings cannot be calculated (fare not known)",
        }

    share = _validated_fare(share, "Shared fare")

    if request.solo_fare is None:
        return {
            "solo_fare": None,
            "shared_fare": share,
            "savings": None,
            "verdict": "Savings cannot be calculated (no solo fare provided)",
        }

    saving = (request.solo_fare - share).quantize(PAISA)

    if saving > 0:
        verdict = f"Saves Rs {saving:.2f}"
    elif saving == 0:
        verdict = "Same cost as travelling solo"
    else:
        verdict = f"Pays Rs {-saving:.2f} more than the solo estimate"

    return {
        "solo_fare": request.solo_fare,
        "shared_fare": share,
        "savings": saving,
        "verdict": verdict,
    }
