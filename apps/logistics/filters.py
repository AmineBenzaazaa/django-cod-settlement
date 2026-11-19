import django_filters

from .models import Order


class OrderFilter(django_filters.FilterSet):
    min_amount = django_filters.NumberFilter(field_name="amount_due", lookup_expr="gte")
    max_amount = django_filters.NumberFilter(field_name="amount_due", lookup_expr="lte")
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")
    status = django_filters.MultipleChoiceFilter(choices=Order.Status.choices)

    class Meta:
        model = Order
        fields = ["status", "courier", "merchant", "city", "currency"]
