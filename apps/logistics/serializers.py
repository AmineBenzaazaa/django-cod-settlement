from django.utils import timezone
from rest_framework import serializers

from apps.accounts.models import Merchant

from .models import Courier, DeliveryAttempt, Order


class CourierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Courier
        fields = ["id", "name", "code", "contact_phone", "is_active", "created_at"]
        read_only_fields = ["created_at"]


class DeliveryAttemptSerializer(serializers.ModelSerializer):
    attempted_at = serializers.DateTimeField(default=timezone.now)

    class Meta:
        model = DeliveryAttempt
        fields = ["id", "attempted_at", "outcome", "note"]


class OrderSerializer(serializers.ModelSerializer):
    # Staff must say which merchant an order belongs to; for merchant users the
    # field is ignored and forced to their own merchant in the view.
    merchant = serializers.PrimaryKeyRelatedField(queryset=Merchant.objects.all(), required=False)
    attempts = DeliveryAttemptSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "merchant",
            "courier",
            "tracking_number",
            "customer_name",
            "customer_phone",
            "city",
            "amount_due",
            "currency",
            "status",
            "delivered_at",
            "attempts",
            "created_at",
            "updated_at",
        ]
        # Status changes go through the /attempts action so the lifecycle stays consistent.
        read_only_fields = ["status", "delivered_at", "created_at", "updated_at"]
        # DRF would auto-generate a UniqueTogetherValidator from the model constraint;
        # we check it in validate() instead to return a field-level error message.
        validators = []

    def validate_tracking_number(self, value: str) -> str:
        return value.strip().upper()

    def validate(self, attrs):
        # On PATCH one of the two fields may come from the existing instance.
        courier = attrs.get("courier", getattr(self.instance, "courier", None))
        tracking = attrs.get("tracking_number", getattr(self.instance, "tracking_number", None))
        if courier and tracking:
            clash = Order.objects.filter(courier=courier, tracking_number=tracking)
            if self.instance:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"tracking_number": "This tracking number already exists for this courier."}
                )
        return attrs


class CourierBalanceSerializer(serializers.Serializer):
    """Response shape for GET /api/couriers/{id}/balance/."""

    courier = serializers.IntegerField()
    merchant = serializers.IntegerField(allow_null=True)
    currency = serializers.CharField()
    delivered_orders = serializers.IntegerField()
    expected_collections = serializers.DecimalField(max_digits=14, decimal_places=2)
    reported_collections = serializers.DecimalField(max_digits=14, decimal_places=2)
    fees = serializers.DecimalField(max_digits=14, decimal_places=2)
    net_remitted = serializers.DecimalField(max_digits=14, decimal_places=2)
    balance_owed = serializers.DecimalField(max_digits=14, decimal_places=2)
