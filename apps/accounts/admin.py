from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Merchant, User


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "contact_email", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "contact_email")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "email", "merchant", "is_staff", "is_active")
    list_filter = ("is_staff", "is_active", "merchant")
    list_select_related = ("merchant",)
    fieldsets = (*BaseUserAdmin.fieldsets, ("Tenant", {"fields": ("merchant",)}))
    add_fieldsets = (*BaseUserAdmin.add_fieldsets, ("Tenant", {"fields": ("merchant",)}))
