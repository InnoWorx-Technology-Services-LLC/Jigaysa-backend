"""Response shapes for the admin Reports page (PRD §3.14).

Plain ``Serializer``s, not ``ModelSerializer``s: none of these correspond to a
table. They are the shapes four charts need, and defining them explicitly is
what stops a view quietly changing a key that a chart is indexing by.
"""

from rest_framework import serializers

from accounts.models import Role, User
from courses.models import Batch, Course, Enrollment


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


# --------------------------------------------------------------------------- #
# The institution console's Overview page
# --------------------------------------------------------------------------- #


class OrganizationBriefSerializer(serializers.Serializer):
    """Who the numbers below belong to.

    Travels with the overview so the console header does not need a second
    call, and carries ``is_active`` so a deactivated institution can be told
    so rather than shown a dashboard that looks entirely normal.
    """

    id = serializers.IntegerField()
    name = serializers.CharField()
    slug = serializers.CharField()
    type = serializers.CharField()
    is_active = serializers.BooleanField()


class InstitutionOverviewSerializer(serializers.Serializer):
    """The four tiles of the institution console.

    ``new_learner_window_days`` ships alongside ``learners_joined_recently``
    for the reason ``ReportSummarySerializer`` ships ``active_window_days``:
    "+128" is meaningless without "in the last 30 days", and a window that
    lives only in the backend is one the frontend will eventually mislabel.

    ``avg_completion`` is **null when the institution has no enrolments** —
    not 0. A college that starts on Monday has no completion rate; 0% says its
    learners are failing.

    ``learners`` is the institution's whole **roll**. The Batches screen's
    "Total learners" tile is a different number — distinct people enrolled in a
    cohort — and lives on that screen's own endpoint,
    ``/institution/batches/summary/``. One field meaning two things across two
    screens is how they start disagreeing in a review.
    """

    organization = OrganizationBriefSerializer()
    active_batches = serializers.IntegerField()
    batches = serializers.IntegerField()
    learners = serializers.IntegerField()
    learners_joined_recently = serializers.IntegerField()
    new_learner_window_days = serializers.IntegerField()
    avg_completion = serializers.FloatField(allow_null=True)
    classrooms = serializers.IntegerField()


class InstitutionBatchSerializer(serializers.Serializer):
    """One row of the institution's batch list.

    ``seats_taken`` and ``learners`` are both here and are not the same number:
    the first is the denormalised counter the enrolment flow maintains (seats
    sold), the second counts enrolments still ``active`` (people actually on
    the course). They diverge exactly when a cohort is emptying out, which is
    the thing worth seeing.

    ``completion`` is a percentage 0–100, or ``null`` when the batch has no
    enrolments yet.

    ``state`` is the card's badge — ``upcoming``, ``active``, ``completing`` or
    ``ended`` — derived **from the batch's dates only**, never from how far its
    learners have got. See ``analytics.institution_api.COMPLETING_WINDOW_DAYS``.
    """

    id = serializers.IntegerField()
    name = serializers.CharField()
    course = serializers.CharField()
    course_id = serializers.IntegerField()
    course_slug = serializers.CharField()
    trainer = serializers.CharField(allow_blank=True)
    trainer_id = serializers.IntegerField(allow_null=True)
    seats_taken = serializers.IntegerField()
    learners = serializers.IntegerField()
    capacity = serializers.IntegerField()
    completion = serializers.FloatField(allow_null=True)
    start_date = serializers.DateField(allow_null=True)
    end_date = serializers.DateField(allow_null=True)
    state = serializers.CharField()
    schedule = serializers.JSONField()


class InstitutionBookingSerializer(serializers.Serializer):
    """One upcoming classroom booking.

    ``status`` is the real ``ClassroomSession`` lifecycle — ``scheduled``,
    ``live`` or ``completed``. It is **not** the mock's confirmed/pending
    approval badge, which nothing in the platform models; see
    ``InstitutionBookingsView``.

    ``ends_at`` is derived from the attached session's duration and is ``null``
    when no session is attached — an empty room booking has no length to
    report, and guessing an hour would put a wrong time on a timetable.
    """

    id = serializers.IntegerField()
    room = serializers.CharField()
    room_id = serializers.IntegerField()
    location = serializers.CharField(allow_blank=True)
    starts_at = serializers.DateTimeField(allow_null=True)
    ends_at = serializers.DateTimeField(allow_null=True)
    status = serializers.CharField()
    batch = serializers.CharField(allow_blank=True)
    batch_id = serializers.IntegerField(allow_null=True)
    course = serializers.CharField(allow_blank=True)
    session_title = serializers.CharField(allow_blank=True)
    trainer = serializers.CharField(allow_blank=True)


class ActivityEventSerializer(serializers.Serializer):
    """One thing that happened, in a shape shared by every event type.

    Uniform on purpose: ``type`` selects the icon and the sentence template,
    ``actor``/``target`` fill it in, and ``meta`` carries the ids the row links
    to. A per-type shape would make the feed a switch statement on the client
    and a breaking change every time a type is added.

    ``actor`` is blank for events nobody performed — a batch being created is
    not attributable to a person the way an enrolment is.
    """

    type = serializers.CharField()
    at = serializers.DateTimeField()
    actor = serializers.CharField(allow_blank=True)
    target = serializers.CharField(allow_blank=True)
    meta = serializers.DictField()


class ActivityFeedSerializer(serializers.Serializer):
    """The recent-activity panel: newest first, capped at ``limit``.

    Not paginated — see ``InstitutionActivityView`` for why an offset over four
    merged tables is not a coherent page 2.
    """

    limit = serializers.IntegerField()
    events = ActivityEventSerializer(many=True)


class InstitutionBatchWriteSerializer(serializers.ModelSerializer):
    """Body of "New batch" and "Manage".

    > ### ``organization`` is deliberately not a field
    >
    > The tenant comes from the caller in the view. `courses.BatchSerializer`
    > *does* expose it as writable, which is exactly why the institution console
    > could not simply reuse that endpoint: it would let one institution file a
    > batch under another institution's name.

    ``enrolled_count`` is not writable either — it is a counter the enrolment
    flow maintains, and a client that could set it could make a cohort claim
    seats nobody bought.
    """

    class Meta:
        model = Batch
        fields = (
            "course", "name", "trainer",
            "start_date", "end_date", "capacity", "schedule",
        )

    def validate_name(self, value):
        name = (value or "").strip()
        if not name:
            raise serializers.ValidationError("Give the batch a name.")
        return name

    def validate_course(self, course):
        """Exactly the catalogue the Courses page shows.

        Deliberately the *same* queryset that backs
        ``GET /institution/courses/`` — see
        ``analytics.institution_api.institution_courses``. If browsing and
        assigning used two similar-but-separate rules, the first course where
        they disagreed would be a card with a live "Assign to batch" button
        that 400s when pressed, and nobody would find it until a customer did.
        """
        from analytics.institution_api import institution_courses

        org = self.context.get("organization")
        if org and institution_courses(org).filter(pk=course.pk).exists():
            return course
        raise serializers.ValidationError(
            "Choose a course from your institution's catalogue."
        )

    def validate_trainer(self, trainer):
        if trainer is None:
            return trainer
        if getattr(trainer, "role", None) != Role.TRAINER:
            raise serializers.ValidationError(
                "That account is not a trainer."
            )
        return trainer

    def validate(self, attrs):
        """Cross-field rules, evaluated against the **merged** object.

        ``PATCH`` sends one field; checking only what was sent would let a
        request that moves ``end_date`` before an untouched ``start_date``
        through. Each value therefore falls back to the instance's current one.
        """
        def merged(field):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, None)

        start, end = merged("start_date"), merged("end_date")
        if start and end and end < start:
            raise serializers.ValidationError(
                {"end_date": "The batch cannot end before it starts."}
            )

        capacity = merged("capacity")
        if capacity is not None and self.instance is not None:
            taken = self.instance.enrollments.filter(
                status=Enrollment.Status.ACTIVE
            ).count()
            if 0 < capacity < taken:
                # Shrinking below the people already in the room would make
                # "120 / 80" render on the card. Refuse rather than display it.
                raise serializers.ValidationError(
                    {"capacity": f"{taken} learners are already enrolled; "
                                 f"capacity cannot be lower."}
                )
        return attrs


class InstitutionBatchSummarySerializer(serializers.Serializer):
    """The Batches screen's tiles: one count per ``state`` a card can show.

    Separate from ``InstitutionOverviewSerializer`` because they belong to
    different screens. ``learners`` here means *distinct people in a cohort*;
    ``learners`` there means *the institution's roll*. Both are right for their
    own page and neither should be reached for from the other.

    ``active`` **includes** ``completing`` — a cohort three weeks from its end
    date is still running. ``active + upcoming + ended`` is the whole set;
    ``total`` is given so the client never has to assume that.
    """

    active = serializers.IntegerField()
    completing = serializers.IntegerField()
    upcoming = serializers.IntegerField()
    ended = serializers.IntegerField()
    total = serializers.IntegerField()
    learners = serializers.IntegerField()


class InstitutionCourseBatchSerializer(serializers.Serializer):
    """One of *your* cohorts running a course — the card's "Assigned to" line."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    learners = serializers.IntegerField()
    capacity = serializers.IntegerField()


class InstitutionCourseSerializer(serializers.Serializer):
    """One course card on the institution's Courses page.

    ``batches`` is **your** cohorts on this course, never anyone else's, and
    ``assigned`` is simply whether that list is non-empty — shipped as its own
    boolean so the card does not have to know that rule.

    ``duration_hours`` is derived from ``duration_minutes`` here rather than in
    the client: the card prints "40 h", and six clients dividing by 60 is six
    chances to round differently.
    """

    id = serializers.IntegerField()
    title = serializers.CharField()
    slug = serializers.CharField()
    subtitle = serializers.CharField(allow_blank=True)
    category = serializers.CharField(allow_blank=True)
    category_id = serializers.IntegerField(allow_null=True)
    trainer = serializers.CharField(allow_blank=True)
    trainer_id = serializers.IntegerField(allow_null=True)
    course_type = serializers.CharField()
    skill_level = serializers.CharField()
    duration_minutes = serializers.IntegerField()
    duration_hours = serializers.FloatField()
    thumbnail = serializers.CharField(allow_blank=True)
    is_free = serializers.BooleanField()
    is_own = serializers.BooleanField()
    assigned = serializers.BooleanField()
    batches = InstitutionCourseBatchSerializer(many=True)


class InstitutionCourseSummarySerializer(serializers.Serializer):
    """The Courses screen's three tiles, plus the denominator.

    ``active`` counts courses with a **currently running** cohort, so it is
    always ``<= assigned`` — a course cannot be running for you without being
    assigned to you. (The sample screen shows 4 active against 3 assigned,
    which no real data can produce.)

    ``learners`` is the same definition as the Batches screen's tile — distinct
    people with an active enrolment in one of your cohorts — and deliberately
    *not* the Dashboard's roll. See ``InstitutionBatchSummarySerializer``.
    """

    available = serializers.IntegerField()
    assigned = serializers.IntegerField()
    active = serializers.IntegerField()
    learners = serializers.IntegerField()


class InstitutionLearnerBatchSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class InstitutionLearnerSerializer(serializers.Serializer):
    """One row of the institution's learner roster.

    ``progress`` averages **your institution's training only** — the same
    enrolments the Dashboard's completion tile averages. A learner's unrelated
    personal course does not move this bar.

    ``last_active`` is ``User.last_login`` and is **null for someone who has
    never signed in** — render "never", not "today". (`LearnerStats
    .last_active_date` looks like a better source and is not: nothing in the
    application maintains it.)

    ``batch`` is the newest of ``batches``, which carries them all — the table
    prints one cell, and a learner in three cohorts should not have two of
    them silently dropped.
    """

    id = serializers.IntegerField()
    full_name = serializers.CharField(allow_blank=True)
    email = serializers.CharField()
    progress = serializers.FloatField()
    last_active = serializers.DateTimeField(allow_null=True)
    status = serializers.CharField()
    enrollments = serializers.IntegerField()
    active_enrollments = serializers.IntegerField()
    completed_enrollments = serializers.IntegerField()
    batch = serializers.CharField(allow_blank=True)
    batches = InstitutionLearnerBatchSerializer(many=True)
    joined = serializers.DateTimeField()


class InstitutionLearnerCourseSerializer(serializers.Serializer):
    """One enrolment on a learner's detail view."""

    enrollment_id = serializers.IntegerField()
    course = serializers.CharField()
    course_id = serializers.IntegerField()
    batch = serializers.CharField(allow_blank=True)
    batch_id = serializers.IntegerField(allow_null=True)
    status = serializers.CharField()
    progress = serializers.IntegerField()
    enrolled_at = serializers.DateTimeField()
    completed_at = serializers.DateTimeField(allow_null=True)


class InstitutionLearnerDetailSerializer(InstitutionLearnerSerializer):
    """The "View" action: the roster row plus what they are actually on."""

    courses = InstitutionLearnerCourseSerializer(many=True)
    certificates = serializers.IntegerField()


class InstitutionLearnerSummarySerializer(serializers.Serializer):
    """The Learners screen's four tiles.

    Three definitions travel with their numbers, for the reason
    ``ReportSummarySerializer`` ships ``active_window_days``: "active",
    "recently issued" and "at risk" are *definitions*, not facts, and one that
    lives only in the backend is one the frontend eventually labels wrongly.

    ``active`` uses the **platform's** existing active-user window, not a
    second one invented for this screen.

    ``avg_completion`` is the same number as the Dashboard's, and is ``null``
    — not 0 — when there is nothing to average.
    """

    total = serializers.IntegerField()
    active = serializers.IntegerField()
    active_window_days = serializers.IntegerField()
    avg_completion = serializers.FloatField(allow_null=True)
    certificates = serializers.IntegerField()
    certificates_recent = serializers.IntegerField()
    certificate_window_days = serializers.IntegerField()
    at_risk_idle_days = serializers.IntegerField()
    at_risk_progress_pct = serializers.IntegerField()


class InstitutionLearnerCreateSerializer(serializers.Serializer):
    """Body of "Add learner", and one row of "Import CSV".

    > ### An existing email is refused, not absorbed
    >
    > Quietly attaching an account somebody already created to an institution
    > would hand that institution a view of a stranger's learning and a say in
    > their account, off the back of typing their email address. Consent for
    > that is not something this endpoint can obtain.
    >
    > Attaching an existing account is a platform-admin action —
    > `POST /admin/organizations/{id}/members/add/` — and stays one.

    No password field. The account is created with **no usable password**;
    the learner sets their own through `/auth/password-reset/`. An institution
    choosing passwords for its learners is an institution that knows them.
    """

    email = serializers.EmailField()
    full_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    batch = serializers.IntegerField(
        required=False, allow_null=True,
        help_text="Optional: enrol the new learner in one of your batches.",
    )

    def validate_email(self, value):
        email = (value or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                "An account with this email already exists. A platform admin "
                "can add it to your institution."
            )
        return email

    def validate_batch(self, value):
        """Must be one of the caller's own batches.

        Looked up against the institution's own batches rather than all of
        them, so a stray id is "no such batch of yours" rather than a way to
        enrol somebody into another institution's cohort.
        """
        if value in (None, ""):
            return None
        org = self.context.get("organization")
        batch = Batch.objects.filter(pk=value, organization=org).first()
        if batch is None:
            raise serializers.ValidationError(
                "No such batch in your institution."
            )
        return batch


class InstitutionLearnerImportRowSerializer(serializers.Serializer):
    """One line of the import report, good or bad."""

    row = serializers.IntegerField(help_text="1-based line number in the file.")
    email = serializers.CharField(allow_blank=True)
    full_name = serializers.CharField(allow_blank=True, required=False)
    error = serializers.CharField(required=False)


class InstitutionLearnerImportSerializer(serializers.Serializer):
    """The result of "Import CSV".

    ``applied`` says whether anything was written. **It is `false` whenever a
    single row was rejected** — the import is all-or-nothing, because a partial
    one leaves the institution unable to tell which half landed and unable to
    safely re-run.

    ``rows`` reports *every* bad row rather than stopping at the first, so one
    round trip is enough to fix the file.
    """

    created = serializers.IntegerField()
    rejected = serializers.IntegerField()
    applied = serializers.BooleanField()
    rows = InstitutionLearnerImportRowSerializer(many=True)


# --------------------------------------------------------------------------- #
# The institution console's Reports page
# --------------------------------------------------------------------------- #


class InstitutionReportSummarySerializer(serializers.Serializer):
    """The Reports screen's four tiles.

    ``certified`` is a **count**, matching the tile. The cohort table's
    ``certified`` is also a count and ``certified_pct`` is the rate — the mock
    shows a count here and a percentage per row, so both are returned
    explicitly rather than left for the UI to infer one from the other.

    ``completion`` and ``attendance`` are ``null``, never ``0``, when there is
    nothing to average: a cohort nobody has taken a register for has no
    attendance rate, and 0% says everybody stayed away.
    """

    cohorts = serializers.IntegerField()
    active_cohorts = serializers.IntegerField()
    completion = serializers.FloatField(allow_null=True)
    certified = serializers.IntegerField()
    attendance = serializers.FloatField(allow_null=True)


class InstitutionReportCohortSerializer(serializers.Serializer):
    """One row of "Cohort completion".

    Every rate is a percentage 0–100 **or null when it has no denominator**:

    * ``completion`` — no enrolments in the cohort
    * ``certified_pct`` — no learners to be a fraction of
    * ``engagement`` — the course has no lessons, or the cohort no learners
    * ``attendance`` — no register has been taken

    Render those as a dash. Colouring "no data" as total failure is the kind of
    chart that starts a meeting.
    """

    batch_id = serializers.IntegerField()
    batch = serializers.CharField()
    course = serializers.CharField()
    course_id = serializers.IntegerField()
    learners = serializers.IntegerField()
    capacity = serializers.IntegerField()
    completion = serializers.FloatField(allow_null=True)
    certified = serializers.IntegerField()
    certified_pct = serializers.FloatField(allow_null=True)
    engagement = serializers.FloatField(allow_null=True)
    attendance = serializers.FloatField(allow_null=True)
    start_date = serializers.DateField(allow_null=True)
    end_date = serializers.DateField(allow_null=True)


class InstitutionAttendancePointSerializer(serializers.Serializer):
    """One month of the attendance trend.

    ``value`` is the present-rate and is **null for a month with no sessions**
    — zero would draw the line to the floor and say nobody turned up.
    ``sessions_recorded`` is the denominator, so the chart can show how much
    weight a point carries (and tell "nobody came" from "one person was
    marked").
    """

    month = serializers.CharField()
    value = serializers.FloatField(allow_null=True)
    sessions_recorded = serializers.IntegerField()


class InstitutionReportTrendSerializer(serializers.Serializer):
    """The attendance chart, over a dense month range — every month present,
    including the quiet ones, so a gap cannot be hidden by the line running
    straight over it."""

    months = serializers.IntegerField()
    attendance = InstitutionAttendancePointSerializer(many=True)
