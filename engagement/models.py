"""Discussion forum, community feed and gamification (PRD §3.12, dashboard).

Forum threads are scoped to a course (or community-wide). Community profile,
points and badges back the dashboard's community card.
"""

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models

from core.models import TimeStampedModel


class DiscussionThread(TimeStampedModel):
    """A question/discussion scoped to a course or the community (PRD §3.12)."""

    class Scope(models.TextChoices):
        COURSE = "course", "Course"
        COMMUNITY = "community", "Community"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"

    class Visibility(models.TextChoices):
        """Who may read the thread. **Both require login** — "public" here means
        platform-wide, never search-engine indexable."""

        COMMUNITY = "community", "My community"
        PUBLIC = "public", "Public"

    course = models.ForeignKey(
        "courses.Course",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="threads",
    )
    batch = models.ForeignKey(
        "courses.Batch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="threads",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="threads",
    )
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    scope = models.CharField(
        max_length=20, choices=Scope.choices, default=Scope.COURSE
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    visibility = models.CharField(
        max_length=20, choices=Visibility.choices, default=Visibility.COMMUNITY
    )
    # Reuses courses.Tag rather than a forum-only tag table — the same
    # vocabulary already tags profiles and courses, and two tag tables would
    # mean "python" existing twice with divergent counts.
    tags = models.ManyToManyField("courses.Tag", blank=True, related_name="threads")
    is_pinned = models.BooleanField(default=False)
    reply_count = models.PositiveIntegerField(default=0)
    views_count = models.PositiveIntegerField(default=0)
    # Signed: a heavily downvoted question goes below zero, as on Stack Overflow.
    score = models.IntegerField(default=0)
    # Declared so deleting a thread takes its votes with it — a generic FK has
    # no database-level cascade of its own.
    votes = GenericRelation("Vote", related_query_name="thread")
    last_activity_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-is_pinned", "-last_activity_at", "-created_at"]

    def __str__(self):
        return self.title


class DiscussionReply(TimeStampedModel):
    """A reply to a thread; self-nesting for threaded replies (PRD §3.12)."""

    thread = models.ForeignKey(
        DiscussionThread, on_delete=models.CASCADE, related_name="replies"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="replies",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )
    body = models.TextField()
    is_accepted_answer = models.BooleanField(default=False)
    score = models.IntegerField(default=0)
    votes = GenericRelation("Vote", related_query_name="reply")

    class Meta:
        ordering = ["created_at"]
        verbose_name_plural = "discussion replies"

    def __str__(self):
        return f"Reply by {self.author} on {self.thread}"


class Vote(TimeStampedModel):
    """An up/down vote on a thread or a reply (Stack Overflow style).

    One row per user per post is what makes a vote *changeable* rather than a
    counter bump: re-voting the same way removes it, voting the other way flips
    it, and reputation is adjusted by the difference either way.

    The target is a generic relation rather than two nullable FKs on purpose.
    Nullable FKs would need *conditional* unique constraints, which MySQL
    silently refuses to create (``models.W036``) — the one thing stopping a
    double vote would quietly not exist in production. A single non-null
    ``(user, content_type, object_id)`` unique key is enforced everywhere.
    """

    UP = 1
    DOWN = -1
    VALUE_CHOICES = ((UP, "Up"), (DOWN, "Down"))

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="votes"
    )
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    target = GenericForeignKey("content_type", "object_id")
    value = models.SmallIntegerField(choices=VALUE_CHOICES)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "content_type", "object_id"],
                name="unique_vote_per_user_per_post",
            )
        ]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self):
        return f"{self.user} {self.get_value_display()} on {self.target}"


class PointRule(TimeStampedModel):
    """How many points an activity is worth — editable by an admin.

    The values ship as Stack Overflow's, but reputation economies need tuning
    per platform, and changing them should not need a deploy. Look-ups fall back
    to the built-in defaults when a row is missing, so deleting one cannot
    silently turn an award into zero.
    """

    activity = models.SlugField(max_length=60, unique=True)
    label = models.CharField(max_length=120, blank=True)
    # Signed: downvotes cost the author points, and casting one costs the voter.
    points = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["activity"]

    def __str__(self):
        return f"{self.activity}: {self.points:+d}"


class CommunityProfile(TimeStampedModel):
    """Gamification state per user (dashboard "1,240 pts", level)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="community_profile",
    )
    points = models.PositiveIntegerField(default=0)
    level = models.PositiveIntegerField(default=1)
    badges_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.user} · {self.points} pts"


class Badge(TimeStampedModel):
    """A badge that can be earned (dashboard medals)."""

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    icon = models.CharField(max_length=64, blank=True)
    description = models.TextField(blank=True)
    criteria = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.name


class UserBadge(TimeStampedModel):
    """A badge earned by a user."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="badges",
    )
    badge = models.ForeignKey(
        Badge, on_delete=models.CASCADE, related_name="awarded_to"
    )
    earned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-earned_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "badge"], name="unique_user_badge"
            )
        ]

    def __str__(self):
        return f"{self.user} earned {self.badge}"


class CommunityPost(TimeStampedModel):
    """A community feed post (PRD community support)."""

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="community_posts",
    )
    body = models.TextField()
    post_type = models.CharField(max_length=40, blank=True)
    likes_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Post by {self.author}"
