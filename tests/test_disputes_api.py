import csv
import io
from decimal import Decimal

import pytest

from apps.settlement.models import Dispute, ReconciliationResult

from .factories import ResultFactory, StatementFactory

pytestmark = pytest.mark.django_db
Outcome = ReconciliationResult.Outcome


@pytest.fixture
def statement(merchant, courier):
    return StatementFactory(merchant=merchant, courier=courier)


@pytest.fixture
def mismatch(statement):
    return ResultFactory(statement=statement, tracking_number="SPX-1")


def test_open_dispute_defaults_claim_to_discrepancy(api, merchant, merchant_user, mismatch):
    resp = api.post("/api/disputes/", {"result": mismatch.pk, "reason": "Short by 20"})
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert body["claimed_amount"] == "20.00"
    assert body["status"] == "open"
    assert body["merchant"] == merchant.pk
    assert body["tracking_number"] == "SPX-1"
    assert Dispute.objects.get().opened_by == merchant_user


def test_explicit_claim_amount(api, mismatch):
    resp = api.post(
        "/api/disputes/", {"result": mismatch.pk, "reason": "x", "claimed_amount": "12.50"}
    )
    assert resp.json()["claimed_amount"] == "12.50"


def test_cannot_dispute_matched_result(api, statement):
    matched = ResultFactory(statement=statement, outcome=Outcome.MATCHED)
    resp = api.post("/api/disputes/", {"result": matched.pk, "reason": "x"})
    assert resp.status_code == 400
    assert resp.json()["result"] == ["Matched results cannot be disputed."]


def test_cannot_dispute_twice(api, mismatch):
    assert api.post("/api/disputes/", {"result": mismatch.pk, "reason": "a"}).status_code == 201
    resp = api.post("/api/disputes/", {"result": mismatch.pk, "reason": "b"})
    assert resp.status_code == 400


def test_dispute_requires_reason_and_non_negative_claim(api, mismatch):
    assert "reason" in api.post("/api/disputes/", {"result": mismatch.pk}).json()
    resp = api.post(
        "/api/disputes/", {"result": mismatch.pk, "reason": "x", "claimed_amount": "-1"}
    )
    assert "claimed_amount" in resp.json()


def test_only_staff_can_resolve(api, staff_api, mismatch):
    dispute_id = api.post("/api/disputes/", {"result": mismatch.pk, "reason": "x"}).json()["id"]
    url = f"/api/disputes/{dispute_id}/resolve/"
    assert api.post(url, {"status": "resolved"}).status_code == 403

    resp = staff_api.post(url, {"status": "resolved", "resolution_note": "Courier paid"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert resp.json()["resolved_at"]
    assert staff_api.post(url, {"status": "rejected"}).status_code == 400  # already closed
    assert staff_api.post(url, {"status": "open"}).status_code == 400


def test_csv_export_is_scoped_filtered_and_injection_safe(
    api, merchant, other_merchant, courier, mismatch
):
    api.post("/api/disputes/", {"result": mismatch.pk, "reason": "=HYPERLINK('evil')"})
    second = ResultFactory(
        statement=mismatch.statement,
        outcome=Outcome.MISSING_FROM_STATEMENT,
        expected_amount=Decimal("80.00"),
        collected_amount=Decimal("0.00"),
        difference=Decimal("-80.00"),
    )
    api.post("/api/disputes/", {"result": second.pk, "reason": "Not on statement"})
    # foreign dispute must not leak into the export
    foreign = ResultFactory(statement=StatementFactory(merchant=other_merchant, courier=courier))
    Dispute.objects.create(
        result=foreign, merchant=other_merchant, reason="theirs", claimed_amount=Decimal("1")
    )

    resp = api.get("/api/disputes/export/")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")
    assert "attachment" in resp["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(resp.content.decode())))
    assert len(rows) == 2
    reasons = {r["reason"] for r in rows}
    assert "'=HYPERLINK('evil')" in reasons  # neutralised formula
    assert "theirs" not in reasons
    assert {r["claimed_amount"] for r in rows} == {"20.00", "80.00"}

    filtered = api.get(f"/api/disputes/export/?statement={mismatch.statement_id}&status=open")
    assert len(list(csv.DictReader(io.StringIO(filtered.content.decode())))) == 2
    empty = api.get("/api/disputes/export/?status=resolved")
    assert len(list(csv.DictReader(io.StringIO(empty.content.decode())))) == 0
