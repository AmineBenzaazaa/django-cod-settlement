"""Courier cash position.

    expected_collections = Σ amount_due of the courier's DELIVERED orders
    reported_collections = Σ amount_collected on reconciled statement lines linked to any
                           of the courier's orders (incl. returned parcels, collected 0)
    fees                 = Σ fee withheld on those lines
    net_remitted         = reported_collections - fees      (what reached the merchant's bank)
    balance_owed         = expected_collections - reported_collections
                         = expected_collections - (net_remitted + fees)

A positive balance_owed is cash the courier collected at the door (or should have)
but has not yet accounted for. Unknown-tracking lines are excluded: they belong to
no order of ours and are handled as disputes instead.
"""

from django.conf import settings
from django.db.models import Count, DecimalField, Sum, Value
from django.db.models.functions import Coalesce

from apps.common.money import ZERO, quantize
from apps.logistics.models import Order

from ..models import RemittanceLine, RemittanceStatement

_MONEY = DecimalField(max_digits=14, decimal_places=2)


def _sum(field: str):
    return Coalesce(Sum(field), Value(ZERO), output_field=_MONEY)


def compute_courier_balance(courier, merchant=None) -> dict:
    orders = Order.objects.filter(courier=courier)
    lines = RemittanceLine.objects.filter(
        order__courier=courier,
        statement__status=RemittanceStatement.Status.RECONCILED,
    )
    if merchant is not None:
        orders = orders.filter(merchant=merchant)
        lines = lines.filter(statement__merchant=merchant)

    delivered = orders.filter(status=Order.Status.DELIVERED).aggregate(
        total=_sum("amount_due"), count=Count("id")
    )
    line_totals = lines.aggregate(collected=_sum("amount_collected"), fees=_sum("fee"))

    expected = quantize(delivered["total"])
    collected = quantize(line_totals["collected"])
    fees = quantize(line_totals["fees"])
    return {
        "courier": courier.pk,
        "merchant": merchant.pk if merchant else None,
        "currency": settings.DEFAULT_CURRENCY,
        "delivered_orders": delivered["count"],
        "expected_collections": expected,
        "reported_collections": collected,
        "fees": fees,
        "net_remitted": quantize(collected - fees),
        "balance_owed": quantize(expected - collected),
    }
