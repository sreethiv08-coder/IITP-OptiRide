"""Vehicle recommendation from vehicle_options.csv.

vehicle_options.csv is a price list, NOT a whitelist of destinations. A
destination that is absent simply has no fare information: the request stays
eligible and is reported as "manual confirmation required"; no fare is invented.
"""
from __future__ import annotations

from collections import defaultdict

from .models import VehicleOption, location_key


class VehicleCatalog:
    def __init__(self, vehicles):
        self._by_destination: dict[str, list[VehicleOption]] = defaultdict(list)
        for v in vehicles:
            self._by_destination[v.destination_key].append(v)
        for options in self._by_destination.values():
            # Cheapest first; ties broken by smaller vehicle, then name (deterministic).
            options.sort(key=lambda v: (v.total_fare, v.passenger_capacity,
                                        v.luggage_capacity, v.vehicle_type))

    def has_destination(self, destination: str) -> bool:
        return location_key(destination) in self._by_destination

    def options(self, destination: str) -> list[VehicleOption]:
        return list(self._by_destination.get(location_key(destination), []))

    def cheapest_fitting(self, destination: str, passengers: int,
                         luggage: int) -> VehicleOption | None:
        """Cheapest listed vehicle whose seats and luggage space both suffice."""
        for v in self._by_destination.get(location_key(destination), []):
            if v.passenger_capacity >= passengers and v.luggage_capacity >= luggage:
                return v
        return None

    def find(self, destination: str, vehicle_type: str) -> VehicleOption | None:
        for v in self._by_destination.get(location_key(destination), []):
            if v.vehicle_type == vehicle_type:
                return v
        return None
