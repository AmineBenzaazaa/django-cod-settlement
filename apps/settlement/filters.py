import django_filters

from .models import Dispute, RemittanceStatement


class StatementFilter(django_filters.FilterSet):
    period_from = django_filters.DateFilter(field_name="period_start", lookup_expr="gte")
    period_to = django_filters.DateFilter(field_name="period_end", lookup_expr="lte")

    class Meta:
        model = RemittanceStatement
        fields = ["courier", "status", "merchant"]


class DisputeFilter(django_filters.FilterSet):
    courier = django_filters.NumberFilter(field_name="result__statement__courier")
    statement = django_filters.NumberFilter(field_name="result__statement")

    class Meta:
        model = Dispute
        fields = ["status", "merchant"]
