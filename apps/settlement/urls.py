from rest_framework.routers import DefaultRouter

from .views import DisputeViewSet, StatementViewSet

router = DefaultRouter()
router.register("statements", StatementViewSet, basename="statement")
router.register("disputes", DisputeViewSet, basename="dispute")

urlpatterns = router.urls
