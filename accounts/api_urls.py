"""Account resources that belong at ``/api/v1/``, not under ``/auth/``.

``accounts.urls`` is mounted at ``/api/v1/auth/`` and is all login/OTP/reset
plumbing. Trainer profiles are an ordinary resource, so they live here to avoid
the nonsensical ``/auth/trainer-profiles/`` path.
"""

from rest_framework.routers import DefaultRouter

from accounts import views

app_name = "accounts_api"

router = DefaultRouter()
router.register(
    "trainer-profiles", views.TrainerProfileViewSet, basename="trainer-profile"
)

urlpatterns = router.urls
