"""Serializers for the discussion forum, community feed and gamification (§3.12)."""

from django.utils.text import slugify
from rest_framework import serializers

from courses.models import Tag
from engagement.models import (
    Badge,
    CommunityPost,
    CommunityProfile,
    DiscussionReply,
    DiscussionThread,
    UserBadge,
)


class AuthorMiniSerializer(serializers.Serializer):
    """Compact author card (works for any user)."""

    id = serializers.IntegerField()
    full_name = serializers.CharField()
    role = serializers.CharField()


class MyVoteMixin:
    """Adds ``my_vote`` (1 / -1 / 0) so the UI can light the pressed arrow."""

    def get_my_vote(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return 0
        vote = obj.votes.filter(user=request.user).first()
        return vote.value if vote else 0


class DiscussionReplySerializer(MyVoteMixin, serializers.ModelSerializer):
    author = AuthorMiniSerializer(read_only=True)
    my_vote = serializers.SerializerMethodField()

    class Meta:
        model = DiscussionReply
        fields = (
            "id",
            "thread",
            "author",
            "parent",
            "body",
            "is_accepted_answer",
            "score",
            "my_vote",
            "created_at",
        )
        read_only_fields = ("author", "is_accepted_answer", "score", "created_at")


class TagField(serializers.Field):
    """Tags as a plain list of names, created on first use.

    The Ask form lets a student type any tag, so a strict "must already exist"
    field would reject the first person to use one. Matching is by slug, which
    keeps "Data Viz", "data-viz" and "data viz" as a single tag rather than
    three near-duplicates.
    """

    def to_representation(self, value):
        return [tag.slug for tag in value.all()]

    def to_internal_value(self, data):
        if not isinstance(data, (list, tuple)):
            raise serializers.ValidationError("Send tags as a list of names.")
        if len(data) > 5:
            raise serializers.ValidationError("A question can carry at most 5 tags.")
        tags = []
        for raw in data:
            name = str(raw).strip()
            if not name:
                continue
            slug = slugify(name)
            if not slug:
                raise serializers.ValidationError(f"'{raw}' is not a usable tag.")
            tag, _ = Tag.objects.get_or_create(slug=slug, defaults={"name": name})
            tags.append(tag)
        return tags


class ForumTagSerializer(serializers.Serializer):
    """A tag plus how many visible questions carry it (``Python · 42``)."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    slug = serializers.SlugField(read_only=True)
    thread_count = serializers.IntegerField(read_only=True)


class LeaderboardEntrySerializer(serializers.ModelSerializer):
    user = AuthorMiniSerializer(read_only=True)

    class Meta:
        model = CommunityProfile
        fields = ("user", "points", "level", "badges_count")


class DiscussionThreadSerializer(MyVoteMixin, serializers.ModelSerializer):
    author = AuthorMiniSerializer(read_only=True)
    my_vote = serializers.SerializerMethodField()
    tags = TagField(required=False)
    tag_names = serializers.SerializerMethodField()

    class Meta:
        model = DiscussionThread
        fields = (
            "id",
            "course",
            "batch",
            "author",
            "title",
            "body",
            "scope",
            "visibility",
            "status",
            "tags",
            "tag_names",
            "is_pinned",
            "reply_count",
            "views_count",
            "score",
            "my_vote",
            "last_activity_at",
            "created_at",
        )
        read_only_fields = (
            "author",
            "status",
            "is_pinned",
            "reply_count",
            "views_count",
            "score",
            "last_activity_at",
            "created_at",
        )

    def get_tag_names(self, obj):
        """Display labels, so the chip row doesn't need a second lookup."""
        return [tag.name for tag in obj.tags.all()]


class DiscussionThreadDetailSerializer(DiscussionThreadSerializer):
    replies = DiscussionReplySerializer(many=True, read_only=True)

    class Meta(DiscussionThreadSerializer.Meta):
        fields = DiscussionThreadSerializer.Meta.fields + ("replies",)


class CommunityPostSerializer(serializers.ModelSerializer):
    author = AuthorMiniSerializer(read_only=True)

    class Meta:
        model = CommunityPost
        fields = (
            "id",
            "author",
            "body",
            "post_type",
            "likes_count",
            "created_at",
        )
        read_only_fields = ("author", "likes_count", "created_at")


class BadgeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Badge
        fields = ("id", "name", "slug", "icon", "description")


class UserBadgeSerializer(serializers.ModelSerializer):
    badge = BadgeSerializer(read_only=True)

    class Meta:
        model = UserBadge
        fields = ("id", "badge", "earned_at")


class CommunityProfileSerializer(serializers.ModelSerializer):
    badges = serializers.SerializerMethodField()

    class Meta:
        model = CommunityProfile
        fields = ("points", "level", "badges_count", "badges")

    def get_badges(self, obj):
        earned = UserBadge.objects.select_related("badge").filter(user=obj.user)
        return UserBadgeSerializer(earned, many=True).data
