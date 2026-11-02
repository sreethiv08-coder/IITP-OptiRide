"""Run-time configuration for IITP RideShare.

Every behaviour that is a policy choice (rather than a hard rule of the problem)
lives here, so it can be changed from the command line and is documented in one
place. See README.md, section "Configuration", for the meaning of each option.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

GENDER_POLICIES = ("strict", "lenient")
OBJECTIVE_MODES = ("lexicographic", "penalty")


@dataclass(frozen=True)
class Config:
    data_dir: Path = PROJECT_ROOT / "data"
    output_dir: Path = PROJECT_ROOT / "outputs"

    # Extra minutes allowed on both sides of a student's departure window.
    # 0 means the window is applied exactly as written in trip_requests.csv.
    time_tolerance_minutes: int = 0

    # Gender-aware matching. When False, gender columns are ignored entirely.
    gender_matching: bool = True
    # What to do when a gender or preference needed for a decision is missing:
    #   strict  -> the pair is treated as incompatible (nothing is assumed)
    #   lenient -> the pair is allowed, and the match is flagged "gender unverified"
    missing_gender_policy: str = "strict"

    # Integer Programming objective.
    #   lexicographic -> stage 1 maximises requests served, stage 2 minimises cost
    #   penalty       -> single objective: cost + penalty * unserved requests
    objective_mode: str = "lexicographic"
    # Penalty per unserved request in "penalty" mode. None = compute a safe value
    # that provably reproduces the lexicographic order (see ip_model.safe_penalty).
    unserved_penalty: float | None = None
    solver_time_limit_seconds: int = 60

    # Booking policy.
    cancellation_deadline_hours: float = 24.0
    bookings_file: Path = field(default=PROJECT_ROOT / "outputs" / "bookings.json")

    def __post_init__(self) -> None:
        if self.missing_gender_policy not in GENDER_POLICIES:
            raise ValueError(f"missing_gender_policy must be one of {GENDER_POLICIES}")
        if self.objective_mode not in OBJECTIVE_MODES:
            raise ValueError(f"objective_mode must be one of {OBJECTIVE_MODES}")
        if self.time_tolerance_minutes < 0:
            raise ValueError("time_tolerance_minutes cannot be negative")
        if self.cancellation_deadline_hours < 0:
            raise ValueError("cancellation_deadline_hours cannot be negative")
        if self.unserved_penalty is not None and self.unserved_penalty <= 0:
            raise ValueError("unserved_penalty must be positive")

    def with_(self, **changes) -> "Config":
        return replace(self, **changes)
