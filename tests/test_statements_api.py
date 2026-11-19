from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.settlement.models import RemittanceStatement

from .factories import OrderFactory

pytestmark = pytest.mark.django_db

CSV = b"tracking_number,amount_collected,fee\nA,100.00,20.00\nB,150.00,20.00\nZZZ,10.00,0\n"


def upload(client, courier_obj, content=CSV, name="speedex-oct.csv", **extra):
    data = {
        "file": SimpleUploadedFile(name, content, content_type="text/csv"),
        "courier": courier_obj.pk,
        "period_start": "2026-10-01",
        "period_end": "2026-10-31",
    }
    data.update(extra)
    return client.post("/api/statements/", data, format="multipart")


@pytest.fixture
def orders(merchant, courier):
    return [
        OrderFactory(merchant=merchant, courier=courier, tracking_number="A", delivered=True),
        OrderFactory(
            merchant=merchant,
            courier=courier,
            tracking_number="B",
            amount_due=Decimal("200.00"),
            delivered=True,
        ),
        OrderFactory(merchant=merchant, courier=courier, tracking_number="C", delivered=True),
    ]


def test_upload_enqueues_and_reconciles(api, courier, orders, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        resp = upload(api, courier)

    assert resp.status_code == 202, resp.json()
    assert resp.json()["duplicate"] is False
    assert len(callbacks) == 1  # the Celery task, sent only after commit

    statement_id = resp.json()["id"]
    detail = api.get(f"/api/statements/{statement_id}/").json()
    assert detail["status"] == "reconciled"
    assert detail["line_count"] == 3

    rec = api.get(f"/api/statements/{statement_id}/reconciliation/").json()
    assert rec["summary"]["counts"] == {
        "matched": 1,
        "amount_mismatch": 1,
        "unknown_tracking": 1,
        "missing_from_statement": 1,
    }
    assert rec["summary"]["collected_total"] == "260.00"
    assert rec["summary"]["fees_total"] == "40.00"
    assert rec["summary"]["net_remitted"] == "220.00"
    assert len(rec["results"]) == 4

    only = api.get(f"/api/statements/{statement_id}/reconciliation/?outcome=amount_mismatch")
    (mismatch,) = only.json()["results"]
    assert mismatch["tracking_number"] == "B"
    assert mismatch["difference"] == "-50.00"
    assert mismatch["line"]["fee"] == "20.00"


def test_reupload_of_same_file_is_a_noop(api, courier, orders, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        first = upload(api, courier)
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        second = upload(api, courier, name="renamed.csv")

    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["id"] == first.json()["id"]
    assert callbacks == []  # nothing re-enqueued
    assert RemittanceStatement.objects.count() == 1


def test_same_file_for_two_merchants_is_independent(api, other_api, courier):
    assert upload(api, courier).status_code == 202
    resp = upload(other_api, courier)
    assert resp.status_code == 202  # no cross-tenant collision / leak
    assert RemittanceStatement.objects.count() == 2


def test_different_content_creates_new_statement(api, courier):
    assert upload(api, courier).status_code == 202
    assert upload(api, courier, content=CSV + b"D,1.00,0\n").status_code == 202


def test_invalid_csv_is_rejected_with_row_errors(api, courier):
    resp = upload(api, courier, content=b"tracking_number,amount_collected,fee\nA,1.234,0\n")
    assert resp.status_code == 400
    assert resp.json()["file"] == [
        "Row 1: amount_collected: '1.234' has more than 2 decimal places"
    ]
    assert RemittanceStatement.objects.count() == 0


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"period_end": "2026-09-01"}, "period_end"),
        ({"name": "statement.xlsx"}, "file"),
        ({"courier": 999999}, "courier"),
    ],
)
def test_upload_field_validation(api, courier, overrides, field):
    overrides = dict(overrides)
    name = overrides.pop("name", "s.csv")
    resp = upload(api, courier, name=name, **overrides)
    assert resp.status_code == 400
    assert field in resp.json()


def test_upload_size_limit(api, courier, settings):
    settings.REMITTANCE_MAX_UPLOAD_BYTES = 10
    resp = upload(api, courier)
    assert resp.status_code == 400
    assert "file" in resp.json()


def test_staff_upload_requires_merchant(staff_api, merchant, courier):
    assert upload(staff_api, courier).status_code == 400
    resp = upload(staff_api, courier, merchant=merchant.pk)
    assert resp.status_code == 202
    assert resp.json()["merchant"] == merchant.pk


def test_reconciliation_of_pending_statement_has_no_summary(api, courier):
    resp = upload(api, courier)  # on_commit callbacks not executed -> still queued
    body = api.get(f"/api/statements/{resp.json()['id']}/reconciliation/").json()
    assert body["statement"]["status"] == "uploaded"
    assert body["summary"] is None
    assert body["results"] == []


def test_unknown_outcome_filter_is_rejected(api, courier):
    resp = upload(api, courier)
    url = f"/api/statements/{resp.json()['id']}/reconciliation/?outcome=bogus"
    assert api.get(url).status_code == 400


def test_statements_are_read_only_after_upload(api, courier):
    statement_id = upload(api, courier).json()["id"]
    assert api.delete(f"/api/statements/{statement_id}/").status_code == 405
    assert api.patch(f"/api/statements/{statement_id}/", {}).status_code == 405
