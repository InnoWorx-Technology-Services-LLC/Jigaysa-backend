"""Serializers for live training, 1:1 and group sessions (PRD §3.5, §3.6)."""

from rest_framework import serializers

from live.models import (
    Attendance,
    IndividualBooking,
    LiveSession,
    SessionDoubt,
    SessionRegistration,
    TrainerAvailability,
)
from payments.models import Order


class TrainerMiniSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    full_name = serializers.CharField()
    email = serializers.EmailField()


class MentorSerializer(serializers.Serializer):
    """A bookable trainer for the 1:1 "choose a mentor" step (PRD §3.6).

    Flat by design: the picker needs a name, a rate and a headline, not the whole
    profile graph.
    """

    id = serializers.IntegerField()
    full_name = serializers.CharField()
    email = serializers.EmailField()
    headline = serializers.CharField(source="profile.headline", default="")
    avatar = serializers.CharField(source="profile.avatar", default="")
    expertise = serializers.CharField(source="trainer_profile.expertise", default="")
    years_experience = serializers.IntegerField(
        source="trainer_profile.years_experience", default=0
    )
    hourly_rate = serializers.DecimalField(
        source="trainer_profile.hourly_rate",
        max_digits=10,
        decimal_places=2,
        default=0,
    )
    rating_avg = serializers.DecimalField(
        source="trainer_profile.rating_avg", max_digits=3, decimal_places=2, default=0
    )
    rating_count = serializers.IntegerField(
        source="trainer_profile.rating_count", default=0
    )


class LiveSessionSerializer(serializers.ModelSerializer):
    trainer = TrainerMiniSerializer(read_only=True)
    registrations_count = serializers.IntegerField(
        source="registrations.count", read_only=True
    )
    my_registration_status = serializers.SerializerMethodField()

    class Meta:
        model = LiveSession
        fields = (
            "id",
            "course",
            "batch",
            "trainer",
            "title",
            "description",
            "session_type",
            "scheduled_start",
            "duration_minutes",
            "capacity",
            "registration_limit",
            "status",
            "join_url",
            "meeting_id",
            "attendees_count",
            "registrations_count",
            "my_registration_status",
            "created_at",
        )
        read_only_fields = ("attendees_count",)

    def get_my_registration_status(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        reg = obj.registrations.filter(student=request.user).first()
        return reg.status if reg else None


class LiveSessionWriteSerializer(serializers.ModelSerializer):
    """Trainer create/update shape. ``trainer`` comes from the request."""

    class Meta:
        model = LiveSession
        fields = (
            "id",
            "course",
            "batch",
            "title",
            "description",
            "session_type",
            "scheduled_start",
            "duration_minutes",
            "capacity",
            "registration_limit",
            "status",
            "join_url",
            "meeting_id",
        )


class SessionRegistrationSerializer(serializers.ModelSerializer):
    session_detail = LiveSessionSerializer(source="session", read_only=True)

    class Meta:
        model = SessionRegistration
        fields = (
            "id",
            "session",
            "session_detail",
            "status",
            "registered_at",
            "joined_at",
            "attended",
        )
        read_only_fields = ("status", "registered_at", "joined_at", "attended")


class SessionDoubtSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.full_name", read_only=True)

    class Meta:
        model = SessionDoubt
        fields = (
            "id",
            "session",
            "student",
            "student_name",
            "text",
            "status",
            "asked_at",
        )
        read_only_fields = ("student", "status", "asked_at")


class TrainerAvailabilitySerializer(serializers.ModelSerializer):
    trainer = TrainerMiniSerializer(read_only=True)

    class Meta:
        model = TrainerAvailability
        fields = (
            "id",
            "trainer",
            "start",
            "end",
            "slot_minutes",
            "is_booked",
        )
        read_only_fields = ("is_booked",)


class IndividualBookingSerializer(serializers.ModelSerializer):
    trainer_name = serializers.CharField(source="trainer.full_name", read_only=True)
    student_name = serializers.CharField(source="student.full_name", read_only=True)
    topic = serializers.CharField(max_length=255)  # required on the booking form
    payment_due_at = serializers.DateTimeField(read_only=True)
    amount_due = serializers.SerializerMethodField()
    is_paid = serializers.SerializerMethodField()

    class Meta:
        model = IndividualBooking
        fields = (
            "id",
            "trainer",
            "trainer_name",
            "student",
            "student_name",
            "topic",
            "notes",
            "start",
            "duration_minutes",
            "status",
            "order",
            "amount_due",
            "is_paid",
            "payment_due_at",
            "meeting_url",
            "created_at",
        )
        read_only_fields = ("student", "status", "order", "meeting_url")

    def get_amount_due(self, obj):
        """What the student still owes, or ``None`` when nothing is payable."""
        if obj.order_id is None or obj.order.status == Order.Status.PAID:
            return None
        return obj.order.total

    def get_is_paid(self, obj):
        return bool(obj.order_id and obj.order.status == Order.Status.PAID)


class AttendanceSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.full_name", read_only=True)

    class Meta:
        model = Attendance
        fields = (
            "id",
            "session",
            "student",
            "student_name",
            "joined_at",
            "left_at",
            "duration_seconds",
            "present",
        )
