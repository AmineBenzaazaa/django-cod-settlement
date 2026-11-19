"""Parse and validate courier remittance CSV files.

Expected format (header row required, column order free, extra columns ignored):

    tracking_number,amount_collected,fee
    SPX-000123,249.00,25.00

Validation is all-or-nothing: a file with any bad row is rejected as a whole and
every problem is reported, so the courier can fix the file in one go.
"""

import csv
import hashlib
import io
from dataclasses import dataclass
from decimal import Decimal

from apps.common.money import InvalidAmount, parse_amount

REQUIRED_COLUMNS = ("tracking_number", "amount_collected", "fee")
MAX_ROWS = 50_000
MAX_REPORTED_ERRORS = 50


@dataclass(frozen=True)
class ParsedLine:
    row_number: int
    tracking_number: str
    amount_collected: Decimal
    fee: Decimal


class CsvValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def normalize_tracking(value: str) -> str:
    return value.strip().upper()


def parse_remittance_csv(content: bytes) -> list[ParsedLine]:
    try:
        # utf-8-sig transparently strips the BOM Excel adds to "CSV UTF-8" exports.
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvValidationError(["File must be UTF-8 encoded."]) from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvValidationError(["File is empty."])

    # Map normalized header -> original header so "Tracking Number " still works.
    headers = {h.strip().lower().replace(" ", "_"): h for h in reader.fieldnames if h}
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        raise CsvValidationError([f"Missing required column(s): {', '.join(missing)}."])

    lines: list[ParsedLine] = []
    errors: list[str] = []
    seen: dict[str, int] = {}

    for row_number, row in enumerate(reader, start=1):
        if row_number > MAX_ROWS:
            raise CsvValidationError([f"File has more than {MAX_ROWS} rows."])
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # tolerate blank lines (common at the end of exported files)

        tracking = normalize_tracking(row[headers["tracking_number"]] or "")
        row_errors: list[str] = []
        if not tracking:
            row_errors.append("tracking_number is empty")
        elif tracking in seen:
            row_errors.append(
                f"duplicate tracking_number {tracking} (first seen row {seen[tracking]})"
            )

        amounts: dict[str, Decimal] = {}
        for column in ("amount_collected", "fee"):
            try:
                amounts[column] = parse_amount(row[headers[column]] or "")
            except InvalidAmount as exc:
                row_errors.append(f"{column}: {exc}")

        if row_errors:
            errors.extend(f"Row {row_number}: {e}" for e in row_errors)
            if len(errors) >= MAX_REPORTED_ERRORS:
                break
            continue

        seen[tracking] = row_number
        lines.append(
            ParsedLine(
                row_number=row_number,
                tracking_number=tracking,
                amount_collected=amounts["amount_collected"],
                fee=amounts["fee"],
            )
        )

    if errors:
        raise CsvValidationError(errors[:MAX_REPORTED_ERRORS])
    if not lines:
        raise CsvValidationError(["File contains no data rows."])
    return lines
