"""Free Online Training Library routes (PRD §3.10). Mounted at ``/api/v1/``."""

from rest_framework.routers import DefaultRouter

from library import views

app_name = "library"

router = DefaultRouter()
router.register(
    "library-resources", views.LibraryResourceViewSet, basename="library-resource"
)
router.register(
    "library-bookmarks", views.LibraryBookmarkViewSet, basename="library-bookmark"
)
router.register(
    "library-categories", views.LibraryCategoryViewSet, basename="library-category"
)

urlpatterns = router.urls
