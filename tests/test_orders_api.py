from decimal import Decimal

import pytest

from apps.logistics.models import Order

from .factories import CourierFactory, OrderFactory

pytestmark = pytest.mark.django_db


def payload(courier, **overrides):
    data = {
        "courier": courier.pk,
        "tracking_number": " spx-001 ",
        "customer_name": "Salma Tazi",
        "city": "Rabat",
        "amount_due": "349.90",
    }
    data.update(overrides)
    return data


def test_create_order(api, merchant, courier):
    resp = api.post("/api/orders/", payload(courier), format="json")
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert body["tracking_number"] == "SPX-001"
    assert body["amount_due"] == "349.90"  # money serialized as string, never float
    assert body["currency"] == "MAD"
    assert body["status"] == "pending"
    assert body["merchant"] == merchant.pk


def test_status_is_read_only_on_create(api, courier):
    resp = api.post("/api/orders/", payload(courier, status="delivered"), format="json")
    assert resp.json()["status"] == "pending"


@pytest.mark.parametrize(
    ("field", "value"),
    [("amount_due", "-1.00"), ("amount_due", "10.999"), ("amount_due", "abc"), ("currency", "EUR")],
)
def test_create_order_validation(api, courier, field, value):
    resp = api.post("/api/orders/", payload(courier, **{field: value}), format="json")
    assert resp.status_code == 400
    assert field in resp.json()


def test_tracking_number_unique_per_courier(api, merchant, courier):
    OrderFactory(merchant=merchant, courier=courier, tracking_number="SPX-001")
    resp = api.post("/api/orders/", payload(courier), format="json")
    assert resp.status_code == 400
    assert "tracking_number" in resp.json()
    # ... but the same number is fine at another courier
    other = CourierFactory()
    assert api.post("/api/orders/", payload(other), format="json").status_code == 201


def test_update_keeps_uniqueness_check(api, merchant, courier):
    OrderFactory(merchant=merchant, courier=courier, tracking_number="A")
    order = OrderFactory(merchant=merchant, courier=courier, tracking_number="B")
    assert api.patch(f"/api/orders/{order.pk}/", {"tracking_number": "a"}).status_code == 400
    assert api.patch(f"/api/orders/{order.pk}/", {"city": "Fes"}).status_code == 200


def test_staff_must_choose_merchant(staff_api, merchant, courier):
    assert staff_api.post("/api/orders/", payload(courier), format="json").status_code == 400
    resp = staff_api.post("/api/orders/", payload(courier, merchant=merchant.pk), format="json")
    assert resp.status_code == 201


def test_filtering_search_ordering_and_pagination(api, merchant, courier):
    OrderFactory(merchant=merchant, courier=courier, city="Rabat", amount_due=Decimal("50.00"))
    OrderFactory(merchant=merchant, courier=courier, delivered=True, amount_due=Decimal("500.00"))
    OrderFactory.create_batch(25, merchant=merchant, courier=courier, customer_name="Bulk")
    target = OrderFactory(merchant=merchant, courier=courier, customer_name="Khadija Fassi")

    page = api.get("/api/orders/").json()
    assert page["count"] == 28
    assert len(page["results"]) == 20 and page["next"]

    assert api.get("/api/orders/?status=delivered").json()["count"] == 1
    assert api.get("/api/orders/?status=delivered&status=pending").json()["count"] == 28
    assert api.get("/api/orders/?min_amount=400").json()["count"] == 1
    assert api.get("/api/orders/?max_amount=60").json()["count"] == 1
    search = api.get("/api/orders/?search=khadija").json()
    assert [o["id"] for o in search["results"]] == [target.pk]
    first = api.get("/api/orders/?ordering=-amount_due").json()["results"][0]
    assert first["amount_due"] == "500.00"


def test_record_delivery_attempt_moves_status(api, merchant, courier):
    order = OrderFactory(merchant=merchant, courier=courier)
    url = f"/api/orders/{order.pk}/attempts/"

    resp = api.post(url, {"outcome": "no_answer"}, format="json")
    assert resp.status_code == 201
    assert resp.json()["status"] == "shipped"

    resp = api.post(url, {"outcome": "delivered", "note": "paid cash"}, format="json")
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["delivered_at"] is not None
    assert [a["outcome"] for a in body["attempts"]] == ["no_answer", "delivered"]

    # final state: further attempts are rejected
    assert api.post(url, {"outcome": "refused"}, format="json").status_code == 400


def test_refused_attempt_returns_order(api, merchant, courier):
    order = OrderFactory(merchant=merchant, courier=courier)
    api.post(f"/api/orders/{order.pk}/attempts/", {"outcome": "refused"}, format="json")
    order.refresh_from_db()
    assert order.status == Order.Status.RETURNED


def test_cancel_and_delete_rules(api, merchant, courier):
    pending = OrderFactory(merchant=merchant, courier=courier)
    delivered = OrderFactory(merchant=merchant, courier=courier, delivered=True)

    assert api.post(f"/api/orders/{delivered.pk}/cancel/").status_code == 400
    assert api.delete(f"/api/orders/{delivered.pk}/").status_code == 400
    assert api.post(f"/api/orders/{pending.pk}/cancel/").json()["status"] == "cancelled"
    fresh = OrderFactory(merchant=merchant, courier=courier)
    assert api.delete(f"/api/orders/{fresh.pk}/").status_code == 204
