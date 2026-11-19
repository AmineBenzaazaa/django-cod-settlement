from django.contrib import admin

from .models import Courier, DeliveryAttempt, Order


@admin.register(Courier)
class CourierAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "contact_phone", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "code")
    prepopulated_fields = {"code": ("name",)}


class DeliveryAttemptInline(admin.TabularInline):
    model = DeliveryAttempt
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "tracking_number",
        "merchant",
        "courier",
        "customer_name",
        "city",
        "amount_due",
        "currency",
        "status",
        "delivered_at",
    )
    list_filter = ("status", "courier", "merchant", "currency")
    search_fields = ("tracking_number", "customer_name", "customer_phone")
    list_select_related = ("merchant", "courier")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "updated_at")
    inlines = [DeliveryAttemptInline]
