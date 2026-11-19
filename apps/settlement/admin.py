from django.contrib import admin, messages

from .models import Dispute, ReconciliationResult, RemittanceLine, RemittanceStatement
from .tasks import process_statement


class RemittanceLineInline(admin.TabularInline):
    model = RemittanceLine
    extra = 0
    fields = ("row_number", "tracking_number", "amount_collected", "fee", "order")
    readonly_fields = fields
    can_delete = False
    show_change_link = False


class ReconciliationResultInline(admin.TabularInline):
    model = ReconciliationResult
    extra = 0
    fields = ("outcome", "tracking_number", "expected_amount", "collected_amount", "difference")
    readonly_fields = fields
    can_delete = False


@admin.register(RemittanceStatement)
class RemittanceStatementAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merchant",
        "courier",
        "period_start",
        "period_end",
        "status",
        "uploaded_at",
        "reconciled_at",
    )
    list_filter = ("status", "courier", "merchant")
    search_fields = ("original_filename", "file_sha256")
    list_select_related = ("merchant", "courier")
    readonly_fields = ("file_sha256", "uploaded_at", "reconciled_at", "uploaded_by")
    date_hierarchy = "uploaded_at"
    inlines = [RemittanceLineInline, ReconciliationResultInline]
    actions = ["reprocess"]

    def has_add_permission(self, request):
        return False  # statements come in through the upload API (validation + hashing)

    @admin.action(description="Re-run import and reconciliation")
    def reprocess(self, request, queryset):
        count = 0
        for statement in queryset:
            statement.status = RemittanceStatement.Status.UPLOADED
            statement.save(update_fields=["status"])
            process_statement.delay(statement.pk)
            count += 1
        self.message_user(request, f"Queued {count} statement(s).", messages.SUCCESS)


@admin.register(ReconciliationResult)
class ReconciliationResultAdmin(admin.ModelAdmin):
    list_display = (
        "tracking_number",
        "statement",
        "outcome",
        "expected_amount",
        "collected_amount",
        "difference",
    )
    list_filter = ("outcome", "statement__courier", "statement__merchant")
    search_fields = ("tracking_number",)
    list_select_related = ("statement__courier", "statement__merchant")
    readonly_fields = [f.name for f in ReconciliationResult._meta.fields]


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ("id", "merchant", "tracking", "claimed_amount", "status", "created_at")
    list_filter = ("status", "merchant")
    search_fields = ("result__tracking_number", "reason")
    list_select_related = ("merchant", "result")
    raw_id_fields = ("result",)
    readonly_fields = ("created_at",)

    @admin.display(description="Tracking", ordering="result__tracking_number")
    def tracking(self, obj):
        return obj.result.tracking_number
