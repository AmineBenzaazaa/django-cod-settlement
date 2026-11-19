"""Unit tests for the pure reconciliation core: no database involved."""

from decimal import Decimal

from apps.logistics.models import Order
from apps.settlement.services.reconciliation import (
    LineData,
    OrderData,
    Outcome,
    classify,
    summarize,
)

D = Decimal


def order(tracking, amount="100.00", status=Order.Status.DELIVERED, pk=None):
    return OrderData(
        order_id=pk or abs(hash(tracking)) % 10_000,
        tracking_number=tracking,
        amount_due=D(amount),
        status=status,
    )


def line(tracking, collected="100.00", fee="20.00", row=1):
    return LineData(
        row_number=row, tracking_number=tracking, amount_collected=D(collected), fee=D(fee)
    )


def by_tracking(*orders):
    return {o.tracking_number: o for o in orders}


def test_exact_amount_is_matched():
    o = order("A", pk=1)
    (r,) = classify([line("A")], by_tracking(o))
    assert r.outcome == Outcome.MATCHED
    assert r.order_id == 1
    assert r.difference == D("0.00")


def test_short_payment_is_amount_mismatch_with_negative_difference():
    (r,) = classify([line("A", collected="80.00")], by_tracking(order("A")))
    assert r.outcome == Outcome.AMOUNT_MISMATCH
    assert r.expected_amount == D("100.00")
    assert r.difference == D("-20.00")


def test_over_collection_is_also_a_mismatch():
    (r,) = classify([line("A", collected="100.01")], by_tracking(order("A")))
    assert r.outcome == Outcome.AMOUNT_MISMATCH
    assert r.difference == D("0.01")


def test_unknown_tracking():
    (r,) = classify([line("ZZZ", collected="50.00")], {})
    assert r.outcome == Outcome.UNKNOWN_TRACKING
    assert r.order_id is None
    assert r.expected_amount == D("0.00")
    assert r.difference == D("50.00")


def test_returned_parcel_expects_zero_cash():
    returned = order("R", status=Order.Status.RETURNED)
    ok, bad = classify(
        [line("R", collected="0.00", fee="10.00"), line("R2", collected="5.00")],
        by_tracking(returned, order("R2", status=Order.Status.CANCELLED)),
    )
    assert ok.outcome == Outcome.MATCHED
    assert bad.outcome == Outcome.AMOUNT_MISMATCH
    assert bad.expected_amount == D("0.00")


def test_cash_reported_for_undelivered_order_is_a_mismatch():
    (r,) = classify([line("S")], by_tracking(order("S", status=Order.Status.SHIPPED)))
    assert r.outcome == Outcome.AMOUNT_MISMATCH
    assert r.difference == D("100.00")


def test_delivered_order_absent_from_statement_is_missing():
    on_statement = order("A")
    absent = order("B", amount="250.50", pk=99)
    results = classify([line("A")], by_tracking(on_statement), [on_statement, absent])
    missing = [r for r in results if r.outcome == Outcome.MISSING_FROM_STATEMENT]
    assert len(missing) == 1
    assert missing[0].tracking_number == "B"
    assert missing[0].order_id == 99
    assert missing[0].collected_amount == D("0.00")
    assert missing[0].difference == D("-250.50")


def test_empty_inputs():
    assert classify([], {}, []) == []


def test_summary_totals_and_counts():
    orders = by_tracking(order("A"), order("B"), order("C", amount="300.00"))
    lines = [line("A"), line("B", collected="90.00", fee="15.00"), line("X", collected="10.00")]
    results = classify(lines, orders, delivered_in_period=list(orders.values()))
    s = summarize(results, lines)

    assert s.counts == {
        "matched": 1,
        "amount_mismatch": 1,
        "unknown_tracking": 1,
        "missing_from_statement": 1,
    }
    assert s.expected_total == D("500.00")  # 100 + 100 + 0 (unknown) + 300 (missing)
    assert s.collected_total == D("200.00")
    assert s.fees_total == D("55.00")
    assert s.net_remitted == D("145.00")
    assert s.difference_total == D("-300.00")


def test_summary_has_no_rounding_drift():
    n = 3_000
    lines = [line(f"T{i}", collected="33.33", fee="0.10", row=i) for i in range(n)]
    orders = by_tracking(*[order(f"T{i}", amount="33.33") for i in range(n)])
    s = summarize(classify(lines, orders), lines)
    assert s.collected_total == D("99990.00")
    assert s.fees_total == D("300.00")
    assert s.net_remitted == D("99690.00")
    assert s.counts["matched"] == n
