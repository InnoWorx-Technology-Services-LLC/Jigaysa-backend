"""Course catalog, structure, enrollment and progress (PRD §3.2, §3.12).

Models are role-agnostic: the same ``Course`` row backs a trainer's authoring
screen and a student's catalog card. ``Category`` and ``Tag`` live here and are
imported across the platform (library, profiles) via lazy FK strings.
"""

from django.conf import settings
from django.db import models
from django.utils.text import slugify

from core.models import TimeStampedModel


class Category(TimeStampedModel):
    """Course / library taxonomy, self-nesting (PRD §3.2 tags/category)."""

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    icon = models.CharField(max_length=64, blank=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class Tag(TimeStampedModel):
    name = models.CharField(max_length=60)
    slug = models.SlugField(max_length=80, unique=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class Course(TimeStampedModel):
    """A course of any delivery type (PRD §3.2 course types & attributes)."""

    class CourseType(models.TextChoices):
        SELF_PACED = "self_paced", "Self-paced online"
        LIVE_BATCH = "live_batch", "Live training batch"
        PHYSICAL = "physical", "Physical classroom"
        HYBRID = "hybrid", "Hybrid"
        INDIVIDUAL = "individual_coaching", "Individual coaching"
        GROUP = "group_coaching", "Group coaching"

    class SkillLevel(models.TextChoices):
        BEGINNER = "beginner", "Beginner"
        INTERMEDIATE = "intermediate", "Intermediate"
        ADVANCED = "advanced", "Advanced"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_REVIEW = "pending_review", "Pending review"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    class Visibility(models.TextChoices):
        PUBLIC = "public", "Public"
        UNLISTED = "unlisted", "Unlisted"
        PRIVATE = "private", "Private"

    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=280, unique=True, blank=True)
    subtitle = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    trainer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="courses_taught",
    )
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="courses",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="courses",
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name="courses")
    course_type = models.CharField(
        max_length=20, choices=CourseType.choices, default=CourseType.SELF_PACED
    )
    skill_level = models.CharField(
        max_length=20, choices=SkillLevel.choices, default=SkillLevel.BEGINNER
    )
    language = models.CharField(max_length=20, default="en")
    duration_minutes = models.PositiveIntegerField(default=0)
    thumbnail = models.URLField(blank=True)
    intro_video_url = models.URLField(blank=True)
    prerequisites = models.ManyToManyField(
        "self", blank=True, symmetrical=False, related_name="required_for"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    visibility = models.CharField(
        max_length=20, choices=Visibility.choices, default=Visibility.PUBLIC
    )
    is_free = models.BooleanField(default=False)
    rating_avg = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    rating_count = models.PositiveIntegerField(default=0)
    enrolled_count = models.PositiveIntegerField(default=0)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)
        super().save(*args, **kwargs)


class Module(TimeStampedModel):
    """A section grouping lessons inside a course (course-player curriculum)."""

    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="modules"
    )
    title = models.CharField(max_length=255)
    summary = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.course.title} / {self.title}"


class Lesson(TimeStampedModel):
    """A single learning unit (PRD §3.12). Quiz/assignment lessons link to an
    Assessment; live lessons link to a LiveSession (lazy FK strings)."""

    class ContentType(models.TextChoices):
        VIDEO = "video", "Video"
        READING = "reading", "Reading"
        QUIZ = "quiz", "Quiz"
        ASSIGNMENT = "assignment", "Assignment"
        LIVE = "live", "Live"

    module = models.ForeignKey(
        Module, on_delete=models.CASCADE, related_name="lessons"
    )
    title = models.CharField(max_length=255)
    content_type = models.CharField(
        max_length=20, choices=ContentType.choices, default=ContentType.VIDEO
    )
    order = models.PositiveIntegerField(default=0)
    duration_minutes = models.PositiveIntegerField(default=0)
    video_url = models.URLField(blank=True)
    # S3/R2 object key for a privately-stored lesson video. When set, the player
    # serves a short-lived presigned URL instead of ``video_url`` (see
    # core.storage). Upload via POST /api/v1/uploads/presign/ (purpose lesson_video).
    video_key = models.CharField(max_length=1024, blank=True)
    content = models.TextField(blank=True)
    is_preview = models.BooleanField(default=False)
    assessment = models.ForeignKey(
        "assessments.Assessment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lessons",
    )
    live_session = models.ForeignKey(
        "live.LiveSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lessons",
    )

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.title


class LessonResource(TimeStampedModel):
    """Downloadable / linked material on a lesson (player "Resources" tab)."""

    lesson = models.ForeignKey(
        Lesson, on_delete=models.CASCADE, related_name="resources"
    )
    title = models.CharField(max_length=255)
    url = models.URLField(blank=True)
    file = models.FileField(upload_to="lesson_resources/", blank=True)
    resource_type = models.CharField(max_length=40, blank=True)

    def __str__(self):
        return self.title


class Batch(TimeStampedModel):
    """A cohort/batch of a live or institution course (PRD §2.4, §3.6)."""

    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="batches"
    )
    name = models.CharField(max_length=255)
    trainer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="batches_led",
    )
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="batches",
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    capacity = models.PositiveIntegerField(default=0)
    enrolled_count = models.PositiveIntegerField(default=0)
    schedule = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name_plural = "batches"
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.course.title} — {self.name}"


class Enrollment(TimeStampedModel):
    """A student's enrollment in a course (PRD §2.3, §2.4 bulk enroll)."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    class Source(models.TextChoices):
        PURCHASE = "purchase", "Purchase"
        FREE = "free", "Free"
        BULK = "bulk", "Bulk (institution)"
        INSTITUTION = "institution", "Institution"

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="enrollments",
    )
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="enrollments"
    )
    batch = models.ForeignKey(
        Batch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="enrollments",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.FREE
    )
    order = models.ForeignKey(
        "payments.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="enrollments",
    )
    progress_pct = models.PositiveIntegerField(default=0)  # 0-100
    enrolled_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-enrolled_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "course"], name="unique_student_course"
            )
        ]

    def __str__(self):
        return f"{self.student} → {self.course}"


class LessonProgress(TimeStampedModel):
    """Per-lesson progress for a student (PRD §3.12 resume learning)."""

    class Status(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"

    enrollment = models.ForeignKey(
        Enrollment, on_delete=models.CASCADE, related_name="lesson_progress"
    )
    lesson = models.ForeignKey(
        Lesson, on_delete=models.CASCADE, related_name="progress_records"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.NOT_STARTED
    )
    watch_pct = models.PositiveIntegerField(default=0)  # 0-100
    time_spent_seconds = models.PositiveIntegerField(default=0)
    last_position_seconds = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "lesson progress"
        constraints = [
            models.UniqueConstraint(
                fields=["enrollment", "lesson"], name="unique_enrollment_lesson"
            )
        ]

    def __str__(self):
        return f"{self.enrollment} · {self.lesson} [{self.status}]"


class CourseReview(TimeStampedModel):
    """Student rating + review of a course (feeds Course.rating_avg)."""

    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="reviews"
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="course_reviews",
    )
    rating = models.PositiveSmallIntegerField(default=5)  # 1-5
    comment = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "student"], name="unique_course_review"
            )
        ]

    def __str__(self):
        return f"{self.student} rated {self.course} ({self.rating})"
