import logging

from celery import shared_task
from django.db import transaction

from .models import RemittanceStatement
from .services.csv_parser import CsvValidationError, parse_remittance_csv
from .services.ingestion import replace_lines
from .services.reconciliation import (
    ReconciliationLocked,
    ensure_not_disputed,
    reconcile_statement,
)

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_statement(self, statement_id: int) -> dict:
    """Import the CSV lines of a statement and reconcile them.

    Idempotent by design (lines and results are replaced), so Celery
    redeliveries (acks_late) or manual re-runs are harmless.
    """
    statement = RemittanceStatement.objects.get(pk=statement_id)
    if statement.status == RemittanceStatement.Status.RECONCILED:
        logger.info("Statement %s already reconciled; skipping", statement_id)
        return {"statement": statement_id, "skipped": True}

    RemittanceStatement.objects.filter(pk=statement_id).update(
        status=RemittanceStatement.Status.PROCESSING
    )
    try:
        ensure_not_disputed(statement)  # replacing lines would cascade into disputed results
        with statement.file.open("rb") as fh:
            parsed = parse_remittance_csv(fh.read())
        with transaction.atomic():
            replace_lines(statement, parsed)
            summary = reconcile_statement(statement)
    except (CsvValidationError, ReconciliationLocked) as exc:
        # Permanent failures: retrying will not help, surface them on the statement.
        _mark_failed(statement_id, str(exc))
        return {"statement": statement_id, "failed": True}
    except Exception as exc:
        logger.exception("Transient error processing statement %s", statement_id)
        if self.request.retries >= self.max_retries:
            _mark_failed(statement_id, f"Processing error: {exc}")
            raise
        raise self.retry(exc=exc) from exc

    logger.info("Statement %s reconciled: %s", statement_id, summary.counts)
    return {"statement": statement_id, "counts": summary.counts}


def _mark_failed(statement_id: int, message: str) -> None:
    RemittanceStatement.objects.filter(pk=statement_id).update(
        status=RemittanceStatement.Status.FAILED, error_message=message[:2000]
    )
