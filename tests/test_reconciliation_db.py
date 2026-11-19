"""Integration tests for reconcile_statement() and the courier balance, against the DB."""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.logistics.models import Order
from apps.settlement.models import Dispute, RemittanceStatement
from apps.settlement.services.balance import compute_courier_balance
from apps.settlement.services.reconciliation import (
    Outcome,
    ReconciliationLocked,
    reconcile_statement,
    summary_for_statement,
)

from .factories import LineFactory, OrderFactory, StatementFactory

pytestmark = pytest.mark.django_db
D = Decimal


@pytest.fixture
def statement(merchant, courier):
    return StatementFactory(merchant=merchant, courier=courier)


def test_reconcile_classifies_and_links_lines(statement, merchant, courier):
    matched = OrderFactory(merchant=merchant, courier=courier, tracking_number="A", delivered=True)
    short = OrderFactory(
        merchant=merchant,
        courier=courier,
        tracking_number="B",
        amount_due=D("250.00"),
        delivered=True,
    )
    missing = OrderFactory(merchant=merchant, courier=courier, tracking_number="C", delivered=True)
    LineFactory(statement=statement, tracking_number="A", amount_collected=D("100.00"))
    LineFactory(statement=statement, tracking_number="B", amount_collected=D("200.00"))
    LineFactory(statement=statement, tracking_number="NOPE", amount_collected=D("10.00"))

    summary = reconcile_statement(statement)

    outcomes = {r.tracking_number: r for r in statement.results.all()}
    assert outcomes["A"].outcome == Outcome.MATCHED
    assert outcomes["A"].order == matched
    assert outcomes["B"].outcome == Outcome.AMOUNT_MISMATCH
    assert outcomes["B"].difference == D("-50.00")
    assert outcomes["B"].order == short
    assert outcomes["NOPE"].outcome == Outcome.UNKNOWN_TRACKING
    assert outcomes["C"].outcome == Outcome.MISSING_FROM_STATEMENT
    assert outcomes["C"].order == missing
    assert summary.counts["matched"] == 1

    statement.refresh_from_db()
    assert statement.status == RemittanceStatement.Status.RECONCILED
    assert statement.reconciled_at is not None
    linked = dict(statement.lines.values_list("tracking_number", "order_id"))
    assert linked == {"A": matched.pk, "B": short.pk, "NOPE": None}


def test_other_merchants_orders_are_never_matched(statement, courier, other_merchant):
    OrderFactory(merchant=other_merchant, courier=courier, tracking_number="A", delivered=True)
    LineFactory(statement=statement, tracking_number="A")
    reconcile_statement(statement)
    assert statement.results.get().outcome == Outcome.UNKNOWN_TRACKING


def test_same_tracking_at_other_courier_is_not_matched(statement, merchant):
    OrderFactory(merchant=merchant, tracking_number="A", delivered=True)  # different courier
    LineFactory(statement=statement, tracking_number="A")
    reconcile_statement(statement)
    assert statement.results.get().outcome == Outcome.UNKNOWN_TRACKING


def test_missing_only_considers_the_statement_period(statement, merchant, courier):
    OrderFactory(
        merchant=merchant,
        courier=courier,
        status=Order.Status.DELIVERED,
        delivered_at=timezone.make_aware(timezone.datetime(2026, 11, 2, 10, 0)),
    )
    reconcile_statement(statement)
    assert not statement.results.exists()


def test_order_settled_on_another_statement_is_not_missing(statement, merchant, courier):
    OrderFactory(merchant=merchant, courier=courier, tracking_number="A", delivered=True)
    earlier = StatementFactory(merchant=merchant, courier=courier)
    LineFactory(statement=earlier, tracking_number="A")
    reconcile_statement(earlier)

    reconcile_statement(statement)  # overlapping period, A not listed again
    assert not statement.results.exists()


def test_reconcile_is_idempotent(statement, merchant, courier):
    OrderFactory(merchant=merchant, courier=courier, tracking_number="A", delivered=True)
    LineFactory(statement=statement, tracking_number="A")
    reconcile_statement(statement)
    reconcile_statement(statement)
    assert statement.results.count() == 1


def test_disputed_statement_cannot_be_reconciled_again(statement, merchant, courier):
    LineFactory(statement=statement, tracking_number="X")
    reconcile_statement(statement)
    Dispute.objects.create(
        result=statement.results.get(), merchant=merchant, reason="?", claimed_amount=D("1.00")
    )
    with pytest.raises(ReconciliationLocked):
        reconcile_statement(statement)


def test_summary_for_statement_matches_reconcile_output(statement, merchant, courier):
    OrderFactory(merchant=merchant, courier=courier, tracking_number="A", delivered=True)
    LineFactory(statement=statement, tracking_number="A", fee=D("12.50"))
    original = reconcile_statement(statement)
    rebuilt = summary_for_statement(statement)
    assert rebuilt.counts == original.counts
    assert rebuilt.fees_total == original.fees_total == D("12.50")
    assert rebuilt.net_remitted == D("87.50")


# --- balance ---------------------------------------------------------------------


def test_balance_formula(statement, merchant, courier):
    OrderFactory(
        merchant=merchant,
        courier=courier,
        tracking_number="A",
        delivered=True,
        amount_due=D("300.00"),
    )
    OrderFactory(
        merchant=merchant,
        courier=courier,
        tracking_number="B",
        delivered=True,
        amount_due=D("200.00"),
    )
    OrderFactory(
        merchant=merchant,
        courier=courier,
        tracking_number="C",
        delivered=True,
        amount_due=D("149.90"),
    )  # not remitted yet
    OrderFactory(
        merchant=merchant, courier=courier, tracking_number="R", status=Order.Status.RETURNED
    )
    OrderFactory(merchant=merchant, courier=courier, status=Order.Status.SHIPPED)  # ignored
    LineFactory(
        statement=statement, tracking_number="A", amount_collected=D("300.00"), fee=D("25.00")
    )
    LineFactory(
        statement=statement, tracking_number="B", amount_collected=D("150.00"), fee=D("25.00")
    )
    LineFactory(
        statement=statement, tracking_number="R", amount_collected=D("0.00"), fee=D("10.00")
    )
    LineFactory(
        statement=statement, tracking_number="UNKNOWN", amount_collected=D("99.00"), fee=D("5.00")
    )  # excluded: no order
    reconcile_statement(statement)

    b = compute_courier_balance(courier, merchant=merchant)

    assert b["delivered_orders"] == 3
    assert b["expected_collections"] == D("649.90")
    assert b["reported_collections"] == D("450.00")
    assert b["fees"] == D("60.00")
    assert b["net_remitted"] == D("390.00")
    assert b["balance_owed"] == D("199.90")  # 50 short on B + 149.90 not yet remitted
    assert b["balance_owed"] == b["expected_collections"] - b["net_remitted"] - b["fees"]


def test_balance_ignores_statements_not_yet_reconciled(statement, merchant, courier):
    order = OrderFactory(merchant=merchant, courier=courier, tracking_number="A", delivered=True)
    LineFactory(statement=statement, tracking_number="A", order=order)  # linked but not reconciled
    b = compute_courier_balance(courier, merchant=merchant)
    assert b["reported_collections"] == D("0.00")
    assert b["balance_owed"] == D("100.00")


def test_balance_is_scoped_per_merchant(merchant, other_merchant, courier):
    OrderFactory(merchant=merchant, courier=courier, delivered=True, amount_due=D("10.00"))
    OrderFactory(merchant=other_merchant, courier=courier, delivered=True, amount_due=D("5.00"))
    assert compute_courier_balance(courier, merchant=merchant)["balance_owed"] == D("10.00")
    assert compute_courier_balance(courier)["balance_owed"] == D("15.00")


def test_balance_has_no_rounding_drift(statement, merchant, courier):
    n = 500
    for i in range(n):
        OrderFactory(
            merchant=merchant,
            courier=courier,
            tracking_number=f"T{i}",
            amount_due=D("0.10"),
            delivered=True,
        )
        LineFactory(
            statement=statement, tracking_number=f"T{i}", amount_collected=D("0.10"), fee=D("0.01")
        )
    reconcile_statement(statement)
    b = compute_courier_balance(courier, merchant=merchant)
    assert b["expected_collections"] == D("50.00")
    assert b["reported_collections"] == D("50.00")
    assert b["fees"] == D("5.00")
    assert b["net_remitted"] == D("45.00")
    assert b["balance_owed"] == D("0.00")
