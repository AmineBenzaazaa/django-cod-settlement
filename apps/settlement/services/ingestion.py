"""Accept an uploaded remittance file and turn it into statement lines."""

from dataclasses import dataclass

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from ..models import RemittanceLine, RemittanceStatement
from .csv_parser import ParsedLine, parse_remittance_csv, sha256_of


@dataclass(frozen=True)
class UploadOutcome:
    statement: RemittanceStatement
    created: bool  # False -> identical file already uploaded (idempotent no-op)


def register_upload(
    *, content: bytes, filename: str, merchant, courier, period_start, period_end, user
) -> UploadOutcome:
    """Validate and store a new statement, or return the existing one for a re-upload.

    The file is validated synchronously (fast, gives the client immediate 400s);
    the heavier line import + reconciliation runs in a Celery task.
    """
    parse_remittance_csv(content)  # raises CsvValidationError
    digest = sha256_of(content)

    existing = RemittanceStatement.objects.filter(merchant=merchant, file_sha256=digest).first()
    if existing:
        return UploadOutcome(existing, created=False)

    statement = RemittanceStatement(
        merchant=merchant,
        courier=courier,
        period_start=period_start,
        period_end=period_end,
        file_sha256=digest,
        original_filename=filename[:255],
        uploaded_by=user,
    )
    statement.file.save(f"{digest}.csv", ContentFile(content), save=False)
    try:
        # Savepoint: two concurrent identical uploads can both pass the check above;
        # the unique constraint is the real guard, and the loser returns the winner's row.
        with transaction.atomic():
            statement.save()
    except IntegrityError:
        statement.file.delete(save=False)
        return UploadOutcome(
            RemittanceStatement.objects.get(merchant=merchant, file_sha256=digest), created=False
        )
    return UploadOutcome(statement, created=True)


@transaction.atomic
def replace_lines(statement: RemittanceStatement, parsed: list[ParsedLine]) -> None:
    """(Re)create the statement's lines from parsed rows. Idempotent."""
    statement.lines.all().delete()
    RemittanceLine.objects.bulk_create(
        RemittanceLine(
            statement=statement,
            row_number=p.row_number,
            tracking_number=p.tracking_number,
            amount_collected=p.amount_collected,
            fee=p.fee,
        )
        for p in parsed
    )
