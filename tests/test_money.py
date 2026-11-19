from decimal import Decimal

import pytest

from apps.common.money import InvalidAmount, money_sum, parse_amount, quantize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10", Decimal("10.00")),
        ("10.5", Decimal("10.50")),
        (" 1 249.90 ", Decimal("1249.90")),
        ("0", Decimal("0.00")),
        (Decimal("3.1"), Decimal("3.10")),
        (7, Decimal("7.00")),
    ],
)
def test_parse_amount_valid(raw, expected):
    value = parse_amount(raw)
    assert value == expected
    assert value.as_tuple().exponent == -2


@pytest.mark.parametrize("raw", ["", "abc", "10.005", "-1", "NaN", "Infinity", "1e15"])
def test_parse_amount_rejects_bad_input(raw):
    with pytest.raises(InvalidAmount):
        parse_amount(raw)


def test_parse_amount_rejects_floats():
    with pytest.raises(InvalidAmount):
        parse_amount(0.1)


def test_parse_amount_allows_negative_when_asked():
    assert parse_amount("-5.25", allow_negative=True) == Decimal("-5.25")


def test_quantize_rounds_half_up():
    assert quantize(Decimal("2.345")) == Decimal("2.35")
    assert quantize(Decimal("2.344")) == Decimal("2.34")


def test_no_rounding_drift_on_large_sums():
    # The classic float bug: 0.1 added 10 000 times is not 1000.0 in binary floating point.
    # (A plain loop on purpose: Python 3.12+ sum() uses compensated summation for floats.)
    float_total = 0.0
    for _ in range(10_000):
        float_total += 0.1
    assert float_total != 1000.0
    assert money_sum([Decimal("0.10")] * 10_000) == Decimal("1000.00")


def test_money_sum_ignores_none_and_is_quantized():
    total = money_sum([Decimal("19.99"), None, Decimal("0.01")])
    assert total == Decimal("20.00")
    assert str(total) == "20.00"
