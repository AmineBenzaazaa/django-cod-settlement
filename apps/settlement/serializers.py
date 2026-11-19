from django.conf import settings
from rest_framework import serializers

from apps.accounts.models import Merchant
from apps.common.money import ZERO
from apps.logistics.models import Courier

from .models import Dispute, ReconciliationResult, RemittanceLine, RemittanceStatement


class StatementUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    courier = serializers.PrimaryKeyRelatedField(queryset=Courier.objects.filter(is_active=True))
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    merchant = serializers.PrimaryKeyRelatedField(
        queryset=Merchant.objects.all(), required=False, help_text="Staff only."
    )

    def validate_file(self, value):
        if value.size > settings.REMITTANCE_MAX_UPLOAD_BYTES:
            raise serializers.ValidationError("File is too large.")
        if not value.name.lower().endswith(".csv"):
            raise serializers.ValidationError("Only .csv files are accepted.")
        return value

    def validate(self, attrs):
        if attrs["period_end"] < attrs["period_start"]:
            raise serializers.ValidationError({"period_end": "Must be on or after period_start."})
        return attrs


class RemittanceStatementSerializer(serializers.ModelSerializer):
    line_count = serializers.IntegerField(source="lines.count", read_only=True)

    class Meta:
        model = RemittanceStatement
        fields = [
            "id",
            "merchant",
            "courier",
            "period_start",
            "period_end",
            "original_filename",
            "file_sha256",
            "currency",
            "status",
            "error_message",
            "line_count",
            "uploaded_at",
            "reconciled_at",
        ]
        read_only_fields = fields


class RemittanceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = RemittanceLine
        fields = ["row_number", "tracking_number", "amount_collected", "fee"]


class ReconciliationResultSerializer(serializers.ModelSerializer):
    line = RemittanceLineSerializer(read_only=True)
    has_dispute = serializers.SerializerMethodField()

    class Meta:
        model = ReconciliationResult
        fields = [
            "id",
            "outcome",
            "tracking_number",
            "order",
            "line",
            "expected_amount",
            "collected_amount",
            "difference",
            "has_dispute",
        ]

    def get_has_dispute(self, obj) -> bool:
        return hasattr(obj, "dispute")


class ReconciliationSummarySerializer(serializers.Serializer):
    counts = serializers.DictField(child=serializers.IntegerField())
    expected_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    collected_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    fees_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    net_remitted = serializers.DecimalField(max_digits=14, decimal_places=2)
    difference_total = serializers.DecimalField(max_digits=14, decimal_places=2)


class StatementReconciliationSerializer(serializers.Serializer):
    statement = RemittanceStatementSerializer()
    summary = ReconciliationSummarySerializer(allow_null=True)
    results = ReconciliationResultSerializer(many=True)


class DisputeSerializer(serializers.ModelSerializer):
    claimed_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=ZERO, required=False
    )
    tracking_number = serializers.CharField(source="result.tracking_number", read_only=True)
    outcome = serializers.CharField(source="result.outcome", read_only=True)

    class Meta:
        model = Dispute
        fields = [
            "id",
            "result",
            "tracking_number",
            "outcome",
            "merchant",
            "reason",
            "claimed_amount",
            "status",
            "resolution_note",
            "created_at",
            "resolved_at",
        ]
        read_only_fields = ["merchant", "status", "resolution_note", "created_at", "resolved_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Tenants can only dispute results on their own statements. Restricting the
        # queryset makes a foreign ID fail validation exactly like a non-existent one.
        request = self.context.get("request")
        qs = ReconciliationResult.objects.select_related("statement")
        if request and not request.user.is_staff:
            qs = qs.filter(statement__merchant_id=request.user.merchant_id)
        self.fields["result"].queryset = qs

    def validate_result(self, result: ReconciliationResult):
        # "Already disputed" is enforced by the OneToOne field's auto UniqueValidator.
        if result.outcome == ReconciliationResult.Outcome.MATCHED:
            raise serializers.ValidationError("Matched results cannot be disputed.")
        return result

    def create(self, validated_data):
        result = validated_data["result"]
        # Default claim = the full discrepancy.
        validated_data.setdefault("claimed_amount", abs(result.difference))
        validated_data["merchant_id"] = result.statement.merchant_id
        return super().create(validated_data)


class DisputeResolveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[Dispute.Status.RESOLVED, Dispute.Status.REJECTED])
    resolution_note = serializers.CharField(allow_blank=True, required=False, default="")
