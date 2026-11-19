from rest_framework.routers import DefaultRouter

from .views import CourierViewSet, OrderViewSet

router = DefaultRouter()
router.register("couriers", CourierViewSet, basename="courier")
router.register("orders", OrderViewSet, basename="order")

urlpatterns = router.urls
