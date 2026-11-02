"""Fare calculation and cost sharing.

Each cab's total fare is split in proportion to the number of passengers each
party represents. The split is done in whole paise with the largest-remainder
method, so the shares always add up exactly to the total fare (to 2 decimals).
"""
from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from .models import TripRequest

PAISA = Decimal("0.01")


def split_fare(total: Decimal, weights: dict[str, int]) -> dict[str, Decimal]:
    """Split ``total`` proportionally to ``weights``; result sums exactly to total."""
    if not weights:
        return {}
    if any(w <= 0 for w in weights.values()):
        raise ValueError("every party must represent at least one passenger")
    total_paise = int((total / PAISA).to_integral_value())
    weight_sum = sum(weights.values())
    exact = {k: Decimal(total_paise) * w / weight_sum for k, w in weights.items()}
    floors = {k: int(v.to_integral_value(rounding=ROUND_FLOOR)) for k, v in exact.items()}
    leftover = total_paise - sum(floors.values())
    # Largest fractional part first; key order breaks ties deterministically.
    order = sorted(weights, key=lambda k: (-(exact[k] - floors[k]), k))
    for k in order[:leftover]:
        floors[k] += 1
    return {k: Decimal(p) * PAISA for k, p in floors.items()}


def savings_summary(request: TripRequest, share: Decimal) -> dict:
    """Compare a shared fare with the student's own solo estimate, if given."""
    if request.solo_fare is None:
        return {"solo_fare": None, "shared_fare": share, "savings": None,
                "verdict": "Savings cannot be calculated (no solo fare provided)"}
    saving = (request.solo_fare - share).quantize(PAISA)
    if saving > 0:
        verdict = f"Saves Rs {saving:.2f}"
    elif saving == 0:
        verdict = "Same cost as travelling solo"
    else:
        verdict = f"Pays Rs {-saving:.2f} more than the solo estimate"
    return {"solo_fare": request.solo_fare, "shared_fare": share, "savings": saving,
            "verdict": verdict}
