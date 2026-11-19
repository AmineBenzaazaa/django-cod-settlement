from django.db.models import ProtectedError
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.accounts.models import Merchant
from apps.accounts.permissions import (
    IsStaffOrMerchantUser,
    IsStaffOrReadOnly,
    MerchantScopedQuerysetMixin,
)
from apps.settlement.services.balance import compute_courier_balance

from .filters import OrderFilter
from .models import Courier, Order
from .serializers import (
    CourierBalanceSerializer,
    CourierSerializer,
    DeliveryAttemptSerializer,
    OrderSerializer,
)
from .services import InvalidTransition, record_attempt


class CourierViewSet(viewsets.ModelViewSet):
    """Couriers are shared reference data: everyone reads, only staff writes."""

    queryset = Courier.objects.all()
    serializer_class = CourierSerializer
    permission_classes = [IsStaffOrMerchantUser, IsStaffOrReadOnly]
    filterset_fields = ["is_active"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "created_at"]

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError as exc:
            raise ValidationError("Courier has orders; deactivate it instead.") from exc

    @extend_schema(
        responses=CourierBalanceSerializer,
        parameters=[
            OpenApiParameter(
                "merchant", int, description="Staff only: restrict the balance to one merchant."
            )
        ],
    )
    @action(detail=True, methods=["get"])
    def balance(self, request, pk=None):
        """How much cash this courier still owes (scoped to the caller's merchant)."""
        courier = self.get_object()
        if request.user.is_staff:
            merchant_id = request.query_params.get("merchant")
            merchant = Merchant.objects.filter(pk=merchant_id).first() if merchant_id else None
            if merchant_id and merchant is None:
                raise ValidationError({"merchant": "Unknown merchant."})
        else:
            merchant = request.user.merchant
        balance = compute_courier_balance(courier, merchant=merchant)
        return Response(CourierBalanceSerializer(balance).data)


class OrderViewSet(MerchantScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = Order.objects.select_related("courier", "merchant").prefetch_related("attempts")
    serializer_class = OrderSerializer
    permission_classes = [IsStaffOrMerchantUser]
    filterset_class = OrderFilter
    search_fields = ["tracking_number", "customer_name", "customer_phone", "city"]
    ordering_fields = ["created_at", "amount_due", "delivered_at", "status"]

    def perform_create(self, serializer):
        user = self.request.user
        if user.is_staff:
            if "merchant" not in serializer.validated_data:
                raise ValidationError({"merchant": "Staff must specify the merchant."})
            serializer.save()
        else:
            # Never trust a client-supplied merchant: tenants can only write to themselves.
            serializer.save(merchant=user.merchant)

    def perform_update(self, serializer):
        if not self.request.user.is_staff:
            serializer.validated_data.pop("merchant", None)
        serializer.save()

    def perform_destroy(self, instance):
        if instance.status != Order.Status.PENDING:
            raise ValidationError("Only pending orders can be deleted; cancel it instead.")
        instance.delete()

    @extend_schema(request=DeliveryAttemptSerializer, responses=OrderSerializer)
    @action(detail=True, methods=["post"])
    def attempts(self, request, pk=None):
        """Record a delivery attempt; moves the order to shipped/delivered/returned."""
        order = self.get_object()
        serializer = DeliveryAttemptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            record_attempt(order, **serializer.validated_data)
        except InvalidTransition as exc:
            raise ValidationError({"status": str(exc)}) from exc
        order.refresh_from_db()
        return Response(self.get_serializer(order).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=OrderSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        order = self.get_object()
        if order.status not in (Order.Status.PENDING, Order.Status.SHIPPED):
            raise ValidationError({"status": f"Cannot cancel a {order.status} order."})
        order.status = Order.Status.CANCELLED
        order.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(order).data)
