"""Create deterministic demo data and a matching courier CSV.

    uv run python manage.py seed_demo

Safe to run repeatedly: reference data is upserted and orders are only created
for merchants that have none yet.
"""

import csv
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Merchant, User
from apps.logistics.models import Courier, DeliveryAttempt, Order

DEMO_PASSWORD = "demo-pass-123"
PERIOD_START = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
ORDERS_PER_MERCHANT = 30
CITIES = ["Casablanca", "Rabat", "Marrakech", "Tangier", "Fes", "Agadir", "Oujda"]
FIRST_NAMES = ["Youssef", "Salma", "Omar", "Khadija", "Hamza", "Imane", "Mehdi", "Sara"]
LAST_NAMES = ["Alaoui", "Bennani", "El Idrissi", "Tazi", "Chraibi", "Berrada", "Fassi"]

MERCHANTS = [("Atlas Home", "atlas-home"), ("Medina Style", "medina-style")]
COURIERS = [("Speedex Colis", "speedex", "SPX"), ("Rapido Express", "rapido", "RPD")]


class Command(BaseCommand):
    help = "Seed demo merchants, couriers, users, orders and fixtures/sample_remittance.csv"

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv-path",
            default=str(Path(settings.BASE_DIR) / "fixtures" / "sample_remittance.csv"),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        rng = random.Random(42)  # deterministic -> the CSV always matches the orders

        merchants = [
            Merchant.objects.update_or_create(
                slug=slug, defaults={"name": name, "contact_email": f"finance@{slug}.example"}
            )[0]
            for name, slug in MERCHANTS
        ]
        couriers = [
            Courier.objects.update_or_create(code=code, defaults={"name": name})[0]
            for name, code, _ in COURIERS
        ]
        prefixes = {c.code: prefix for c, (_, _, prefix) in zip(couriers, COURIERS, strict=True)}

        self._user("ops", is_staff=True, is_superuser=True)
        for merchant in merchants:
            self._user(merchant.slug.split("-")[0], merchant=merchant)

        for m_index, merchant in enumerate(merchants):
            if merchant.orders.exists():
                continue
            for i in range(ORDERS_PER_MERCHANT):
                courier = couriers[i % len(couriers)]
                tracking = f"{prefixes[courier.code]}-{m_index + 1}{i + 1:05d}"
                self._order(rng, merchant, courier, tracking, i)

        csv_path = Path(options["csv_path"])
        rows = self._write_sample_csv(csv_path, merchants[0], couriers[0], rng)

        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write(f"  users: ops (staff), atlas, medina  password: {DEMO_PASSWORD}")
        self.stdout.write(
            f"  sample CSV: {csv_path} ({rows} rows) for {merchants[0]} / {couriers[0]}"
        )
        self.stdout.write("  upload it with period 2026-10-01 .. 2026-10-31")

    def _user(self, username, *, merchant=None, is_staff=False, is_superuser=False):
        user, _ = User.objects.update_or_create(
            username=username,
            defaults={"merchant": merchant, "is_staff": is_staff, "is_superuser": is_superuser},
        )
        user.set_password(DEMO_PASSWORD)
        user.save()
        return user

    def _order(self, rng, merchant, courier, tracking, i):
        # Mostly delivered, with a realistic sprinkle of returns / in-flight parcels.
        status = rng.choices(
            [Order.Status.DELIVERED, Order.Status.RETURNED, Order.Status.SHIPPED],
            weights=[75, 15, 10],
        )[0]
        created = PERIOD_START + timedelta(days=i % 28, hours=rng.randint(0, 8))
        order = Order.objects.create(
            merchant=merchant,
            courier=courier,
            tracking_number=tracking,
            customer_name=f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
            customer_phone=f"+2126{rng.randint(10000000, 99999999)}",
            city=rng.choice(CITIES),
            amount_due=Decimal(rng.randrange(9900, 89900, 50)) / 100,  # 99.00 .. 898.50
            status=status,
            delivered_at=created + timedelta(days=1) if status == Order.Status.DELIVERED else None,
        )
        Order.objects.filter(pk=order.pk).update(created_at=created)
        outcome = {
            Order.Status.DELIVERED: DeliveryAttempt.Outcome.DELIVERED,
            Order.Status.RETURNED: DeliveryAttempt.Outcome.REFUSED,
            Order.Status.SHIPPED: DeliveryAttempt.Outcome.NO_ANSWER,
        }[status]
        DeliveryAttempt.objects.create(
            order=order, outcome=outcome, attempted_at=created + timedelta(days=1)
        )

    def _write_sample_csv(self, path: Path, merchant, courier, rng) -> int:
        """A realistic courier file: mostly correct, plus each kind of discrepancy."""
        orders = list(
            Order.objects.filter(
                merchant=merchant,
                courier=courier,
                status__in=[Order.Status.DELIVERED, Order.Status.RETURNED],
            ).order_by("tracking_number")
        )
        fee = Decimal("25.00")
        rows = []
        delivered_seen = 0
        for order in orders:
            if order.status == Order.Status.RETURNED:
                rows.append((order.tracking_number, Decimal("0.00"), Decimal("10.00")))
                continue
            delivered_seen += 1
            if delivered_seen == 1:
                continue  # left out -> missing_from_statement
            collected = order.amount_due
            if delivered_seen == 2:
                collected -= Decimal("50.00")  # courier short-paid -> amount_mismatch
            rows.append((order.tracking_number, collected, fee))
        rows.append(("SPX-UNKNOWN01", Decimal("199.00"), fee))  # -> unknown_tracking

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["tracking_number", "amount_collected", "fee"])
            writer.writerows((t, f"{c:.2f}", f"{f:.2f}") for t, c, f in rows)
        return len(rows)
