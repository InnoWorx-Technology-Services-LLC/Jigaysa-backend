"""Assessments & assignments API (PRD §3.12).

Students browse published assessments and attempt them in a single submit call;
auto-gradable questions (MCQ / multi-select) are scored immediately, while
descriptive/coding/file answers are held for trainer review. Trainers author
assessments and manually grade the held submissions. Answer keys are never
exposed to students (see ``ChoiceSerializer``).
"""

from django.db import transaction
from django.db.models import Avg, Count, Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from assessments.models import (
    Answer,
    Assessment,
    Question,
    Rubric,
    Submission,
)
from assessments.serializers import (
    AssessmentBoardSerializer,
    AssessmentDetailSerializer,
    AssessmentSerializer,
    AssignmentStatsSerializer,
    GradeSerializer,
    QuestionAuthorSerializer,
    QuestionBulkSerializer,
    RubricSerializer,
    SubmissionSerializer,
    SubmitSerializer,
)
from courses.models import Enrollment

ALL_ROLES = ("student", "trainer", "admin", "institution")
TRAINER_WRITE = ("trainer", "admin")
AUTO_GRADED_TYPES = {Question.QuestionType.MCQ, Question.QuestionType.MULTI}


def _is_admin(user):
    return getattr(user, "role", None) == "admin"


def _is_trainer_role(user):
    return getattr(user, "role", None) in TRAINER_WRITE


def _filter_by(qs, request, param, field=None):
    value = request.query_params.get(param)
    if value:
        qs = qs.filter(**{field or param: value})
    return qs


def _clean_criteria(criteria):
    """Validate the rubric's shape before it is stored.

    ``criteria`` is a JSONField, so nothing at the database layer stops a
    malformed list — and a rubric that grades against ``{"nmae": ...}`` fails
    silently at marking time, which is the worst moment to find out.
    """
    if not isinstance(criteria, list):
        raise ValidationError("criteria must be a list.")
    cleaned = []
    for i, row in enumerate(criteria, start=1):
        if not isinstance(row, dict):
            raise ValidationError(f"Criterion {i} must be an object.")
        name = str(row.get("name", "")).strip()
        if not name:
            raise ValidationError(f"Criterion {i} needs a name.")
        try:
            points = float(row.get("max_points", 0))
        except (TypeError, ValueError):
            raise ValidationError(f"Criterion {i}: max_points must be a number.")
        if points <= 0:
            raise ValidationError(
                f"Criterion {i}: max_points must be greater than zero."
            )
        cleaned.append({"name": name[:255], "max_points": points})
    return cleaned


def _rubric_total(assessment):
    """Total points a rubric allows, or ``None`` when it does not apply."""
    if assessment.grading_type != Assessment.GradingType.RUBRIC:
        return None
    rubric = getattr(assessment, "rubric", None)
    if rubric is None:
        return 0
    return sum(float(c.get("max_points", 0)) for c in (rubric.criteria or []))


class AssessmentViewSet(viewsets.ModelViewSet):
    """Quizzes/assignments. Filters: ``?course=<id>``, ``?lesson=<id>``,
    ``?assessment_type=quiz|assignment|coding|descriptive``. Students only see
    published assessments; trainers see their own (draft or published). Authoring
    is limited to the owning trainer or an admin."""

    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES
    api_roles_by_action = {
        "create": TRAINER_WRITE,
        "update": TRAINER_WRITE,
        "partial_update": TRAINER_WRITE,
        "destroy": TRAINER_WRITE,
        "questions": TRAINER_WRITE,
        "rubric": TRAINER_WRITE,
        "board": TRAINER_WRITE,
        "stats": TRAINER_WRITE,
        "submit": ("student",),
    }

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AssessmentDetailSerializer
        if self.action == "board":
            return AssessmentBoardSerializer
        return AssessmentSerializer

    def get_queryset(self):
        user = self.request.user
        qs = Assessment.objects.select_related("course", "lesson", "trainer")
        if not _is_admin(user):
            if _is_trainer_role(user):
                qs = qs.filter(Q(is_published=True) | Q(trainer=user))
            else:
                qs = qs.filter(is_published=True)

        # ``?mine=true`` narrows to what this trainer authored. Needed because
        # the default deliberately includes every *published* assessment — good
        # for browsing, wrong for a page called "your assignments", which would
        # otherwise list colleagues' work with an Edit button beside it.
        if self.request.query_params.get("mine", "").lower() in ("1", "true", "yes"):
            qs = qs.filter(trainer=user)

        qs = _filter_by(qs, self.request, "course", "course_id")
        qs = _filter_by(qs, self.request, "lesson", "lesson_id")
        qs = _filter_by(qs, self.request, "assessment_type")
        return qs

    def _with_counts(self, queryset):
        """Annotate the three per-row numbers the Assignments table shows.

        One query for all of them. ``distinct=True`` on each because joining
        submissions and enrollments in the same statement multiplies the rows —
        without it a course with 30 students reports 30× the submissions.
        """
        return queryset.annotate(
            submitted_count=Count(
                "submissions",
                filter=Q(
                    submissions__status__in=(
                        Submission.Status.SUBMITTED,
                        Submission.Status.GRADED,
                        Submission.Status.PASSED,
                        Submission.Status.FAILED,
                    )
                ),
                distinct=True,
            ),
            pending_review_count=Count(
                "submissions",
                filter=Q(submissions__status=Submission.Status.SUBMITTED),
                distinct=True,
            ),
            enrolled_count=Count(
                "course__enrollments",
                filter=Q(course__enrollments__status=Enrollment.Status.ACTIVE),
                distinct=True,
            ),
        )

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "request": self.request}

    def perform_create(self, serializer):
        if not _is_trainer_role(self.request.user):
            raise PermissionDenied("Only trainers can create assessments.")
        serializer.save(trainer=self.request.user)

    def _assert_owner(self, assessment):
        user = self.request.user
        if assessment.trainer_id != user.id and not _is_admin(user):
            raise PermissionDenied("You do not own this assessment.")

    def perform_update(self, serializer):
        self._assert_owner(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._assert_owner(instance)
        instance.delete()

    @extend_schema(request=RubricSerializer, responses=RubricSerializer)
    @action(detail=True, methods=["get", "put"], url_path="rubric")
    def rubric(self, request, pk=None):
        """The grading rubric for an assessment.

        ``grading_type: "rubric"`` has been a selectable value since the first
        migration with no way to reach the ``Rubric`` model behind it, so it
        graded exactly like ``manual``. This is the missing half.

        ``criteria`` is a list of ``{"name": str, "max_points": number}``.
        ``PUT`` replaces the whole list — a rubric is edited as one thing, and
        per-criterion patching would leave the editor reconciling deletes.
        """
        assessment = self.get_object()
        rubric, _ = Rubric.objects.get_or_create(assessment=assessment)

        if request.method == "GET":
            return Response(RubricSerializer(rubric).data)

        self._assert_owner(assessment)
        body = RubricSerializer(rubric, data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        criteria = _clean_criteria(body.validated_data.get("criteria", []))
        rubric.criteria = criteria
        rubric.save(update_fields=["criteria", "updated_at"])
        return Response(RubricSerializer(rubric).data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "assessment_type", str, OpenApiParameter.QUERY,
                description="quiz | assignment | coding | descriptive",
            ),
            OpenApiParameter("course", int, OpenApiParameter.QUERY),
        ],
        responses=AssessmentBoardSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def board(self, request):
        """GET ``/assessments/board/`` — the trainer's Assignments list.

        The plain list with three counts annotated on: how many students have
        handed in, how many of those are waiting on this trainer, and how many
        are enrolled. Paginated, and always scoped to the caller's own work —
        a page with an Edit button beside every row has no business showing a
        colleague's assignment.
        """
        # ``api_roles_by_action`` is schema metadata in this project, not an
        # enforcement point (see core.schema) — the role check has to be here,
        # the same way ``perform_create`` does it.
        if not _is_trainer_role(request.user):
            raise PermissionDenied("Only trainers have an assignments board.")

        queryset = self._with_counts(
            self.get_queryset().filter(trainer=request.user)
        ).order_by("-created_at")
        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(
                self.get_serializer(page, many=True).data
            )
        return Response(self.get_serializer(queryset, many=True).data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "assessment_type", str, OpenApiParameter.QUERY,
                description=(
                    "Narrow the tiles to one type, e.g. `assignment`. Pass the "
                    "same value the board is showing or the two will disagree."
                ),
            )
        ],
        responses=AssignmentStatsSerializer,
    )
    @action(detail=False, methods=["get"])
    def stats(self, request):
        """GET ``/assessments/stats/`` — the three tiles.

        Scoped to the caller's own assessments. ``average_score`` is ``null``,
        never ``0``, when nothing has been graded yet — a brand-new trainer has
        no average, which is not the same as an average of zero.
        """
        if not _is_trainer_role(request.user):
            raise PermissionDenied("Only trainers have assignment statistics.")

        mine = Assessment.objects.filter(trainer=request.user)
        # Honour the same ``?assessment_type=`` the board is called with, so the
        # tile and the list beneath it count the same things. Without it the
        # tile silently included quizzes and coding tests while the list showed
        # assignments only, and the two disagreed for no visible reason.
        mine = _filter_by(mine, request, "assessment_type")
        now = timezone.now()

        open_count = mine.filter(is_published=True).filter(
            Q(available_to__isnull=True) | Q(available_to__gte=now)
        ).count()
        pending = Submission.objects.filter(
            assessment__in=mine, status=Submission.Status.SUBMITTED
        ).count()
        average = Submission.objects.filter(
            assessment__in=mine,
            status__in=(
                Submission.Status.GRADED,
                Submission.Status.PASSED,
                Submission.Status.FAILED,
            ),
        ).aggregate(avg=Avg("percent"))["avg"]

        return Response(
            AssignmentStatsSerializer(
                {
                    "open_assignments": open_count,
                    "pending_reviews": pending,
                    "average_score": round(average, 1) if average is not None else None,
                }
            ).data
        )

    @action(detail=True, methods=["get", "post"], url_path="questions")
    def questions(self, request, pk=None):
        """Author the question set — the editor's "Save questions" button.

        ``GET`` returns the questions **with the answer key**, which is why it
        is trainer-only. ``POST`` replaces the whole set in one call, so the
        editor never has to reconcile per-question creates, updates and
        deletes against what the server already had.
        """
        assessment = self.get_object()
        self._assert_owner(assessment)

        if request.method == "GET":
            return Response(
                QuestionAuthorSerializer(
                    assessment.questions.prefetch_related("choices"), many=True
                ).data
            )

        payload = QuestionBulkSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        with transaction.atomic():
            assessment.questions.all().delete()
            for order, question_data in enumerate(
                payload.validated_data["questions"]
            ):
                question_data.setdefault("order", order)
                question_data["assessment"] = assessment
                QuestionAuthorSerializer().create(question_data)
            assessment.refresh_from_db()
        return Response(
            QuestionAuthorSerializer(
                assessment.questions.prefetch_related("choices"), many=True
            ).data,
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        """Attempt this assessment in one call. Auto-grades objective questions;
        holds subjective answers for trainer review."""
        assessment = self.get_object()
        if not assessment.is_published:
            raise ValidationError("This assessment is not open.")
        now = timezone.now()
        if assessment.available_from and now < assessment.available_from:
            raise ValidationError("This assessment is not yet available.")
        if assessment.available_to and now > assessment.available_to:
            raise ValidationError("This assessment is closed.")

        payload = SubmitSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        prior = Submission.objects.filter(
            assessment=assessment, student=request.user
        ).count()
        if assessment.max_attempts and prior >= assessment.max_attempts:
            raise ValidationError("You have used all attempts for this assessment.")

        submission = _grade_and_create_submission(
            assessment=assessment,
            student=request.user,
            attempt_no=prior + 1,
            answers=payload.validated_data["answers"],
            time_taken=payload.validated_data.get("time_taken_seconds", 0),
        )
        return Response(
            SubmissionSerializer(submission).data, status=status.HTTP_201_CREATED
        )


def _grade_and_create_submission(assessment, student, attempt_no, answers, time_taken):
    """Create a Submission + Answers, auto-grading objective questions.

    Objective (MCQ/multi) answers are scored against the correct choice set.
    Subjective answers (descriptive/coding/file) get 0 pending manual review and
    force the submission into SUBMITTED (awaiting grading) rather than PASS/FAIL.
    """
    questions = {q.id: q for q in assessment.questions.prefetch_related("choices")}
    total_points = sum(q.points for q in questions.values()) or 0

    enrollment = Enrollment.objects.filter(
        student=student, course=assessment.course
    ).first()

    submission = Submission.objects.create(
        assessment=assessment,
        student=student,
        enrollment=enrollment,
        attempt_no=attempt_no,
        status=Submission.Status.IN_PROGRESS,
        started_at=timezone.now(),
        time_taken_seconds=time_taken,
    )

    earned = 0
    has_manual = False
    for ans in answers:
        question = ans["question"]
        if question.id not in questions:
            continue  # answer for a question not on this assessment — skip
        selected = ans.get("selected_choices", [])
        answer = Answer.objects.create(
            submission=submission,
            question=question,
            text_answer=ans.get("text_answer", ""),
            code=ans.get("code", ""),
            file_key=ans.get("file_key", ""),
        )
        if selected:
            answer.selected_choices.set(selected)

        if question.question_type in AUTO_GRADED_TYPES:
            correct_ids = set(
                question.choices.filter(is_correct=True).values_list("id", flat=True)
            )
            selected_ids = {c.id for c in selected}
            is_correct = bool(correct_ids) and selected_ids == correct_ids
            points = question.points if is_correct else 0
            answer.is_correct = is_correct
            answer.points_awarded = points
            answer.save(update_fields=["is_correct", "points_awarded"])
            earned += points
        else:
            has_manual = True

    percent = round(earned / total_points * 100) if total_points else 0
    now = timezone.now()
    submission.score = earned
    submission.percent = percent
    submission.submitted_at = now
    if has_manual:
        # Objective part scored; subjective part awaits trainer grading.
        submission.status = Submission.Status.SUBMITTED
        submission.passed = False
    else:
        passed = percent >= assessment.pass_percent
        submission.status = (
            Submission.Status.PASSED if passed else Submission.Status.FAILED
        )
        submission.passed = passed
    submission.save(
        update_fields=[
            "score", "percent", "submitted_at", "status", "passed", "updated_at"
        ]
    )
    return submission


class SubmissionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Assessment attempts. Students see their own; trainers see submissions for
    assessments they own. Filter by ``?assessment=<id>``, ``?status=<status>``.
    Trainers grade held submissions via ``POST .../{id}/grade/``."""

    serializer_class = SubmissionSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES
    api_roles_by_action = {"grade": TRAINER_WRITE}

    def get_queryset(self):
        user = self.request.user
        qs = Submission.objects.select_related(
            "assessment", "assessment__trainer", "student"
        ).prefetch_related("answers")
        if _is_admin(user):
            pass
        elif _is_trainer_role(user):
            qs = qs.filter(Q(assessment__trainer=user) | Q(student=user))
        else:
            qs = qs.filter(student=user)
        qs = _filter_by(qs, self.request, "assessment", "assessment_id")
        qs = _filter_by(qs, self.request, "status")
        return qs

    @extend_schema(request=GradeSerializer, responses=SubmissionSerializer)
    @action(detail=True, methods=["post"])
    def grade(self, request, pk=None):
        """Trainer manual grade for subjective submissions (PRD §3.12 rubric).

        The body is ``GradeSerializer`` — ``score``, ``percent``, and optional
        ``feedback`` and ``passed``. Annotated explicitly because the viewset's
        ``serializer_class`` is ``SubmissionSerializer``, which is what the
        schema would otherwise advertise as the request body.
        """
        submission = self.get_object()
        user = request.user
        if submission.assessment.trainer_id != user.id and not _is_admin(user):
            raise PermissionDenied("Only the assessment's trainer can grade it.")
        payload = GradeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        # When the assessment is graded by rubric, the score has to fit it —
        # otherwise "rubric" is a label on the same free-text grading as
        # "manual", which is exactly what it was before.
        rubric_total = _rubric_total(submission.assessment)
        if rubric_total is not None:
            if rubric_total <= 0:
                raise ValidationError(
                    "This assessment is graded by rubric but its rubric has no "
                    "criteria yet. Add them before grading."
                )
            if data["score"] > rubric_total:
                raise ValidationError(
                    f"Score {data['score']} is above the rubric total of "
                    f"{rubric_total}."
                )

        submission.score = data["score"]
        submission.percent = data["percent"]
        submission.feedback = data.get("feedback", submission.feedback)
        passed = data.get(
            "passed", data["percent"] >= submission.assessment.pass_percent
        )
        submission.passed = passed
        submission.status = (
            Submission.Status.PASSED if passed else Submission.Status.FAILED
        )
        submission.grader = user
        submission.graded_at = timezone.now()
        submission.save(
            update_fields=[
                "score", "percent", "feedback", "passed",
                "status", "grader", "graded_at", "updated_at",
            ]
        )
        return Response(SubmissionSerializer(submission).data)
