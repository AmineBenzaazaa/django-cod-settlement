import csv
from pathlib import Path

import pytest
from django.contrib.admin.sites import site
from django.core.management import call_command

from apps.accounts.models import Merchant, User
from apps.logistics.models import Order
from apps.settlement.services.csv_parser import parse_remittance_csv

pytestmark = pytest.mark.django_db


def test_openapi_schema_and_swagger(anon_api):
    schema = anon_api.get("/api/schema/")
    assert schema.status_code == 200
    text = schema.content.decode()
    for path in [
        "/api/couriers/{id}/balance/",
        "/api/statements/{id}/reconciliation/",
        "/api/disputes/export/",
    ]:
        assert path in text
    assert anon_api.get("/api/docs/").status_code == 200


def test_jwt_login_flow(anon_api, merchant_user):
    resp = anon_api.post("/api/auth/token/", {"username": "atlas", "password": "pass-1234"})
    assert resp.status_code == 200
    access = resp.json()["access"]
    anon_api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert anon_api.get("/api/orders/").status_code == 200

    bad = anon_api.post("/api/auth/token/", {"username": "atlas", "password": "nope"})
    assert bad.status_code == 401


def test_seed_demo_is_idempotent_and_csv_is_valid(tmp_path):
    csv_path = Path(tmp_path) / "sample.csv"
    call_command("seed_demo", csv_path=str(csv_path))
    call_command("seed_demo", csv_path=str(csv_path))

    assert Merchant.objects.count() == 2
    assert Order.objects.count() == 60
    assert User.objects.filter(is_staff=True).count() == 1
    lines = parse_remittance_csv(csv_path.read_bytes())
    assert any(ln.tracking_number == "SPX-UNKNOWN01" for ln in lines)
    with csv_path.open() as fh:
        assert next(csv.reader(fh)) == ["tracking_number", "amount_collected", "fee"]


@pytest.mark.parametrize(
    "url",
    [
        "/admin/settlement/remittancestatement/",
        "/admin/logistics/order/",
        "/admin/settlement/dispute/",
        "/admin/settlement/reconciliationresult/",
        "/admin/accounts/user/",
        "/admin/accounts/merchant/",
        "/admin/logistics/courier/",
    ],
)
def test_admin_changelists_render(client, url, tmp_path):
    call_command("seed_demo", csv_path=str(tmp_path / "s.csv"))
    client.force_login(User.objects.get(username="ops"))
    assert client.get(url).status_code == 200


def test_admin_statement_detail_and_reprocess(client, tmp_path, api, courier):
    from django.core.files.uploadedfile import SimpleUploadedFile

    resp = api.post(
        "/api/statements/",
        {
            "file": SimpleUploadedFile("s.csv", b"tracking_number,amount_collected,fee\nA,1,0\n"),
            "courier": courier.pk,
            "period_start": "2026-10-01",
            "period_end": "2026-10-31",
        },
        format="multipart",
    )
    statement_id = resp.json()["id"]
    staff = User.objects.create_superuser("root", password="x")
    client.force_login(staff)
    detail = client.get(f"/admin/settlement/remittancestatement/{statement_id}/change/")
    assert detail.status_code == 200
    client.post(
        "/admin/settlement/remittancestatement/",
        {"action": "reprocess", "_selected_action": [statement_id]},
    )
    from apps.settlement.models import RemittanceStatement

    assert RemittanceStatement.objects.get(pk=statement_id).status == "reconciled"
    assert site.is_registered(RemittanceStatement)
