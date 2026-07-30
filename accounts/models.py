from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from accounts.managers import UserManager
from core.models import TimeStampedModel


class Role(models.TextChoices):
    """Platform roles (PRD §2). Mirrored by core.permissions role classes."""

    ADMIN = "admin", "Admin"
    TRAINER = "trainer", "Trainer / Lecturer"
    STUDENT = "student", "Student / Learner"
    INSTITUTION = "institution", "Institution / Corporate Client"


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """Email-first custom user (PRD §3.1).

    Deliberately has no ``username``. ``phone`` and ``organization`` are
    present now so future features (OTP login, institution multi-tenancy,
    social/biometric IDs) attach without a painful user-model migration.
    """

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255, blank=True)
    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.STUDENT
    )
    phone = models.CharField(max_length=20, blank=True, null=True)
    phone_verified = models.BooleanField(default=False)
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="members",
    )

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # email + password are prompted by default

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.email


class UserProfile(TimeStampedModel):
    """Public-facing profile shown on the dashboard header (all roles).

    Kept separate from ``User`` so auth stays lean while bios, avatars and
    social links can grow freely.
    """

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="profile"
    )
    headline = models.CharField(max_length=255, blank=True)
    bio = models.TextField(blank=True)
    avatar = models.URLField(blank=True)
    location = models.CharField(max_length=255, blank=True)
    website = models.URLField(blank=True)
    github_url = models.URLField(blank=True)
    linkedin_url = models.URLField(blank=True)
    language = models.CharField(max_length=20, default="en")
    timezone = models.CharField(max_length=64, default="UTC")
    tags = models.ManyToManyField("courses.Tag", blank=True, related_name="profiles")

    def __str__(self):
        return f"Profile<{self.user.email}>"


class TrainerProfile(TimeStampedModel):
    """Trainer-specific data (PRD §2.2, §3.3 revenue sharing, admin approval)."""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="trainer_profile"
    )
    expertise = models.CharField(max_length=255, blank=True)
    years_experience = models.PositiveIntegerField(default=0)
    hourly_rate = models.DecimalField(  # 1:1 pay-per-hour rate (PRD §3.6)
        max_digits=10, decimal_places=2, default=0
    )
    rating_avg = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    rating_count = models.PositiveIntegerField(default=0)
    is_approved = models.BooleanField(default=False)  # admin onboarding gate
    revenue_share_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=70
    )
    payout_account_ref = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"TrainerProfile<{self.user.email}>"


class LearnerStats(TimeStampedModel):
    """Denormalized learner dashboard counters (PRD §3.12 learning progress)."""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="learner_stats"
    )
    streak_days = models.PositiveIntegerField(default=0)
    last_active_date = models.DateField(null=True, blank=True)
    courses_enrolled = models.PositiveIntegerField(default=0)
    avg_progress = models.PositiveIntegerField(default=0)  # 0-100
    certificates_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name_plural = "learner stats"

    def __str__(self):
        return f"LearnerStats<{self.user.email}>"


class LoginActivity(TimeStampedModel):
    """Login audit trail (PRD §3.1 advanced: login activity logs)."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="login_activities",
        null=True,
        blank=True,
    )
    email_attempted = models.EmailField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    success = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "login activities"

    def __str__(self):
        status = "OK" if self.success else "FAIL"
        return f"{self.email_attempted or self.user} [{status}] @ {self.created_at}"
