import pytest
from rest_framework.test import APIClient

from config.celery import app as celery_app

from .factories import CourierFactory, MerchantFactory, StaffFactory, UserFactory


@pytest.fixture(autouse=True)
def _test_settings(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    # Fast hashing for tests; production keeps Django's default PBKDF2.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture(autouse=True)
def _celery_eager():
    """Run Celery tasks inline so tests need no broker."""
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield
    celery_app.conf.task_always_eager = False


@pytest.fixture
def merchant(db):
    return MerchantFactory(name="Atlas Home", slug="atlas")


@pytest.fixture
def other_merchant(db):
    return MerchantFactory(name="Medina Style", slug="medina")


@pytest.fixture
def courier(db):
    return CourierFactory(name="Speedex", code="speedex")


@pytest.fixture
def merchant_user(merchant):
    return UserFactory(username="atlas", merchant=merchant)


@pytest.fixture
def other_user(other_merchant):
    return UserFactory(username="medina", merchant=other_merchant)


@pytest.fixture
def staff_user(db):
    return StaffFactory(username="ops")


def _client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def api(merchant_user):
    """API client authenticated as the main merchant's user."""
    return _client_for(merchant_user)


@pytest.fixture
def other_api(other_user):
    return _client_for(other_user)


@pytest.fixture
def staff_api(staff_user):
    return _client_for(staff_user)


@pytest.fixture
def anon_api():
    return APIClient()
