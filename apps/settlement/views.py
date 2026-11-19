import csv

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from apps.accounts.permissions import IsStaffOrMerchantUser, MerchantScopedQuerysetMixin

from .filters import DisputeFilter, StatementFilter
from .models import Dispute, ReconciliationResult, RemittanceStatement
from .serializers import (
    DisputeResolveSerializer,
    DisputeSerializer,
    RemittanceStatementSerializer,
    StatementReconciliationSerializer,
    StatementUploadSerializer,
)
from .services.csv_parser import CsvValidationError
from .services.ingestion import register_upload
from .services.reconciliation import summary_for_statement
from .tasks import process_statement


class StatementViewSet(
    MerchantScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Remittance statements. Created by uploading a courier CSV; immutable afterwards."""

    queryset = RemittanceStatement.objects.select_related("courier", "merchant")
    serializer_class = RemittanceStatementSerializer
    permission_classes = [IsStaffOrMerchantUser]
    filterset_class = StatementFilter
    search_fields = ["original_filename", "file_sha256"]
    ordering_fields = ["uploaded_at", "period_start"]

    def get_parsers(self):
        if getattr(self, "action", None) == "create":
            return [MultiPartParser(), FormParser()]
        return super().get_parsers()

    @extend_schema(
        request=StatementUploadSerializer,
        responses={
            202: RemittanceStatementSerializer,
            200: OpenApiResponse(
                RemittanceStatementSerializer, description="Identical file already uploaded."
            ),
            400: OpenApiResponse(description="Invalid CSV (all row errors are listed)."),
        },
    )
    def create(self, request, *args, **kwargs):
        upload = StatementUploadSerializer(data=request.data)
        upload.is_valid(raise_exception=True)
        data = upload.validated_data

        if request.user.is_staff:
            merchant = data.get("merchant")
            if merchant is None:
                raise ValidationError({"merchant": "Staff must specify the merchant."})
        else:
            merchant = request.user.merchant

        try:
            outcome = register_upload(
                content=data["file"].read(),
                filename=data["file"].name,
                merchant=merchant,
                courier=data["courier"],
                period_start=data["period_start"],
                period_end=data["period_end"],
                user=request.user,
            )
        except CsvValidationError as exc:
            raise ValidationError({"file": exc.errors}) from exc

        body = RemittanceStatementSerializer(outcome.statement).data
        if not outcome.created:
            return Response({**body, "duplicate": True}, status=status.HTTP_200_OK)

        # Enqueue only after the DB commit, otherwise the worker could look for a
        # statement row that does not exist yet (ATOMIC_REQUESTS is on).
        statement_id = outcome.statement.pk
        transaction.on_commit(lambda: process_statement.delay(statement_id))
        return Response({**body, "duplicate": False}, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        responses=StatementReconciliationSerializer,
        parameters=[
            OpenApiParameter(
                "outcome",
                str,
                enum=[o.value for o in ReconciliationResult.Outcome],
                description="Only return results with this outcome.",
            )
        ],
    )
    @action(detail=True, methods=["get"])
    def reconciliation(self, request, pk=None):
        statement = self.get_object()
        results = statement.results.select_related("line", "dispute")
        outcome = request.query_params.get("outcome")
        if outcome:
            if outcome not in ReconciliationResult.Outcome.values:
                raise ValidationError({"outcome": "Unknown outcome."})
            results = results.filter(outcome=outcome)

        reconciled = statement.status == RemittanceStatement.Status.RECONCILED
        payload = {
            "statement": statement,
            "summary": summary_for_statement(statement) if reconciled else None,
            "results": results,
        }
        return Response(StatementReconciliationSerializer(payload).data)


# Characters that make spreadsheet apps treat a cell as a formula (CSV injection).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


class DisputeViewSet(
    MerchantScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Dispute.objects.select_related(
        "merchant", "result", "result__statement", "result__statement__courier"
    )
    serializer_class = DisputeSerializer
    permission_classes = [IsStaffOrMerchantUser]
    filterset_class = DisputeFilter
    search_fields = ["result__tracking_number", "reason"]
    ordering_fields = ["created_at", "claimed_amount"]

    def perform_create(self, serializer):
        serializer.save(opened_by=self.request.user)

    @extend_schema(request=DisputeResolveSerializer, responses=DisputeSerializer)
    @action(detail=True, methods=["post"], permission_classes=[IsAdminUser])
    def resolve(self, request, pk=None):
        """Staff closes a dispute as resolved (courier pays) or rejected."""
        dispute = self.get_object()
        if dispute.status != Dispute.Status.OPEN:
            raise ValidationError({"status": "Dispute is already closed."})
        serializer = DisputeResolveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dispute.status = serializer.validated_data["status"]
        dispute.resolution_note = serializer.validated_data["resolution_note"]
        dispute.resolved_at = timezone.now()
        dispute.save(update_fields=["status", "resolution_note", "resolved_at"])
        return Response(self.get_serializer(dispute).data)

    @extend_schema(responses={(200, "text/csv"): OpenApiResponse(description="CSV file")})
    @action(detail=False, methods=["get"])
    def export(self, request):
        """Download the (filtered) disputes as CSV, e.g. to send to the courier."""
        disputes = self.filter_queryset(self.get_queryset())
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="disputes.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                "dispute_id",
                "merchant",
                "courier",
                "statement_id",
                "tracking_number",
                "outcome",
                "expected_amount",
                "collected_amount",
                "claimed_amount",
                "status",
                "reason",
                "created_at",
            ]
        )
        for d in disputes.iterator(chunk_size=500):
            r = d.result
            writer.writerow(
                [
                    d.pk,
                    _safe_cell(d.merchant.name),
                    _safe_cell(r.statement.courier.name),
                    r.statement_id,
                    _safe_cell(r.tracking_number),
                    r.outcome,
                    r.expected_amount,
                    r.collected_amount,
                    d.claimed_amount,
                    d.status,
                    _safe_cell(d.reason),
                    d.created_at.isoformat(),
                ]
            )
        return response
