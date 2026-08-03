"""Discussion forum, community & gamification routes (PRD §3.12).

Mounted at ``/api/v1/``.
"""

from rest_framework.routers import DefaultRouter

from engagement import views

app_name = "engagement"

router = DefaultRouter()
router.register(
    "discussion-threads", views.DiscussionThreadViewSet, basename="discussion-thread"
)
router.register(
    "discussion-replies", views.DiscussionReplyViewSet, basename="discussion-reply"
)
router.register("community-posts", views.CommunityPostViewSet, basename="community-post")
router.register("forum-tags", views.ForumTagViewSet, basename="forum-tag")
router.register("badges", views.BadgeViewSet, basename="badge")
router.register(
    "community-profile", views.CommunityProfileViewSet, basename="community-profile"
)

urlpatterns = router.urls
