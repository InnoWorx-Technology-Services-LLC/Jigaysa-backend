"""Serializers for quizzes, assignments, submissions and grading (PRD §3.12).

Choice correctness is hidden from students: ``ChoiceSerializer`` never exposes
``is_correct``, and question payloads for the attempt flow omit answer keys.
"""

from django.utils import timezone
from rest_framework import serializers

from assessments.models import (
    Answer,
    Assessment,
    Choice,
    Question,
    Rubric,
    Submission,
)


class ChoiceSerializer(serializers.ModelSerializer):
    """Student-facing choice — never reveals the answer key."""

    class Meta:
        model = Choice
        fields = ("id", "text", "order")


class QuestionSerializer(serializers.ModelSerializer):
    """Student-facing question shape (no ``is_correct`` on choices)."""

    choices = ChoiceSerializer(many=True, read_only=True)

    class Meta:
        model = Question
        fields = (
            "id",
            "assessment",
            "question_type",
            "text",
            "points",
            "order",
            "choices",
        )


class ChoiceAuthorSerializer(serializers.ModelSerializer):
    """Trainer-facing choice — **includes the answer key**.

    Deliberately separate from ``ChoiceSerializer``: that one is the student
    shape and must never carry ``is_correct``. Keeping them apart means a
    student endpoint cannot leak the key by accident.
    """

    class Meta:
        model = Choice
        fields = ("id", "text", "is_correct", "order")


class QuestionAuthorSerializer(serializers.ModelSerializer):
    """Trainer-facing question with writable nested choices.

    The editor posts a question and its options together, so choices are
    written here rather than through a second endpoint — a half-saved question
    with no options is not a state worth allowing.
    """

    choices = ChoiceAuthorSerializer(many=True, required=False)

    class Meta:
        model = Question
        fields = (
            "id",
            "assessment",
            "question_type",
            "text",
            "points",
            "order",
            "meta",
            "choices",
        )
        # Always taken from the URL of the bulk endpoint, never the body — a
        # payload can't move a question onto someone else's assessment.
        read_only_fields = ("assessment",)

    def validate(self, attrs):
        """Objective questions need options and exactly the right number of keys."""
        question_type = attrs.get(
            "question_type",
            getattr(self.instance, "question_type", Question.QuestionType.MCQ),
        )
        choices = attrs.get("choices")
        if choices is None:
            return attrs  # partial update that leaves the options alone

        objective = question_type in (
            Question.QuestionType.MCQ,
            Question.QuestionType.MULTI,
        )
        if not objective:
            return attrs
        if len(choices) < 2:
            raise serializers.ValidationError(
                {"choices": "Give the question at least two options."}
            )
        correct = [c for c in choices if c.get("is_correct")]
        if not correct:
            raise serializers.ValidationError(
                {"choices": "Mark which option is correct."}
            )
        if question_type == Question.QuestionType.MCQ and len(correct) > 1:
            raise serializers.ValidationError(
                {"choices": "A single-answer question can have only one correct option."}
            )
        return attrs

    def _write_choices(self, question, choices):
        """Replace the option set wholesale.

        Simpler and safer than diffing: the editor always submits the full list,
        and a stale option surviving a rewrite could silently remain a valid
        answer.
        """
        question.choices.all().delete()
        Choice.objects.bulk_create(
            [
                Choice(
                    question=question,
                    text=choice.get("text", ""),
                    is_correct=choice.get("is_correct", False),
                    order=choice.get("order", index),
                )
                for index, choice in enumerate(choices)
            ]
        )

    def create(self, validated_data):
        choices = validated_data.pop("choices", [])
        question = Question.objects.create(**validated_data)
        self._write_choices(question, choices)
        _sync_question_count(question.assessment)
        return question

    def update(self, instance, validated_data):
        choices = validated_data.pop("choices", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if choices is not None:
            self._write_choices(instance, choices)
        return instance


def _sync_question_count(assessment):
    Assessment.objects.filter(pk=assessment.pk).update(
        total_questions=assessment.questions.count()
    )


class QuestionBulkSerializer(serializers.Serializer):
    """Body of ``POST /assessments/{id}/questions/`` — the "Save questions" button."""

    questions = QuestionAuthorSerializer(many=True)


class RubricSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rubric
        fields = ("id", "assessment", "criteria")


class AssessmentSerializer(serializers.ModelSerializer):
    """List/detail card. Questions are embedded on retrieve via the view."""

    class Meta:
        model = Assessment
        fields = (
            "id",
            "course",
            "lesson",
            "trainer",
            "title",
            "assessment_type",
            "description",
            "time_limit_minutes",
            "pass_percent",
            "max_attempts",
            "available_from",
            "available_to",
            "grading_type",
            "is_published",
            "total_questions",
            "created_at",
        )
        read_only_fields = ("trainer", "total_questions")


class AssessmentDetailSerializer(AssessmentSerializer):
    questions = QuestionSerializer(many=True, read_only=True)

    class Meta(AssessmentSerializer.Meta):
        fields = AssessmentSerializer.Meta.fields + ("questions",)


class AnswerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Answer
        fields = (
            "id",
            "question",
            "selected_choices",
            "text_answer",
            "code",
            "file_key",
            "is_correct",
            "points_awarded",
        )
        read_only_fields = ("is_correct", "points_awarded")


class SubmissionSerializer(serializers.ModelSerializer):
    """A submission, with the student inlined.

    The name and email travel with the row because the only screen that reads
    this list is a grading queue, and a queue that says "student: 12" cannot be
    worked. Mirrors how ``AdminPayoutSerializer`` inlines the trainer.
    """

    answers = AnswerSerializer(many=True, read_only=True)
    student_name = serializers.CharField(
        source="student.full_name", read_only=True, default=""
    )
    student_email = serializers.EmailField(
        source="student.email", read_only=True
    )
    assessment_title = serializers.CharField(
        source="assessment.title", read_only=True
    )

    class Meta:
        model = Submission
        fields = (
            "id",
            "assessment",
            "assessment_title",
            "student",
            "student_name",
            "student_email",
            "enrollment",
            "attempt_no",
            "status",
            "started_at",
            "submitted_at",
            "score",
            "percent",
            "passed",
            "time_taken_seconds",
            "feedback",
            "graded_at",
            "answers",
        )
        read_only_fields = (
            "student",
            "attempt_no",
            "status",
            "started_at",
            "submitted_at",
            "score",
            "percent",
            "passed",
            "feedback",
            "graded_at",
        )


class AnswerInputSerializer(serializers.Serializer):
    """One answer within a submit payload."""

    question = serializers.PrimaryKeyRelatedField(queryset=Question.objects.all())
    selected_choices = serializers.PrimaryKeyRelatedField(
        queryset=Choice.objects.all(), many=True, required=False
    )
    text_answer = serializers.CharField(required=False, allow_blank=True)
    code = serializers.CharField(required=False, allow_blank=True)
    # Object key from POST /api/v1/uploads/presign/ for assignment file answers.
    file_key = serializers.CharField(required=False, allow_blank=True, max_length=1024)


class SubmitSerializer(serializers.Serializer):
    """Payload to attempt an assessment: a list of per-question answers."""

    answers = AnswerInputSerializer(many=True)
    time_taken_seconds = serializers.IntegerField(required=False, min_value=0)


class GradeSerializer(serializers.Serializer):
    """Trainer manual-grade payload for descriptive/coding submissions."""

    score = serializers.DecimalField(max_digits=7, decimal_places=2)
    percent = serializers.IntegerField(min_value=0, max_value=100)
    feedback = serializers.CharField(required=False, allow_blank=True)
    passed = serializers.BooleanField(required=False)


# --------------------------------------------------------------------------- #
# The trainer's Assignments page
# --------------------------------------------------------------------------- #


class AssessmentBoardSerializer(AssessmentSerializer):
    """An assessment as the trainer's Assignments list draws it.

    The three counts are **annotated by the view in one query**, not computed
    per row — a serializer that reaches back into the ORM turns a 20-row page
    into 60 queries, and this page exists to be scanned quickly.
    """

    course_title = serializers.CharField(source="course.title", read_only=True)
    submitted_count = serializers.IntegerField(read_only=True)
    pending_review_count = serializers.IntegerField(read_only=True)
    enrolled_count = serializers.IntegerField(read_only=True)
    state = serializers.SerializerMethodField()

    class Meta(AssessmentSerializer.Meta):
        fields = AssessmentSerializer.Meta.fields + (
            "course_title", "submitted_count", "pending_review_count",
            "enrolled_count", "state",
        )

    def get_state(self, obj) -> str:
        """The `open` / `closed` badge, as one word the table prints.

        Three things decide it and the client should not have to reassemble
        them: an unpublished assessment is a `draft`, one past its
        ``available_to`` is `closed`, anything else is `open`. A row with no
        deadline never closes on its own.
        """
        if not obj.is_published:
            return "draft"
        if obj.available_to and obj.available_to < timezone.now():
            return "closed"
        return "open"


class AssignmentStatsSerializer(serializers.Serializer):
    """The three tiles above the list.

    ``pending_reviews`` is the only actionable number on the page — it is work
    queued for this trainer specifically — so it is counted across all their
    assessments, not just the open ones. Closing an assignment does not grade
    what students already handed in.
    """

    open_assignments = serializers.IntegerField()
    pending_reviews = serializers.IntegerField()
    average_score = serializers.FloatField(allow_null=True)
