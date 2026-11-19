from unittest import mock

import pytest
from celery.exceptions import Retry
from django.core.files.base import ContentFile

from apps.settlement.models import Dispute, RemittanceStatement
from apps.settlement.services.ingestion import register_upload
from apps.settlement.tasks import process_statement

from .factories import OrderFactory, StatementFactory

pytestmark = pytest.mark.django_db

GOOD = b"tracking_number,amount_collected,fee\nA,100.00,20.00\n"


def make_statement(merchant, courier, content=GOOD):
    statement = StatementFactory(merchant=merchant, courier=courier)
    statement.file.save("s.csv", ContentFile(content))
    return statement


def test_task_imports_lines_and_reconciles(merchant, courier):
    OrderFactory(merchant=merchant, courier=courier, tracking_number="A", delivered=True)
    statement = make_statement(merchant, courier)

    result = process_statement.delay(statement.pk).get()

    assert result["counts"]["matched"] == 1
    statement.refresh_from_db()
    assert statement.status == RemittanceStatement.Status.RECONCILED
    assert statement.lines.count() == 1


def test_task_skips_already_reconciled(merchant, courier):
    statement = make_statement(merchant, courier)
    process_statement.delay(statement.pk)
    assert process_statement.delay(statement.pk).get() == {
        "statement": statement.pk,
        "skipped": True,
    }


def test_corrupt_stored_file_marks_statement_failed(merchant, courier):
    statement = make_statement(merchant, courier, content=b"nonsense")
    assert process_statement.delay(statement.pk).get()["failed"] is True
    statement.refresh_from_db()
    assert statement.status == RemittanceStatement.Status.FAILED
    assert "Missing required column" in statement.error_message


def test_disputed_statement_is_not_reprocessed(merchant, courier):
    statement = make_statement(merchant, courier)
    process_statement.delay(statement.pk)
    Dispute.objects.create(
        result=statement.results.get(), merchant=merchant, reason="x", claimed_amount=1
    )
    RemittanceStatement.objects.filter(pk=statement.pk).update(status="uploaded")

    assert process_statement.delay(statement.pk).get()["failed"] is True
    statement.refresh_from_db()
    assert statement.status == RemittanceStatement.Status.FAILED
    assert statement.results.count() == 1  # disputed result preserved


def test_transient_error_schedules_a_retry(merchant, courier):
    statement = make_statement(merchant, courier)
    with (
        mock.patch("apps.settlement.tasks.reconcile_statement", side_effect=RuntimeError("boom")),
        mock.patch.object(process_statement, "retry", side_effect=Retry()) as retry,
        pytest.raises(Retry),
    ):
        process_statement.apply(args=[statement.pk])
    retry.assert_called_once()
    statement.refresh_from_db()
    assert statement.status == RemittanceStatement.Status.PROCESSING  # not failed yet


def test_transient_error_after_last_retry_marks_failed(merchant, courier):
    statement = make_statement(merchant, courier)
    with (
        mock.patch(
            "apps.settlement.tasks.reconcile_statement", side_effect=RuntimeError("db down")
        ),
        pytest.raises(RuntimeError),
    ):
        process_statement.apply(args=[statement.pk], retries=process_statement.max_retries)
    statement.refresh_from_db()
    assert statement.status == RemittanceStatement.Status.FAILED
    assert "db down" in statement.error_message


def test_concurrent_duplicate_upload_returns_winner(merchant, courier, merchant_user):
    kwargs = dict(
        content=GOOD,
        filename="a.csv",
        merchant=merchant,
        courier=courier,
        period_start="2026-10-01",
        period_end="2026-10-31",
        user=merchant_user,
    )
    winner = register_upload(**kwargs)
    # Simulate the race: the pre-check misses the row the other request just inserted.
    with mock.patch("django.db.models.query.QuerySet.first", return_value=None):
        loser = register_upload(**kwargs)
    assert winner.created is True
    assert loser.created is False
    assert loser.statement.pk == winner.statement.pk
