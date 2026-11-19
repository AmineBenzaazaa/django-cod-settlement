"""Money helpers.

Rules used across the codebase:
  * money is always `decimal.Decimal`, never float (floats cannot represent 0.10 exactly);
  * amounts are quantized to 2 decimal places (centimes) with ROUND_HALF_UP,
    the rounding mode accountants expect;
  * we quantize *once*, at the boundary (parsing input / producing a total),
    so sums of already-quantized values never drift.
"""

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_DIGITS = 12  # matches the DB columns: up to 9,999,999,999.99


class InvalidAmount(ValueError):
    """Raised when a value cannot be interpreted as a valid money amount."""


def quantize(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_amount(raw: str | int | Decimal, *, allow_negative: bool = False) -> Decimal:
    """Strictly parse user/courier-supplied input into a 2-dp Decimal.

    We *reject* more than two decimal places instead of silently rounding:
    a courier file saying 10.005 MAD is a data problem we want surfaced.
    """
    if isinstance(raw, float):  # defensive: floats must never reach money code
        raise InvalidAmount("float values are not accepted for money")
    text = str(raw).strip().replace(" ", "")
    if not text:
        raise InvalidAmount("amount is empty")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise InvalidAmount(f"{raw!r} is not a number") from exc
    if not value.is_finite():
        raise InvalidAmount(f"{raw!r} is not a finite number")
    if value.as_tuple().exponent < -2:  # type: ignore[operator]
        raise InvalidAmount(f"{raw!r} has more than 2 decimal places")
    if value < 0 and not allow_negative:
        raise InvalidAmount(f"{raw!r} must not be negative")
    value = quantize(value)
    if len(value.as_tuple().digits) > MAX_DIGITS:
        raise InvalidAmount(f"{raw!r} is too large")
    return value


def money_sum(values: Iterable[Decimal | None]) -> Decimal:
    """Sum Decimals (None treated as 0) and return a quantized total."""
    total = sum((v for v in values if v is not None), start=ZERO)
    return quantize(total)
