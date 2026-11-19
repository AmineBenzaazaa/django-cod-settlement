"""Tenant isolation helpers shared by every API view.

Isolation is enforced at the queryset level, not only with object permissions:
a merchant asking for another merchant's object gets a 404 (it simply does not
exist for them), which also avoids leaking which IDs are taken.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsStaffOrMerchantUser(BasePermission):
    """Authenticated *and* either staff or attached to a merchant."""

    message = "Your account is not linked to a merchant."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and (user.is_staff or user.merchant_id))


class IsStaffOrReadOnly(BasePermission):
    """Reference data (e.g. couriers) is readable by tenants, writable by staff."""

    def has_permission(self, request, view) -> bool:
        return request.method in SAFE_METHODS or bool(request.user and request.user.is_staff)


class MerchantScopedQuerysetMixin:
    """Restrict a view's queryset to the requesting user's merchant.

    `merchant_lookup` is the ORM path from the model to Merchant, e.g. "merchant"
    for Order or "statement__merchant" for a model hanging off a statement.
    """

    merchant_lookup = "merchant"

    def scope_to_merchant(self, queryset):
        user = self.request.user
        if user.is_staff:
            return queryset
        if not user.merchant_id:
            return queryset.none()
        return queryset.filter(**{f"{self.merchant_lookup}_id": user.merchant_id})

    def get_queryset(self):
        return self.scope_to_merchant(super().get_queryset())
