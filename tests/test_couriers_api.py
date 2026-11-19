import pytest

from .factories import CourierFactory, OrderFactory

pytestmark = pytest.mark.django_db


def test_merchants_can_read_couriers(api, courier):
    CourierFactory(name="Rapido", code="rapido", is_active=False)
    assert api.get("/api/couriers/").json()["count"] == 2
    assert api.get("/api/couriers/?is_active=true").json()["count"] == 1
    assert api.get("/api/couriers/?search=speed").json()["results"][0]["code"] == "speedex"


def test_only_staff_can_write_couriers(api, staff_api, courier):
    data = {"name": "New Courier", "code": "new"}
    assert api.post("/api/couriers/", data).status_code == 403
    assert api.patch(f"/api/couriers/{courier.pk}/", {"name": "x"}).status_code == 403
    assert staff_api.post("/api/couriers/", data).status_code == 201


def test_courier_with_orders_cannot_be_deleted(staff_api, merchant, courier):
    OrderFactory(merchant=merchant, courier=courier)
    assert staff_api.delete(f"/api/couriers/{courier.pk}/").status_code == 400
    empty = CourierFactory()
    assert staff_api.delete(f"/api/couriers/{empty.pk}/").status_code == 204


def test_staff_balance_for_one_merchant_or_all(staff_api, merchant, other_merchant, courier):
    OrderFactory(merchant=merchant, courier=courier, delivered=True)
    OrderFactory(merchant=other_merchant, courier=courier, delivered=True)
    url = f"/api/couriers/{courier.pk}/balance/"

    assert staff_api.get(url).json()["expected_collections"] == "200.00"
    one = staff_api.get(f"{url}?merchant={merchant.pk}").json()
    assert one["expected_collections"] == "100.00"
    assert one["merchant"] == merchant.pk
    assert staff_api.get(f"{url}?merchant=999999").status_code == 400
