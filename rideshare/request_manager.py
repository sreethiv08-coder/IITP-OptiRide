"""Student request management: lookup, grouping by route, and new submissions."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from .data_loader import GENDERS, PREFERENCES
from .models import Dataset, TripRequest


class RequestBook:
    def __init__(self, requests):
        self._by_id: dict[str, TripRequest] = {}
        for r in requests:
            self.add(r)

    def add(self, request: TripRequest) -> None:
        if request.request_id in self._by_id:
            raise ValueError(f"Request {request.request_id} already exists")
        self._by_id[request.request_id] = request

    def get(self, request_id: str) -> TripRequest:
        try:
            return self._by_id[request_id]
        except KeyError:
            raise KeyError(f"Unknown request {request_id}") from None

    def __contains__(self, request_id: str) -> bool:
        return request_id in self._by_id

    def __iter__(self):
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def by_route(self) -> dict[tuple[str, str], list[TripRequest]]:
        groups = defaultdict(list)
        for r in self._by_id.values():
            groups[r.route].append(r)
        return dict(groups)

    @classmethod
    def from_dataset(cls, dataset: Dataset) -> "RequestBook":
        return cls(dataset.requests)


def submit_request(book: RequestBook, *, request_id: str, student_name: str,
                   pickup_location: str, destination: str, earliest: datetime,
                   latest: datetime, passengers: int, luggage: int,
                   gender: str | None = None, gender_preference: str | None = None,
                   solo_fare: Decimal | None = None) -> TripRequest:
    """Validate and register a new travel request (same rules as the CSV loader).

    Any destination is accepted: vehicle availability is decided later.
    """
    errors = []
    if not request_id.strip() or not student_name.strip():
        errors.append("request_id and student_name are required")
    if not pickup_location.strip() or not destination.strip():
        errors.append("pickup_location and destination are required")
    if earliest > latest:
        errors.append("earliest departure is later than latest departure")
    if passengers < 1:
        errors.append("passengers must be at least 1")
    if luggage < 0:
        errors.append("luggage cannot be negative")
    if solo_fare is not None and solo_fare <= 0:
        errors.append("solo_fare must be positive")
    g = GENDERS.get(gender.casefold()) if gender else None
    p = PREFERENCES.get(gender_preference.casefold()) if gender_preference else None
    if gender and g is None:
        errors.append(f"invalid gender '{gender}'")
    if gender_preference and p is None:
        errors.append(f"invalid gender preference '{gender_preference}'")
    if request_id in book:
        errors.append(f"request {request_id} already exists")
    if errors:
        raise ValueError("; ".join(errors))
    request = TripRequest(request_id, student_name, pickup_location, destination, earliest,
                          latest, passengers, luggage, g, p, solo_fare)
    book.add(request)
    return request
