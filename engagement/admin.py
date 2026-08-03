"""Admin for the forum and gamification (PRD §3.12).

``PointRule`` is the point of this module: reputation values are tuned from
here, not from a code deploy. The other registrations exist so an admin can
moderate threads and see why someone's score moved.
"""

from django.contrib import admin

from engagement.models import (
    Badge,
    CommunityPost,
    CommunityProfile,
    DiscussionReply,
    DiscussionThread,
    PointRule,
    UserBadge,
    Vote,
)


@admin.register(PointRule)
class PointRuleAdmin(admin.ModelAdmin):
    """Edit what each activity is worth. Changes apply to the *next* award —
    reputation already granted is never recalculated."""

    list_display = ("activity", "label", "points", "is_active")
    list_editable = ("points", "is_active")
    search_fields = ("activity", "label")
    ordering = ("activity",)


@admin.register(DiscussionThread)
class DiscussionThreadAdmin(admin.ModelAdmin):
    list_display = (
        "title", "author", "visibility", "status", "score", "reply_count",
        "views_count", "created_at",
    )
    list_filter = ("visibility", "status", "scope", "is_pinned")
    search_fields = ("title", "body", "author__email")
    filter_horizontal = ("tags",)
    readonly_fields = ("score", "reply_count", "views_count")


@admin.register(DiscussionReply)
class DiscussionReplyAdmin(admin.ModelAdmin):
    list_display = ("thread", "author", "is_accepted_answer", "score", "created_at")
    list_filter = ("is_accepted_answer",)
    search_fields = ("body", "author__email")
    readonly_fields = ("score",)


@admin.register(Vote)
class VoteAdmin(admin.ModelAdmin):
    list_display = ("user", "content_type", "object_id", "value", "created_at")
    list_filter = ("value", "content_type")
    search_fields = ("user__email",)


@admin.register(CommunityProfile)
class CommunityProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "points", "level", "badges_count")
    search_fields = ("user__email", "user__full_name")
    ordering = ("-points",)


admin.site.register([Badge, UserBadge, CommunityPost])
