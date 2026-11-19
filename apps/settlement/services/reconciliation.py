"""Reconcile a courier remittance statement against the merchant's orders.

Two layers:

1. `classify()` - a *pure* function over plain dataclasses. No ORM, no I/O,
   so every business rule is unit-tested in microseconds.
2. `reconcile_statement()` - loads data from the DB, calls `classify()` and
   persists the results atomically. Safe to run again (it replaces old results).

Rules (per statement = one courier, one merchant, one period):

* Each CSV line is matched to an order by tracking number (unique per courier).
* The cash we *expect* for a line is the order's amount_due if the order is
  delivered, otherwise 0 (a returned/cancelled parcel collects nothing).
* collected == expected            -> MATCHED
* collected != expected            -> AMOUNT_MISMATCH (difference recorded)
* no order with that tracking      -> UNKNOWN_TRACKING
* order delivered inside the period, not on this statement and not remitted
  on any other statement           -> MISSING_FROM_STATEMENT
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.common.money import ZERO, money_sum, quantize
from apps.logistics.models import Order

from ..models import Dispute, ReconciliationResult, RemittanceLine, RemittanceStatement

Outcome = ReconciliationResult.Outcome


# --- Pure core ----------------------------------------------------------------


@dataclass(frozen=True)
class LineData:
    row_number: int
    tracking_number: str
    amount_collected: Decimal
    fee: Decimal
    line_id: int | None = None


@dataclass(frozen=True)
class OrderData:
    order_id: int
    tracking_number: str
    amount_due: Decimal
    status: str

    @property
    def expected_collection(self) -> Decimal:
        return self.amount_due if self.status == Order.Status.DELIVERED else ZERO


@dataclass(frozen=True)
class ResultData:
    outcome: str
    tracking_number: str
    expected_amount: Decimal
    collected_amount: Decimal
    line_id: int | None = None
    order_id: int | None = None

    @property
    def difference(self) -> Decimal:
        return quantize(self.collected_amount - self.expected_amount)


@dataclass
class ReconciliationSummary:
    counts: dict[str, int] = field(default_factory=dict)
    expected_total: Decimal = ZERO
    collected_total: Decimal = ZERO
    fees_total: Decimal = ZERO

    @property
    def net_remitted(self) -> Decimal:
        return quantize(self.collected_total - self.fees_total)

    @property
    def difference_total(self) -> Decimal:
        return quantize(self.collected_total - self.expected_total)


def classify(
    lines: Iterable[LineData],
    orders_by_tracking: Mapping[str, OrderData],
    delivered_in_period: Iterable[OrderData] = (),
) -> list[ResultData]:
    """Classify every statement line, then flag delivered orders the courier left out."""
    results: list[ResultData] = []
    seen_tracking: set[str] = set()

    for line in lines:
        seen_tracking.add(line.tracking_number)
        order = orders_by_tracking.get(line.tracking_number)
        if order is None:
            results.append(
                ResultData(
                    outcome=Outcome.UNKNOWN_TRACKING,
                    tracking_number=line.tracking_number,
                    expected_amount=ZERO,
                    collected_amount=line.amount_collected,
                    line_id=line.line_id,
                )
            )
            continue

        expected = order.expected_collection
        outcome = Outcome.MATCHED if line.amount_collected == expected else Outcome.AMOUNT_MISMATCH
        results.append(
            ResultData(
                outcome=outcome,
                tracking_number=line.tracking_number,
                expected_amount=expected,
                collected_amount=line.amount_collected,
                line_id=line.line_id,
                order_id=order.order_id,
            )
        )

    for order in delivered_in_period:
        if order.tracking_number not in seen_tracking:
            results.append(
                ResultData(
                    outcome=Outcome.MISSING_FROM_STATEMENT,
                    tracking_number=order.tracking_number,
                    expected_amount=order.amount_due,
                    collected_amount=ZERO,
                    order_id=order.order_id,
                )
            )
    return results


def summarize(results: Iterable[ResultData], lines: Iterable[LineData]) -> ReconciliationSummary:
    results = list(results)
    lines = list(lines)
    counts = Counter(r.outcome for r in results)
    return ReconciliationSummary(
        counts={o.value: counts.get(o.value, 0) for o in Outcome},
        expected_total=money_sum(r.expected_amount for r in results),
        collected_total=money_sum(line.amount_collected for line in lines),
        fees_total=money_sum(line.fee for line in lines),
    )


# --- DB orchestration -----------------------------------------------------------


class ReconciliationLocked(Exception):
    """Raised when re-reconciling would discard results that are under dispute."""


def ensure_not_disputed(statement: RemittanceStatement) -> None:
    if Dispute.objects.filter(result__statement=statement).exists():
        raise ReconciliationLocked("Statement has disputes; it can no longer be re-processed.")


def _order_data(order: Order) -> OrderData:
    return OrderData(
        order_id=order.pk,
        tracking_number=order.tracking_number,
        amount_due=order.amount_due,
        status=order.status,
    )


def _other_statements(statement: RemittanceStatement):
    return RemittanceStatement.objects.filter(
        merchant_id=statement.merchant_id, courier_id=statement.courier_id
    ).exclude(pk=statement.pk)


def _delivered_in_period(statement: RemittanceStatement) -> list[Order]:
    return list(
        Order.objects.filter(
            merchant_id=statement.merchant_id,
            courier_id=statement.courier_id,
            status=Order.Status.DELIVERED,
            delivered_at__date__gte=statement.period_start,
            delivered_at__date__lte=statement.period_end,
        )
        # Already settled on another statement (e.g. overlapping periods) -> not missing.
        .exclude(remittance_lines__statement__in=_other_statements(statement))
        .distinct()
    )


@transaction.atomic
def reconcile_statement(statement: RemittanceStatement) -> ReconciliationSummary:
    # Row lock: a duplicate task delivery waits here instead of racing us.
    statement = RemittanceStatement.objects.select_for_update().get(pk=statement.pk)

    ensure_not_disputed(statement)

    line_rows = list(statement.lines.all())
    lines = [
        LineData(
            row_number=ln.row_number,
            tracking_number=ln.tracking_number,
            amount_collected=ln.amount_collected,
            fee=ln.fee,
            line_id=ln.pk,
        )
        for ln in line_rows
    ]
    orders = Order.objects.filter(
        merchant_id=statement.merchant_id,
        courier_id=statement.courier_id,
        tracking_number__in=[ln.tracking_number for ln in lines],
    )
    orders_by_tracking = {o.tracking_number: _order_data(o) for o in orders}
    delivered = [_order_data(o) for o in _delivered_in_period(statement)]

    results = classify(lines, orders_by_tracking, delivered)

    # Replace previous results so the task is idempotent.
    statement.results.all().delete()
    ReconciliationResult.objects.bulk_create(
        ReconciliationResult(
            statement=statement,
            outcome=r.outcome,
            tracking_number=r.tracking_number,
            line_id=r.line_id,
            order_id=r.order_id,
            expected_amount=r.expected_amount,
            collected_amount=r.collected_amount,
            difference=r.difference,
        )
        for r in results
    )

    # Link lines to their orders so balances can be computed with plain joins.
    order_by_line = {r.line_id: r.order_id for r in results if r.line_id is not None}
    for ln in line_rows:
        ln.order_id = order_by_line.get(ln.pk)
    RemittanceLine.objects.bulk_update(line_rows, ["order"])

    statement.status = RemittanceStatement.Status.RECONCILED
    statement.reconciled_at = timezone.now()
    statement.error_message = ""
    statement.save(update_fields=["status", "reconciled_at", "error_message"])
    return summarize(results, lines)


def summary_for_statement(statement: RemittanceStatement) -> ReconciliationSummary:
    """Rebuild the summary from persisted rows (used by the read endpoint)."""
    results = [
        ResultData(
            outcome=r.outcome,
            tracking_number=r.tracking_number,
            expected_amount=r.expected_amount,
            collected_amount=r.collected_amount,
        )
        for r in statement.results.all()
    ]
    lines = [
        LineData(
            row_number=ln.row_number,
            tracking_number=ln.tracking_number,
            amount_collected=ln.amount_collected,
            fee=ln.fee,
        )
        for ln in statement.lines.all()
    ]
    return summarize(results, lines)
