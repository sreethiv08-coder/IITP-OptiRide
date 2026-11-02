"""Compatibility rules used by the Greedy algorithm, the IP model and bookings.

Route rule
    Two parties may share a cab only if pickup AND destination are equal
    (compared case/spacing-insensitively, see models.location_key).

Time rule
    A student's effective window is [earliest - tol, latest + tol], where tol is
    Config.time_tolerance_minutes (0 by default).
    * Existing cab: its fixed departure time must lie inside the window.
    * New cab: all members' windows must have a common point. For intervals,
      pairwise overlap is enough (Helly's theorem in one dimension), which lets
      the IP model express this with pairwise constraints.

Gender rule
    Party A *accepts* party B when A's preference is
        ANY              -> always
        SAME             -> B's gender equals A's gender
        MALE/FEMALE/OTHER-> B's gender equals that value (taken literally)
    Two parties are compatible only if each accepts the other. A cab is
    compatible only if every pair of parties in it is compatible (the original
    booker of an existing cab counts as a party). The preference governs who a
    request shares with; the passengers inside one request travel together
    regardless.
    If the information needed for a decision is missing, Config.missing_gender_policy
    decides: "strict" (default) treats the pair as incompatible, "lenient" allows
    it and flags the cab as gender-unverified. Nothing is assumed silently.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations
from typing import Iterable

from .config import Config
from .models import Party, TripRequest

# Reason codes reported for requests that cannot be served.
REASONS = {
    "NO_RIDE_ON_ROUTE": "No booked cab on the requested route",
    "TIME_INCOMPATIBLE": "Departure time incompatibility",
    "INSUFFICIENT_SEATS": "Insufficient seats",
    "INSUFFICIENT_LUGGAGE": "Insufficient luggage capacity",
    "GENDER_INCOMPATIBLE": "Gender preference incompatibility",
    "GENDER_INFO_MISSING": "Gender or preference information missing (strict policy)",
    "CAPACITY_TAKEN": "Compatible cab exists, but its space went to other requests",
    "NO_VEHICLE_INFO": "Vehicle/fare information unavailable for this destination - "
                       "manual confirmation required",
    "NO_VEHICLE_LARGE_ENOUGH": "No listed vehicle can carry this request's passengers/luggage",
    "NOT_SELECTED": "Left unserved by the optimiser (serving it costs more than its penalty)",
    "INVALID_RECORD": "Record failed input validation",
}


def effective_window(req: TripRequest, cfg: Config) -> tuple[datetime, datetime]:
    tol = timedelta(minutes=cfg.time_tolerance_minutes)
    return req.earliest_departure - tol, req.latest_departure + tol


def departure_compatible(req: TripRequest, when: datetime, cfg: Config) -> bool:
    start, end = effective_window(req, cfg)
    return start <= when <= end


def windows_overlap(a: TripRequest, b: TripRequest, cfg: Config) -> bool:
    a0, a1 = effective_window(a, cfg)
    b0, b1 = effective_window(b, cfg)
    return a0 <= b1 and b0 <= a1


def common_window(requests: Iterable[TripRequest], cfg: Config):
    """Intersection of the effective windows, or None if it is empty."""
    windows = [effective_window(r, cfg) for r in requests]
    start = max(w[0] for w in windows)
    end = min(w[1] for w in windows)
    return (start, end) if start <= end else None


@dataclass(frozen=True)
class GenderCheck:
    ok: bool
    unverified: bool = False
    reason: str | None = None
    detail: str = ""


def _accepts(viewer: Party, other: Party) -> bool | None:
    """True/False, or None when the answer depends on missing information."""
    pref = viewer.preference
    if pref is None:
        return None
    if pref == "ANY":
        return True
    target = viewer.gender if pref == "SAME" else pref
    if target is None or other.gender is None:
        return None
    return other.gender == target


def gender_check(a: Party, b: Party, cfg: Config) -> GenderCheck:
    if not cfg.gender_matching:
        return GenderCheck(True)
    ab, ba = _accepts(a, b), _accepts(b, a)
    if ab is False:
        return GenderCheck(False, reason="GENDER_INCOMPATIBLE",
                           detail=f"{a.label} prefers {a.preference}; {b.label} is {b.gender}")
    if ba is False:
        return GenderCheck(False, reason="GENDER_INCOMPATIBLE",
                           detail=f"{b.label} prefers {b.preference}; {a.label} is {a.gender}")
    if ab is None or ba is None:
        missing = a.label if ab is None else b.label
        if cfg.missing_gender_policy == "strict":
            return GenderCheck(False, reason="GENDER_INFO_MISSING",
                               detail=f"cannot verify preference involving {missing}")
        return GenderCheck(True, unverified=True,
                           detail=f"allowed without verification ({missing})")
    return GenderCheck(True)


def group_gender_check(parties: Iterable[Party], cfg: Config) -> GenderCheck:
    unverified = False
    for a, b in combinations(list(parties), 2):
        result = gender_check(a, b, cfg)
        if not result.ok:
            return result
        unverified = unverified or result.unverified
    return GenderCheck(True, unverified=unverified)


def requests_can_share(a: TripRequest, b: TripRequest, cfg: Config) -> bool:
    """Route, time and gender compatibility of two requests in a NEW cab."""
    return (a.route == b.route and windows_overlap(a, b, cfg)
            and gender_check(a.party, b.party, cfg).ok)
