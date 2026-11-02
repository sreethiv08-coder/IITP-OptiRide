
"""Vehicle recommendation from vehicle_options.csv.

vehicle_options.csv is a price list, NOT a whitelist of destinations. A
destination that is absent simply has no fare information: the request stays
eligible and is reported as "manual confirmation required"; no fare is invented.

Vehicles with known fares are sorted by price. Vehicles with unknown fares
remain available for display and manual confirmation but are not used in
cheapest-fare comparisons.
"""

from __future__ import annotations

from collections import defaultdict

from .models import VehicleOption, location_key


class VehicleCatalog:
    def __init__(self, vehicles):
        self._by_destination: dict[str, list[VehicleOption]] = defaultdict(list)

        for vehicle in vehicles:
            self._by_destination[vehicle.destination_key].append(vehicle)

        for options in self._by_destination.values():
            # Known fares first, cheapest first.
            # Unknown fares remain available after priced options.
            # Ties are broken deterministically.
            options.sort(
                key=lambda v: (
                    v.total_fare is None,
                    v.total_fare if v.total_fare is not None else 0,
                    v.passenger_capacity,
                    v.luggage_capacity,
                    v.vehicle_type,
                )
            )

    def has_destination(self, destination: str) -> bool:
        return location_key(destination) in self._by_destination

    def options(self, destination: str) -> list[VehicleOption]:
        """Return all listed vehicles, including those with unknown fares."""
        return list(
            self._by_destination.get(location_key(destination), [])
        )

    def priced_options(self, destination: str) -> list[VehicleOption]:
        """Return only vehicles with a known fare."""
        return [
            vehicle
            for vehicle in self.options(destination)
            if vehicle.total_fare is not None
        ]

    def unpriced_options(self, destination: str) -> list[VehicleOption]:
        """Return vehicles that require manual fare confirmation."""
        return [
            vehicle
            for vehicle in self.options(destination)
            if vehicle.total_fare is None
        ]

    def cheapest_fitting(
        self,
        destination: str,
        passengers: int,
        luggage: int,
    ) -> VehicleOption | None:
        """Return the cheapest priced vehicle with sufficient capacity."""
        for vehicle in self._by_destination.get(
            location_key(destination), []
        ):
            if vehicle.total_fare is None:
                continue

            if (
                vehicle.passenger_capacity >= passengers
                and vehicle.luggage_capacity >= luggage
            ):
                return vehicle

        return None

    def find(
        self,
        destination: str,
        vehicle_type: str,
    ) -> VehicleOption | None:
        """Find a vehicle by destination and vehicle type."""
        for vehicle in self._by_destination.get(
            location_key(destination), []
        ):
            if vehicle.vehicle_type == vehicle_type:
                return vehicle

        return None
