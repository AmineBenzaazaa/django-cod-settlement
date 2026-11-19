from django.conf import settings
from django.db import models

from apps.logistics.models import Currency


def statement_upload_path(instance: "RemittanceStatement", filename: str) -> str:
    # Content-addressed path: the same file always lands in the same place.
    return f"remittances/{instance.merchant_id}/{instance.file_sha256}.csv"


class RemittanceStatement(models.Model):
    """A courier's settlement file for one merchant over a period.

    The courier collects cash at the door, keeps its delivery fee and transfers
    the rest; this CSV says which parcels the transfer covers.
    """

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded (queued)"
        PROCESSING = "processing", "Processing"
        RECONCILED = "reconciled", "Reconciled"
        FAILED = "failed", "Failed"

    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="statements"
    )
    courier = models.ForeignKey(
        "logistics.Courier", on_delete=models.PROTECT, related_name="statements"
    )
    period_start = models.DateField()
    period_end = models.DateField()
    file = models.FileField(upload_to=statement_upload_path)
    original_filename = models.CharField(max_length=255, blank=True)
    file_sha256 = models.CharField(max_length=64, editable=False)
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=settings.DEFAULT_CURRENCY
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UPLOADED)
    error_message = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    reconciled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]
        constraints = [
            # Idempotency: re-uploading byte-identical content is a no-op.
            # Scoped per merchant so one tenant can never "see" another's file via a clash.
            models.UniqueConstraint(
                fields=["merchant", "file_sha256"], name="uniq_statement_file_per_merchant"
            ),
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F("period_start")),
                name="statement_period_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.courier} → {self.merchant} [{self.period_start}..{self.period_end}]"


class RemittanceLine(models.Model):
    """One row of the courier CSV: a parcel, the cash collected and the fee withheld."""

    statement = models.ForeignKey(
        RemittanceStatement, on_delete=models.CASCADE, related_name="lines"
    )
    row_number = models.PositiveIntegerField(help_text="1-based data row in the CSV.")
    tracking_number = models.CharField(max_length=64)
    amount_collected = models.DecimalField(max_digits=12, decimal_places=2)
    fee = models.DecimalField(max_digits=12, decimal_places=2)
    order = models.ForeignKey(
        "logistics.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="remittance_lines",
    )

    class Meta:
        ordering = ["row_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["statement", "tracking_number"], name="uniq_line_tracking_per_statement"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tracking_number}: {self.amount_collected} (fee {self.fee})"

    @property
    def net_amount(self):
        return self.amount_collected - self.fee


class ReconciliationResult(models.Model):
    class Outcome(models.TextChoices):
        MATCHED = "matched", "Matched"
        AMOUNT_MISMATCH = "amount_mismatch", "Amount mismatch"
        UNKNOWN_TRACKING = "unknown_tracking", "Unknown tracking number"
        MISSING_FROM_STATEMENT = "missing_from_statement", "Delivered but missing from statement"

    statement = models.ForeignKey(
        RemittanceStatement, on_delete=models.CASCADE, related_name="results"
    )
    outcome = models.CharField(max_length=32, choices=Outcome.choices, db_index=True)
    # A result relates a statement line, an order, or both (see Outcome).
    line = models.OneToOneField(
        RemittanceLine, on_delete=models.CASCADE, null=True, blank=True, related_name="result"
    )
    order = models.ForeignKey(
        "logistics.Order",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="reconciliation_results",
    )
    tracking_number = models.CharField(max_length=64)
    expected_amount = models.DecimalField(max_digits=12, decimal_places=2)
    collected_amount = models.DecimalField(max_digits=12, decimal_places=2)
    difference = models.DecimalField(
        max_digits=12, decimal_places=2, help_text="collected - expected (negative = shortfall)."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["outcome", "tracking_number"]

    def __str__(self) -> str:
        return f"{self.tracking_number}: {self.outcome}"


class Dispute(models.Model):
    """A merchant contesting a reconciliation discrepancy with the courier."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        REJECTED = "rejected", "Rejected"

    result = models.OneToOneField(
        ReconciliationResult, on_delete=models.PROTECT, related_name="dispute"
    )
    # Denormalised for fast tenant filtering and exports.
    merchant = models.ForeignKey(
        "accounts.Merchant", on_delete=models.PROTECT, related_name="disputes"
    )
    reason = models.TextField()
    claimed_amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    resolution_note = models.TextField(blank=True)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Dispute #{self.pk} ({self.status})"
