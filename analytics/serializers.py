"""Response shapes for the admin Reports page (PRD §3.14).

Plain ``Serializer``s, not ``ModelSerializer``s: none of these correspond to a
table. They are the shapes four charts need, and defining them explicitly is
what stops a view quietly changing a key that a chart is indexing by.
"""

from rest_framework import serializers


class ReportSummarySerializer(serializers.Serializer):
    """The four tiles.

    ``active_window_days`` travels with ``active_users`` on purpose — "active"
    is a definition, not a fact, and a number whose definition lives only in the
    backend is one the frontend will eventually label wrongly.
    """

    active_users = serializers.IntegerField()
    enrollments = serializers.IntegerField()
    revenue = serializers.DecimalField(max_digits=14, decimal_places=2)
    payouts = serializers.DecimalField(max_digits=14, decimal_places=2)
    currency = serializers.CharField()
    active_window_days = serializers.IntegerField()


class TrendPointSerializer(serializers.Serializer):
    """One month on a chart. ``month`` is ``YYYY-MM``, always present."""

    month = serializers.CharField()
    value = serializers.DecimalField(max_digits=14, decimal_places=2)


class TrendSerializer(serializers.Serializer):
    """Both line charts, over one shared and gap-free month range.

    Every month in the window appears in both arrays, in order, including the
    ones with nothing in them — so index *n* is the same month in each and the
    two charts can share an axis.
    """

    months = serializers.IntegerField()
    enrollments = TrendPointSerializer(many=True)
    revenue = TrendPointSerializer(many=True)
    currency = serializers.CharField()


class RoleCountSerializer(serializers.Serializer):
    role = serializers.CharField()
    label = serializers.CharField()
    count = serializers.IntegerField()


class RoleBreakdownSerializer(serializers.Serializer):
    """The "Users by role" bars, with the denominator included."""

    total = serializers.IntegerField()
    roles = RoleCountSerializer(many=True)


class AttendanceRowSerializer(serializers.Serializer):
    """One batch in the attendance table.

    ``attendance_rate`` is a percentage 0–100, or **null when no register has
    been taken** for the batch. Null and zero mean opposite things here and the
    bar should render as "no data", not as total absence.
    """

    batch_id = serializers.IntegerField()
    batch = serializers.CharField()
    course = serializers.CharField()
    course_slug = serializers.CharField()
    enrolled = serializers.IntegerField()
    capacity = serializers.IntegerField()
    attendance_rate = serializers.FloatField(allow_null=True)


# --------------------------------------------------------------------------- #
# The trainer's Analytics page
# --------------------------------------------------------------------------- #


class TrainerSummarySerializer(serializers.Serializer):
    """The four tiles, all scoped to the caller's own courses.

    The three rates are **null when there is nothing to average**, never zero.
    A trainer with no submissions has no quiz average; printing 0% would read
    as "everyone failed", which is a different and much worse message.
    """

    completion = serializers.FloatField(allow_null=True)
    quiz_average = serializers.FloatField(allow_null=True)
    submission_rate = serializers.FloatField(allow_null=True)
    active_learners = serializers.IntegerField()
    courses = serializers.IntegerField()


class TrainerTrendSerializer(serializers.Serializer):
    """The engagement chart.

    Two series over one dense, shared month range — index *n* is the same month
    in both. Named for what they actually count rather than reusing the admin
    trend shape, which would have handed this page a field called ``revenue``.
    """

    months = serializers.IntegerField()
    lessons_completed = TrendPointSerializer(many=True)
    submissions = TrendPointSerializer(many=True)


class CourseInsightSerializer(serializers.Serializer):
    """One row of the per-course table.

    ``completion`` and ``quiz_average`` are percentages 0–100, or ``null`` when
    the course has no enrolments / nothing graded — the same null-not-zero rule
    the tiles follow.
    """

    course_id = serializers.IntegerField()
    course = serializers.CharField()
    course_slug = serializers.CharField()
    learners = serializers.IntegerField()
    completion = serializers.FloatField(allow_null=True)
    quiz_average = serializers.FloatField(allow_null=True)


class DoubtSerializer(serializers.Serializer):
    """A question a student raised in one of this trainer's sessions.

    The raw doubt, not a clustered topic — see ``TrainerDoubtsView`` for why the
    mock's "AI-detected" ranking is not what this returns.
    """

    id = serializers.IntegerField()
    text = serializers.CharField()
    status = serializers.CharField()
    asked_at = serializers.DateTimeField()
    student_name = serializers.CharField(allow_blank=True)
    session_title = serializers.CharField(allow_blank=True)
    course = serializers.CharField(allow_blank=True)
    answer = serializers.CharField(allow_blank=True, default="")
    answered_at = serializers.DateTimeField(allow_null=True, default=None)


class DoubtAnswerSerializer(serializers.Serializer):
    """Body of ``POST /trainer/analytics/doubts/{id}/answer/``."""

    answer = serializers.CharField(max_length=10000)

    def validate_answer(self, value):
        answer = (value or "").strip()
        if not answer:
            raise serializers.ValidationError("Write an answer to send.")
        return answer
