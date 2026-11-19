from datetime import date
from decimal import Decimal

import factory
from django.utils import timezone

from apps.accounts.models import Merchant, User
from apps.logistics.models import Courier, Order
from apps.settlement.models import (
    ReconciliationResult,
    RemittanceLine,
    RemittanceStatement,
)


class MerchantFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Merchant

    name = factory.Sequence(lambda n: f"Merchant {n}")
    slug = factory.Sequence(lambda n: f"merchant-{n}")


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        skip_postgeneration_save = True

    username = factory.Sequence(lambda n: f"user{n}")
    merchant = factory.SubFactory(MerchantFactory)
    password = factory.PostGenerationMethodCall("set_password", "pass-1234")

    @factory.post_generation
    def _save(obj, create, extracted, **kwargs):
        if create:
            obj.save()


class StaffFactory(UserFactory):
    merchant = None
    is_staff = True


class CourierFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Courier

    name = factory.Sequence(lambda n: f"Courier {n}")
    code = factory.Sequence(lambda n: f"courier-{n}")


class OrderFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Order

    merchant = factory.SubFactory(MerchantFactory)
    courier = factory.SubFactory(CourierFactory)
    tracking_number = factory.Sequence(lambda n: f"TRK-{n:06d}")
    customer_name = "Test Customer"
    city = "Casablanca"
    amount_due = Decimal("100.00")
    status = Order.Status.PENDING

    class Params:
        delivered = factory.Trait(
            status=Order.Status.DELIVERED,
            delivered_at=factory.LazyFunction(
                lambda: timezone.make_aware(timezone.datetime(2026, 10, 15, 12, 0))
            ),
        )


class StatementFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = RemittanceStatement

    merchant = factory.SubFactory(MerchantFactory)
    courier = factory.SubFactory(CourierFactory)
    period_start = date(2026, 10, 1)
    period_end = date(2026, 10, 31)
    file_sha256 = factory.Sequence(lambda n: f"{n:064x}")
    file = factory.django.FileField(
        filename="statement.csv", data=b"tracking_number,amount_collected,fee\n"
    )


class LineFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = RemittanceLine

    statement = factory.SubFactory(StatementFactory)
    row_number = factory.Sequence(lambda n: n + 1)
    tracking_number = factory.Sequence(lambda n: f"TRK-{n:06d}")
    amount_collected = Decimal("100.00")
    fee = Decimal("20.00")


class ResultFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ReconciliationResult

    statement = factory.SubFactory(StatementFactory)
    outcome = ReconciliationResult.Outcome.AMOUNT_MISMATCH
    tracking_number = factory.Sequence(lambda n: f"TRK-{n:06d}")
    expected_amount = Decimal("100.00")
    collected_amount = Decimal("80.00")
    difference = Decimal("-20.00")
