"""A merchant must never read or write another merchant's data."""

from decimal import Decimal

import pytest

from apps.logistics.models import Order

from .factories import OrderFactory, ResultFactory, StatementFactory, UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def foreign_data(other_merchant, courier):
    order = OrderFactory(merchant=other_merchant, courier=courier)
    statement = StatementFactory(merchant=other_merchant, courier=courier)
    result = ResultFactory(statement=statement)
    return {"order": order, "statement": statement, "result": result}


def test_unauthenticated_requests_are_rejected(anon_api):
    for url in ["/api/orders/", "/api/couriers/", "/api/statements/", "/api/disputes/"]:
        assert anon_api.get(url).status_code == 401


def test_user_without_merchant_is_forbidden(db):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(UserFactory(merchant=None))
    assert client.get("/api/orders/").status_code == 403


def test_list_endpoints_only_show_own_data(api, merchant, courier, foreign_data):
    mine = OrderFactory(merchant=merchant, courier=courier)
    body = api.get("/api/orders/").json()
    assert [o["id"] for o in body["results"]] == [mine.id]
    assert api.get("/api/statements/").json()["count"] == 0
    assert api.get("/api/disputes/").json()["count"] == 0


@pytest.mark.parametrize(
    "url",
    [
        "/api/orders/{order}/",
        "/api/statements/{statement}/",
        "/api/statements/{statement}/reconciliation/",
    ],
)
def test_foreign_objects_return_404(api, foreign_data, url):
    ids = {k: v.pk for k, v in foreign_data.items()}
    assert api.get(url.format(**ids)).status_code == 404


def test_cannot_modify_foreign_order(api, foreign_data):
    order = foreign_data["order"]
    assert api.patch(f"/api/orders/{order.pk}/", {"city": "X"}).status_code == 404
    assert api.delete(f"/api/orders/{order.pk}/").status_code == 404
    assert api.post(f"/api/orders/{order.pk}/cancel/").status_code == 404
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING


def test_cannot_create_order_for_another_merchant(api, merchant, other_merchant, courier):
    resp = api.post(
        "/api/orders/",
        {
            "merchant": other_merchant.pk,
            "courier": courier.pk,
            "tracking_number": "x1",
            "customer_name": "C",
            "amount_due": "10.00",
        },
    )
    assert resp.status_code == 201
    assert Order.objects.get(pk=resp.json()["id"]).merchant == merchant  # forced to own


def test_cannot_dispute_foreign_result(api, foreign_data):
    resp = api.post("/api/disputes/", {"result": foreign_data["result"].pk, "reason": "steal"})
    assert resp.status_code == 400
    assert "result" in resp.json()


def test_balance_is_scoped_to_callers_merchant(api, merchant, other_merchant, courier):
    OrderFactory(merchant=merchant, courier=courier, delivered=True, amount_due=Decimal("10.00"))
    OrderFactory(merchant=other_merchant, courier=courier, delivered=True)
    body = api.get(f"/api/couriers/{courier.pk}/balance/").json()
    assert body["merchant"] == merchant.pk
    assert body["expected_collections"] == "10.00"


def test_staff_sees_all_merchants(staff_api, merchant, courier, foreign_data):
    OrderFactory(merchant=merchant, courier=courier)
    assert staff_api.get("/api/orders/").json()["count"] == 2
    assert staff_api.get("/api/statements/").json()["count"] == 1
