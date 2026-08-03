"""Course Management Module routes (PRD §3.2, §3.12).

Mounted at ``/api/v1/courses/`` by the project urlconf. A single DRF router
exposes the catalog, authoring tree and student endpoints.
"""

from rest_framework.routers import DefaultRouter

from courses import views

app_name = "courses"

router = DefaultRouter()
router.register("categories", views.CategoryViewSet, basename="category")
router.register("tags", views.TagViewSet, basename="tag")
router.register("courses", views.CourseViewSet, basename="course")
router.register("modules", views.ModuleViewSet, basename="module")
router.register("lessons", views.LessonViewSet, basename="lesson")
router.register(
    "lesson-resources", views.LessonResourceViewSet, basename="lesson-resource"
)
router.register("batches", views.BatchViewSet, basename="batch")
router.register("enrollments", views.EnrollmentViewSet, basename="enrollment")
router.register(
    "lesson-progress", views.LessonProgressViewSet, basename="lesson-progress"
)
router.register("lesson-notes", views.LessonNoteViewSet, basename="lesson-note")
router.register("reviews", views.CourseReviewViewSet, basename="review")

urlpatterns = router.urls
