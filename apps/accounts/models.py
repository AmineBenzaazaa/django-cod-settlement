from django.contrib.auth.models import AbstractUser
from django.db import models


class Merchant(models.Model):
    """An e-commerce shop whose parcels are delivered cash-on-delivery.

    The merchant is the tenant boundary: every order, statement and dispute
    belongs to exactly one merchant, and merchant users only see their own data.
    """

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    contact_email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class User(AbstractUser):
    """Custom user so we can attach a merchant (and evolve auth later).

    - merchant set, is_staff False -> merchant user, scoped to that merchant
    - is_staff True                 -> operations staff, sees all merchants
    """

    merchant = models.ForeignKey(
        Merchant,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="users",
    )

