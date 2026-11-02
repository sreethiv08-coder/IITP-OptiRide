
"""Load the three input CSV files and validate every record.

Rules:
* A missing file or a missing required column is fatal (DataValidationError).
* A bad record is never dropped silently: it gets an ERROR issue, is excluded
  from optimisation, and (for requests) is reported as unmatched/INVALID_RECORD.
* Suspicious-but-usable data gets a WARNING and is kept.
* The CSV files are only read, never modified.
* Vehicle options may have an unknown fare, which requires manual confirmation.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import (
    Dataset,
    ExistingRide,
    TripRequest,
    ValidationIssue,
    VehicleOption,
    location_key,
)

TRIP_FILE = "trip_requests.csv"
RIDES_FILE = "available_rides.csv"
VEHICLES_FILE = "vehicle_options.csv"

TRIP_REQUIRED = [
    "request_id",
    "student_name",
    "pickup_location",
    "destination",
    "earliest_departure",
    "latest_departure",
    "passengers",
    "luggage",
]

TRIP_OPTIONAL = [
    "gender",
    "gender_preference",
    "solo_fare",
    "roll_number",
    "contact_number",
    "ride_status",
]

RIDES_REQUIRED = [
    "ride_id",
    "booked_by",
    "pickup_location",
    "destination",
    "departure_time",
    "vehicle_type",
    "total_fare",
    "passenger_capacity",
    "luggage_capacity",
    "booked_passengers",
    "booked_luggage",
]

RIDES_OPTIONAL = [
    "booker_gender",
    "booker_gender_preference",
]

VEHICLES_REQUIRED = [
    "vehicle_type",
    "destination",
    "passenger_capacity",
    "luggage_capacity",
    "total_fare",
]

DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
)

GENDERS = {
    "male": "MALE",
    "female": "FEMALE",
    "other": "OTHER",
}

PREFERENCES = {
    "any": "ANY",
    "same": "SAME",
    "male": "MALE",
    "female": "FEMALE",
    "other": "OTHER",
}


class DataValidationError(Exception):
    """Raised when the input cannot be used at all (missing file / columns)."""

    def __init__(self, messages: list[str]):
        super().__init__("\n".join(messages))
        self.messages = messages


class _RowChecker:
    """Collects the errors of one CSV row while converting its fields."""

    def __init__(
        self,
        file: str,
        row_no: int,
        record_id: str,
        issues: list,
    ):
        self.file = file
        self.row_no = row_no
        self.record_id = record_id
        self.issues = issues
        self.errors: list[str] = []

    def error(self, column: str, message: str) -> None:
        self.errors.append(f"{column}: {message}")
        self.issues.append(
            ValidationIssue(
                "ERROR",
                self.file,
                self.row_no,
                self.record_id,
                column,
                message,
            )
        )

    def warning(self, column: str, message: str) -> None:
        self.issues.append(
            ValidationIssue(
                "WARNING",
                self.file,
                self.row_no,
                self.record_id,
                column,
                message,
            )
        )

    def text(self, row: dict, column: str) -> str:
        value = (row.get(column) or "").strip()

        if not value:
            self.error(column, "value is missing")

        return value

    def datetime(self, row: dict, column: str) -> datetime | None:
        raw = self.text(row, column)

        if not raw:
            return None

        for fmt in DATETIME_FORMATS:
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue

        self.error(
            column,
            f"invalid date-time '{raw}' (expected YYYY-MM-DD HH:MM)",
        )
        return None

    def integer(
        self,
        row: dict,
        column: str,
        minimum: int,
    ) -> int | None:
        raw = self.text(row, column)

        if not raw:
            return None

        try:
            value = int(raw)
        except ValueError:
            self.error(column, f"'{raw}' is not a whole number")
            return None

        if value < minimum:
            self.error(
                column,
                f"{value} is below the minimum allowed value {minimum}",
            )
            return None

        return value

    def money(
        self,
        row: dict,
        column: str,
        required: bool = True,
    ) -> Decimal | None:
        raw = (row.get(column) or "").strip()

        if not raw:
            if required:
                self.error(column, "value is missing")
            return None

        try:
            value = Decimal(raw)
        except InvalidOperation:
            self.error(column, f"'{raw}' is not a valid amount")
            return None

        if not value.is_finite() or value <= 0:
            self.error(
                column,
                f"fare must be a positive amount, got '{raw}'",
            )
            return None

        return value.quantize(Decimal("0.01"))

    def choice(
        self,
        row: dict,
        column: str,
        allowed: dict,
    ) -> str | None:
        raw = (row.get(column) or "").strip()

        if not raw:
            return None

        value = allowed.get(raw.casefold())

        if value is None:
            self.error(
                column,
                f"invalid value '{raw}' "
                f"(allowed: {', '.join(sorted(set(allowed.values())))})",
            )

        return value


def _read_csv(
    path: Path,
    required: list[str],
) -> tuple[list[str], list[dict]]:
    if not path.is_file():
        raise DataValidationError([f"Missing input file: {path}"])

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = [
            h.strip()
            for h in (reader.fieldnames or [])
        ]
        reader.fieldnames = header

        missing = [
            column
            for column in required
            if column not in header
        ]

        if missing:
            raise DataValidationError(
                [
                    f"{path.name}: missing required column(s): "
                    f"{', '.join(missing)}. "
                    f"Columns found: {', '.join(header) or '(none)'}"
                ]
            )

        return header, list(reader)


def _flag_duplicates(
    rows: list[dict],
    id_column: str,
    file: str,
    issues: list,
) -> set[int]:
    """Return indexes of rows whose ID already appeared earlier."""
    seen: dict[str, int] = {}
    duplicates = set()

    for i, row in enumerate(rows):
        key = (row.get(id_column) or "").strip()

        if not key:
            continue

        if key in seen:
            duplicates.add(i)
            issues.append(
                ValidationIssue(
                    "ERROR",
                    file,
                    i + 1,
                    key,
                    id_column,
                    f"duplicate {id_column} "
                    f"(first seen on row {seen[key] + 1})",
                )
            )
        else:
            seen[key] = i

    return duplicates


def _load_requests(path: Path, issues: list):
    header, rows = _read_csv(path, TRIP_REQUIRED)

    for column in ("gender", "gender_preference"):
        if column not in header:
            issues.append(
                ValidationIssue(
                    "WARNING",
                    path.name,
                    None,
                    "",
                    column,
                    "column absent: gender-aware matching "
                    "will treat it as unknown",
                )
            )

    duplicates = _flag_duplicates(
        rows,
        "request_id",
        path.name,
        issues,
    )

    requests = []
    invalid = {}

    for i, row in enumerate(rows):
        rid = (
            (row.get("request_id") or "").strip()
            or f"<row {i + 1}>"
        )

        if i in duplicates:
            invalid[f"{rid}@row{i + 1}"] = ("duplicate request_id",)
            continue

        chk = _RowChecker(path.name, i + 1, rid, issues)

        chk.text(row, "request_id")
        name = chk.text(row, "student_name")
        pickup = chk.text(row, "pickup_location")
        dest = chk.text(row, "destination")
        earliest = chk.datetime(row, "earliest_departure")
        latest = chk.datetime(row, "latest_departure")
        passengers = chk.integer(row, "passengers", minimum=1)
        luggage = chk.integer(row, "luggage", minimum=0)
        gender = chk.choice(row, "gender", GENDERS)
        preference = chk.choice(
            row,
            "gender_preference",
            PREFERENCES,
        )
        solo = chk.money(row, "solo_fare", required=False)

        if earliest and latest and earliest > latest:
            chk.error(
                "earliest_departure",
                f"earliest ({earliest:%Y-%m-%d %H:%M}) is later "
                f"than latest ({latest:%Y-%m-%d %H:%M})",
            )

        if chk.errors:
            invalid[rid] = tuple(chk.errors)
            continue

        requests.append(
            TripRequest(
                request_id=rid,
                student_name=name,
                pickup_location=pickup,
                destination=dest,
                earliest_departure=earliest,
                latest_departure=latest,
                passengers=passengers,
                luggage=luggage,
                gender=gender,
                gender_preference=preference,
                solo_fare=solo,
                roll_number=(row.get("roll_number") or "").strip(),
                ride_status=(row.get("ride_status") or "").strip(),
            )
        )

    _warn_shared_roll_numbers(requests, path.name, issues)

    if "ride_status" in header:
        issues.append(
            ValidationIssue(
                "WARNING",
                path.name,
                None,
                "",
                "ride_status",
                "column is not part of the specification; it is kept "
                "for reference but not used in matching",
            )
        )

    return requests, invalid


def _warn_shared_roll_numbers(requests, file, issues):
    by_roll = defaultdict(list)

    for request in requests:
        if request.roll_number:
            by_roll[request.roll_number].append(request)

    for roll, request_list in sorted(by_roll.items()):
        names = {
            request.student_name
            for request in request_list
        }

        if len(request_list) > 1 and len(names) > 1:
            ids = ", ".join(
                request.request_id
                for request in request_list
            )

            issues.append(
                ValidationIssue(
                    "WARNING",
                    file,
                    None,
                    ids,
                    "roll_number",
                    f"roll number {roll} is used by different names "
                    f"({', '.join(sorted(names))})",
                )
            )


def _load_rides(path: Path, issues: list):
    header, rows = _read_csv(path, RIDES_REQUIRED)

    gender_cols = all(
        column in header
        for column in RIDES_OPTIONAL
    )

    if not gender_cols:
        issues.append(
            ValidationIssue(
                "WARNING",
                path.name,
                None,
                "",
                "booker_gender",
                "booker_gender / booker_gender_preference columns "
                "are absent: the booker's gender and preference "
                "are UNKNOWN (see --gender-policy)",
            )
        )

    duplicates = _flag_duplicates(
        rows,
        "ride_id",
        path.name,
        issues,
    )

    rides = []
    invalid = {}

    for i, row in enumerate(rows):
        rid = (
            (row.get("ride_id") or "").strip()
            or f"<row {i + 1}>"
        )

        if i in duplicates:
            invalid[f"{rid}@row{i + 1}"] = ("duplicate ride_id",)
            continue

        chk = _RowChecker(path.name, i + 1, rid, issues)

        booked_by = chk.text(row, "booked_by")
        pickup = chk.text(row, "pickup_location")
        dest = chk.text(row, "destination")
        departure = chk.datetime(row, "departure_time")
        vehicle = chk.text(row, "vehicle_type")
        fare = chk.money(row, "total_fare")
        pax_cap = chk.integer(row, "passenger_capacity", minimum=1)
        lug_cap = chk.integer(row, "luggage_capacity", minimum=0)
        booked_pax = chk.integer(row, "booked_passengers", minimum=0)
        booked_lug = chk.integer(row, "booked_luggage", minimum=0)
        gender = chk.choice(row, "booker_gender", GENDERS)
        preference = chk.choice(
            row,
            "booker_gender_preference",
            PREFERENCES,
        )

        if (
            pax_cap is not None
            and booked_pax is not None
            and booked_pax > pax_cap
        ):
            chk.error(
                "booked_passengers",
                f"{booked_pax} booked exceeds capacity {pax_cap}",
            )

        if (
            lug_cap is not None
            and booked_lug is not None
            and booked_lug > lug_cap
        ):
            chk.error(
                "booked_luggage",
                f"{booked_lug} booked exceeds luggage capacity {lug_cap}",
            )

        if chk.errors:
            invalid[rid] = tuple(chk.errors)
            continue

        rides.append(
            ExistingRide(
                rid,
                booked_by,
                pickup,
                dest,
                departure,
                vehicle,
                fare,
                pax_cap,
                lug_cap,
                booked_pax,
                booked_lug,
                gender,
                preference,
            )
        )

    return rides, invalid, gender_cols


def _load_vehicles(path: Path, issues: list):
    _, rows = _read_csv(path, VEHICLES_REQUIRED)

    vehicles = []
    seen = {}

    for i, row in enumerate(rows):
        vtype = (row.get("vehicle_type") or "").strip()
        dest = (row.get("destination") or "").strip()

        chk = _RowChecker(
            path.name,
            i + 1,
            f"{vtype}/{dest}",
            issues,
        )

        chk.text(row, "vehicle_type")
        chk.text(row, "destination")

        pax_cap = chk.integer(
            row,
            "passenger_capacity",
            minimum=1,
        )
        lug_cap = chk.integer(
            row,
            "luggage_capacity",
            minimum=0,
        )

        # Vehicle catalogue fares may be blank.
        # A blank fare becomes None and requires manual confirmation.
        fare_raw = (row.get("total_fare") or "").strip()
        fare = chk.money(
            row,
            "total_fare",
            required=False,
        )

        if not fare_raw:
            chk.warning(
                "total_fare",
                "fare is unknown; manual confirmation or a driver "
                "quotation is required. This vehicle will not be "
                "used in cost optimisation until a fare is provided.",
            )

        key = (
            vtype.casefold(),
            location_key(dest),
        )

        if vtype and dest and key in seen:
            chk.error(
                "vehicle_type",
                f"duplicate vehicle option (first seen on row {seen[key]})",
            )

        seen.setdefault(key, i + 1)

        if not chk.errors:
            vehicles.append(
                VehicleOption(
                    vtype,
                    dest,
                    pax_cap,
                    lug_cap,
                    fare,
                )
            )

    return vehicles


def load_dataset(data_dir: Path) -> Dataset:
    data_dir = Path(data_dir)

    issues: list[ValidationIssue] = []
    fatal = []
    results = {}

    for name, loader in (
        (TRIP_FILE, _load_requests),
        (RIDES_FILE, _load_rides),
        (VEHICLES_FILE, _load_vehicles),
    ):
        try:
            results[name] = loader(data_dir / name, issues)
        except DataValidationError as exc:
            fatal.extend(exc.messages)

    if fatal:
        raise DataValidationError(fatal)

    requests, invalid_requests = results[TRIP_FILE]
    rides, invalid_rides, gender_cols = results[RIDES_FILE]
    vehicles = results[VEHICLES_FILE]

    return Dataset(
        tuple(requests),
        tuple(rides),
        tuple(vehicles),
        tuple(issues),
        invalid_requests,
        invalid_rides,
        gender_cols,
    )
