"""Order lifecycle rules, kept out of views so they are reusable and testable."""

from django.db import transaction

from .models import DeliveryAttempt, Order

# Which order status a delivery attempt outcome moves the order to.
# Outcomes not listed (no answer, rescheduled) keep the parcel "shipped".
_OUTCOME_TO_STATUS = {
    DeliveryAttempt.Outcome.DELIVERED: Order.Status.DELIVERED,
    DeliveryAttempt.Outcome.REFUSED: Order.Status.RETURNED,
    DeliveryAttempt.Outcome.RETURNED: Order.Status.RETURNED,
}

FINAL_STATUSES = {Order.Status.DELIVERED, Order.Status.RETURNED, Order.Status.CANCELLED}


class InvalidTransition(Exception):
    pass


@transaction.atomic
def record_attempt(order: Order, *, outcome: str, attempted_at, note: str = "") -> DeliveryAttempt:
    # Lock the row so two concurrent courier callbacks cannot both "deliver" it.
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status in FINAL_STATUSES:
        raise InvalidTransition(f"Order is already {order.status}.")

    attempt = DeliveryAttempt.objects.create(
        order=order, outcome=outcome, attempted_at=attempted_at, note=note
    )
    order.status = _OUTCOME_TO_STATUS.get(outcome, Order.Status.SHIPPED)
    if order.status == Order.Status.DELIVERED:
        order.delivered_at = attempted_at
    order.save(update_fields=["status", "delivered_at", "updated_at"])
    return attempt
