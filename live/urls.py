"""Live training, 1:1 & group session routes (PRD §3.5, §3.6).

Mounted at ``/api/v1/``.
"""

from rest_framework.routers import DefaultRouter

from live import views

app_name = "live"

router = DefaultRouter()
router.register("live-sessions", views.LiveSessionViewSet, basename="live-session")
router.register(
    "session-registrations",
    views.SessionRegistrationViewSet,
    basename="session-registration",
)
router.register("session-doubts", views.SessionDoubtViewSet, basename="session-doubt")
router.register(
    "trainer-availability",
    views.TrainerAvailabilityViewSet,
    basename="trainer-availability",
)
router.register(
    "individual-bookings",
    views.IndividualBookingViewSet,
    basename="individual-booking",
)
router.register("mentors", views.MentorViewSet, basename="mentor")

urlpatterns = router.urls
