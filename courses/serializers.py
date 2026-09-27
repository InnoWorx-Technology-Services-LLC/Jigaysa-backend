"""Serializers for the Course Management Module (PRD §3.2, §3.12).

Read and write shapes are split where they diverge: catalog cards stay light
(``CourseListSerializer``), the detail/authoring view nests structure, and a
dedicated write serializer keeps trainer-controlled fields server-side.
"""

from django.contrib.auth import get_user_model
from rest_framework import serializers

from payments import entitlements
from courses.models import (
    Batch,
    Category,
    Course,
    CourseReview,
    Enrollment,
    FeedbackAnswer,
    FeedbackForm,
    FeedbackQuestion,
    FeedbackResponse,
    Lesson,
    LessonNote,
    LessonProgress,
    LessonResource,
    Module,
    Tag,
)

User = get_user_model()


class TrainerMiniSerializer(serializers.ModelSerializer):
    """Compact trainer card embedded in course payloads."""

    class Meta:
        model = User
        fields = ("id", "full_name", "email")


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name", "slug", "parent", "icon")
        read_only_fields = ("slug",)


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ("id", "name", "slug")
        read_only_fields = ("slug",)


class LessonResourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = LessonResource
        fields = ("id", "lesson", "title", "url", "file", "resource_type")


class LessonSerializer(serializers.ModelSerializer):
    """Full lesson shape used by trainers when authoring."""

    resources = LessonResourceSerializer(many=True, read_only=True)

    class Meta:
        model = Lesson
        fields = (
            "id",
            "module",
            "title",
            "content_type",
            "order",
            "duration_minutes",
            "video_url",
            "video_key",
            "content",
            "is_preview",
            "assessment",
            "live_session",
            "resources",
        )


class LessonPlayerSerializer(serializers.ModelSerializer):
    """Student-facing lesson shape for the course player.

    Gated content (video/body) is hidden unless the requester may access it
    (preview lesson, enrolled, owner or admin). Per-lesson progress
    (``completed``/``watch_pct``/``last_position_seconds``) is folded in from the
    student's ``LessonProgress`` via a ``progress_map`` in context, so the player
    can render the completion ticks and resume point from one call. Private
    videos (``video_key``) are returned as a short-lived presigned URL.
    """

    locked = serializers.SerializerMethodField()
    video_url = serializers.SerializerMethodField()
    content = serializers.SerializerMethodField()
    resources = serializers.SerializerMethodField()
    completed = serializers.SerializerMethodField()
    watch_pct = serializers.SerializerMethodField()
    last_position_seconds = serializers.SerializerMethodField()

    class Meta:
        model = Lesson
        fields = (
            "id",
            "module",
            "title",
            "content_type",
            "order",
            "duration_minutes",
            "is_preview",
            "locked",
            "video_url",
            "content",
            "resources",
            "completed",
            "watch_pct",
            "last_position_seconds",
        )

    def _accessible(self, obj):
        if obj.is_preview:
            return True
        return bool(self.context.get("has_access"))

    def _progress(self, obj):
        return (self.context.get("progress_map") or {}).get(obj.id)

    def get_locked(self, obj):
        return not self._accessible(obj)

    def get_video_url(self, obj):
        """Playable URL: a presigned link for private ``video_key`` objects,
        else the stored ``video_url``. Empty when the lesson is locked."""
        if not self._accessible(obj):
            return ""
        if obj.video_key:
            try:
                from core import storage

                if storage.is_configured():
                    return storage.generate_presigned_download(obj.video_key)
            except Exception:
                pass  # fall back to the stored URL rather than 500
        return obj.video_url

    def get_content(self, obj):
        return obj.content if self._accessible(obj) else ""

    def get_resources(self, obj):
        if not self._accessible(obj):
            return []
        return LessonResourceSerializer(obj.resources.all(), many=True).data

    def get_completed(self, obj):
        p = self._progress(obj)
        return bool(p and p["completed"])

    def get_watch_pct(self, obj):
        p = self._progress(obj)
        return p["watch_pct"] if p else 0

    def get_last_position_seconds(self, obj):
        p = self._progress(obj)
        return p["last_position_seconds"] if p else 0


class ModuleSerializer(serializers.ModelSerializer):
    lessons = LessonSerializer(many=True, read_only=True)

    class Meta:
        model = Module
        fields = ("id", "course", "title", "summary", "order", "lessons")


class ModulePlayerSerializer(serializers.ModelSerializer):
    lessons = LessonPlayerSerializer(many=True, read_only=True)

    class Meta:
        model = Module
        fields = ("id", "title", "summary", "order", "lessons")


def public_media_url(stored_url, key):
    """Resolve an uploaded object to something a browser can load.

    Course cover art and the intro video are public marketing assets shown on
    the logged-out catalog, so they resolve to a plain CDN/bucket URL rather
    than a short-lived presigned link — an anonymous visitor has no token to
    call the presign endpoint with, and a signed URL would expire in the page.
    Private teaching content (``Lesson.video_key``) is still presigned.
    """
    if key:
        try:
            from core import storage

            if storage.is_configured():
                return storage.public_url(key)
        except Exception:
            pass  # fall through to whatever URL was stored
    return stored_url


class MediaUrlMixin:
    def get_thumbnail(self, obj):
        return public_media_url(obj.thumbnail, obj.thumbnail_key)

    def get_intro_video_url(self, obj):
        return public_media_url(obj.intro_video_url, obj.intro_video_key)


class CourseListSerializer(MediaUrlMixin, serializers.ModelSerializer):
    """Lightweight catalog card."""

    trainer = TrainerMiniSerializer(read_only=True)
    category = serializers.StringRelatedField()
    thumbnail = serializers.SerializerMethodField()

    class Meta:
        model = Course
        fields = (
            "id",
            "slug",
            "title",
            "subtitle",
            "trainer",
            "category",
            "course_type",
            "skill_level",
            "language",
            "duration_minutes",
            "thumbnail",
            "thumbnail_key",
            "thumbnail_color",
            "is_free",
            "status",
            "has_unapproved_changes",
            "rating_avg",
            "rating_count",
            "enrolled_count",
            "published_at",
        )


class CourseDetailSerializer(MediaUrlMixin, serializers.ModelSerializer):
    """Full course read shape with embedded taxonomy and trainer."""

    trainer = TrainerMiniSerializer(read_only=True)
    category = CategorySerializer(read_only=True)
    tags = TagSerializer(many=True, read_only=True)
    thumbnail = serializers.SerializerMethodField()
    intro_video_url = serializers.SerializerMethodField()
    module_count = serializers.IntegerField(
        source="modules.count", read_only=True
    )

    class Meta:
        model = Course
        fields = (
            "id",
            "slug",
            "title",
            "subtitle",
            "description",
            "trainer",
            "organization",
            "category",
            "tags",
            "course_type",
            "skill_level",
            "language",
            "duration_minutes",
            "thumbnail",
            "thumbnail_key",
            "thumbnail_color",
            "intro_video_url",
            "intro_video_key",
            "outcomes",
            "welcome_message",
            "completion_message",
            "certificate_enabled",
            "prerequisites",
            "status",
            "has_unapproved_changes",
            "review_note",
            "visibility",
            "is_free",
            "rating_avg",
            "rating_count",
            "enrolled_count",
            "module_count",
            "published_at",
            "created_at",
            "updated_at",
        )


class CourseWriteSerializer(serializers.ModelSerializer):
    """Create/update shape. ``trainer`` is taken from the request, not the body;
    ``status``/``published_at`` are controlled via the publish action."""

    category = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(), required=False, allow_null=True
    )
    tags = serializers.PrimaryKeyRelatedField(
        queryset=Tag.objects.all(), many=True, required=False
    )

    class Meta:
        model = Course
        fields = (
            "id",
            "title",
            "subtitle",
            "description",
            "organization",
            "category",
            "tags",
            "course_type",
            "skill_level",
            "language",
            "duration_minutes",
            "thumbnail",
            "thumbnail_key",
            "thumbnail_color",
            "intro_video_url",
            "intro_video_key",
            "outcomes",
            "welcome_message",
            "completion_message",
            "certificate_enabled",
            "prerequisites",
            "visibility",
            "is_free",
        )

    def create(self, validated_data):
        validated_data["trainer"] = self.context["request"].user
        return super().create(validated_data)


class BatchSerializer(serializers.ModelSerializer):
    trainer = TrainerMiniSerializer(read_only=True)
    trainer_id = serializers.PrimaryKeyRelatedField(
        source="trainer",
        queryset=User.objects.all(),
        write_only=True,
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Batch
        fields = (
            "id",
            "course",
            "name",
            "trainer",
            "trainer_id",
            "organization",
            "start_date",
            "end_date",
            "capacity",
            "enrolled_count",
            "schedule",
        )
        read_only_fields = ("enrolled_count",)


class EnrollmentSerializer(serializers.ModelSerializer):
    course = CourseListSerializer(read_only=True)

    class Meta:
        model = Enrollment
        fields = (
            "id",
            "student",
            "course",
            "batch",
            "status",
            "source",
            "order",
            "progress_pct",
            "enrolled_at",
            "completed_at",
        )
        read_only_fields = fields


class EnrollmentCreateSerializer(serializers.Serializer):
    """Self-enroll into a course.

    Free courses are open to anyone. A paid course needs either a purchase or a
    plan that includes all paid courses (PRD §3.4) — the subscriber's enrollment
    is tagged ``subscription`` so access can lapse with the plan.
    """

    course = serializers.PrimaryKeyRelatedField(queryset=Course.objects.all())
    batch = serializers.PrimaryKeyRelatedField(
        queryset=Batch.objects.all(), required=False, allow_null=True
    )

    def validate_course(self, course):
        if course.status != Course.Status.PUBLISHED:
            raise serializers.ValidationError("Course is not open for enrollment.")
        user = self.context["request"].user
        if Enrollment.objects.filter(student=user, course=course).exists():
            raise serializers.ValidationError("Already enrolled in this course.")
        if not course.is_free and not entitlements.can_access_paid_courses(user):
            raise serializers.ValidationError(
                "This is a paid course. Buy it via checkout "
                "(POST /api/v1/orders/ then /orders/{id}/pay/), or subscribe to "
                "a plan that includes all paid courses."
            )
        return course

    def enrollment_source(self, course):
        """Where the resulting enrollment came from, for the access rules."""
        if course.is_free:
            return Enrollment.Source.FREE
        return Enrollment.Source.SUBSCRIPTION


class LessonNoteSerializer(serializers.ModelSerializer):
    """The player's Notes tab. ``student`` comes from the request, never the body."""

    class Meta:
        model = LessonNote
        fields = ("id", "lesson", "body", "created_at", "updated_at")
        read_only_fields = ("created_at", "updated_at")


class LessonProgressSerializer(serializers.ModelSerializer):
    class Meta:
        model = LessonProgress
        fields = (
            "id",
            "enrollment",
            "lesson",
            "status",
            "watch_pct",
            "time_spent_seconds",
            "last_position_seconds",
            "completed_at",
        )
        read_only_fields = ("completed_at",)


class CourseReviewSerializer(serializers.ModelSerializer):
    student = TrainerMiniSerializer(read_only=True)

    class Meta:
        model = CourseReview
        fields = (
            "id",
            "course",
            "student",
            "rating",
            "comment",
            "created_at",
        )
        read_only_fields = ("student", "created_at")

    def validate_rating(self, value):
        if not 1 <= value <= 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return value


# --------------------------------------------------------------------------- #
# Course feedback form
# --------------------------------------------------------------------------- #


class FeedbackQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = FeedbackQuestion
        fields = (
            "id",
            "question_type",
            "text",
            "help_text",
            "is_required",
            "order",
            "options",
        )

    def validate(self, attrs):
        question_type = attrs.get(
            "question_type",
            getattr(self.instance, "question_type", FeedbackQuestion.QuestionType.RATING),
        )
        options = attrs.get("options", getattr(self.instance, "options", []))
        if question_type == FeedbackQuestion.QuestionType.CHOICE:
            if not isinstance(options, list) or len(options) < 2:
                raise serializers.ValidationError(
                    {"options": "A choice question needs at least two options."}
                )
            if any(not str(option).strip() for option in options):
                raise serializers.ValidationError(
                    {"options": "Options cannot be blank."}
                )
        elif options:
            raise serializers.ValidationError(
                {"options": f"A {question_type} question does not take options."}
            )
        return attrs


class FeedbackFormSerializer(serializers.ModelSerializer):
    """The form and its questions travel together.

    The builder screen saves the whole questionnaire in one go, and a form
    persisted without its questions is not a state worth allowing.
    """

    questions = FeedbackQuestionSerializer(many=True, required=False)
    response_count = serializers.SerializerMethodField()

    class Meta:
        model = FeedbackForm
        fields = (
            "id",
            "course",
            "title",
            "description",
            "is_active",
            "is_anonymous",
            "require_completion",
            "questions",
            "response_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")

    def get_response_count(self, obj):
        return obj.responses.filter(submitted_at__isnull=False).count()

    def _write_questions(self, form, questions):
        """Replace the question set wholesale — the builder always submits the
        full list, and answers cascade with the question they belong to."""
        form.questions.all().delete()
        FeedbackQuestion.objects.bulk_create(
            [
                FeedbackQuestion(
                    form=form,
                    order=question.pop("order", index),
                    **question,
                )
                for index, question in enumerate(questions)
            ]
        )

    def create(self, validated_data):
        questions = validated_data.pop("questions", [])
        form = FeedbackForm.objects.create(**validated_data)
        self._write_questions(form, questions)
        return form

    def update(self, instance, validated_data):
        questions = validated_data.pop("questions", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if questions is not None:
            self._write_questions(instance, questions)
        return instance


class FeedbackAnswerSerializer(serializers.ModelSerializer):
    class Meta:
        model = FeedbackAnswer
        fields = ("id", "question", "rating", "text")


class FeedbackResponseSerializer(serializers.ModelSerializer):
    """A student's submission, answers nested. ``student`` comes from the
    request, never the body."""

    answers = FeedbackAnswerSerializer(many=True)
    student = TrainerMiniSerializer(read_only=True)

    class Meta:
        model = FeedbackResponse
        fields = (
            "id",
            "form",
            "student",
            "answers",
            "submitted_at",
            "created_at",
        )
        read_only_fields = ("student", "submitted_at", "created_at")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.form.is_anonymous:
            data["student"] = None
        return data

    def validate(self, attrs):
        form = attrs.get("form") or self.instance.form
        answers = attrs.get("answers", [])
        questions = {q.id: q for q in form.questions.all()}

        seen = set()
        for answer in answers:
            question = answer["question"]
            if question.id not in questions:
                raise serializers.ValidationError(
                    {"answers": f"Question {question.id} is not on this form."}
                )
            if question.id in seen:
                raise serializers.ValidationError(
                    {"answers": f"Question {question.id} answered twice."}
                )
            seen.add(question.id)
            self._validate_answer(question, answer)

        missing = [
            q.id for q in questions.values() if q.is_required and q.id not in seen
        ]
        if missing:
            raise serializers.ValidationError(
                {"answers": f"Required questions unanswered: {missing}."}
            )
        return attrs

    def _validate_answer(self, question, answer):
        rating = answer.get("rating")
        text = (answer.get("text") or "").strip()
        label = f"Question {question.id}"

        if question.question_type in FeedbackQuestion.NUMERIC_TYPES:
            ceiling = 5 if question.question_type == "rating" else 10
            if rating is None:
                raise serializers.ValidationError(
                    {"answers": f"{label} needs a rating."}
                )
            if not 1 <= rating <= ceiling:
                raise serializers.ValidationError(
                    {"answers": f"{label} must be between 1 and {ceiling}."}
                )
            return

        if rating is not None:
            raise serializers.ValidationError(
                {"answers": f"{label} does not take a rating."}
            )
        if question.is_required and not text:
            raise serializers.ValidationError({"answers": f"{label} needs an answer."})
        if question.question_type == FeedbackQuestion.QuestionType.CHOICE and text:
            if text not in question.options:
                raise serializers.ValidationError(
                    {"answers": f"{label}: '{text}' is not one of the options."}
                )
        if question.question_type == FeedbackQuestion.QuestionType.YES_NO and text:
            if text.lower() not in ("yes", "no"):
                raise serializers.ValidationError(
                    {"answers": f"{label} must be 'yes' or 'no'."}
                )

    def create(self, validated_data):
        answers = validated_data.pop("answers")
        response = FeedbackResponse.objects.create(**validated_data)
        FeedbackAnswer.objects.bulk_create(
            [FeedbackAnswer(response=response, **answer) for answer in answers]
        )
        return response
