"""Discussion forum, community feed and gamification API (PRD §3.12).

Students open threads and reply (peer learning); the thread author or the course
trainer/an admin can mark a reply as the accepted answer, which resolves the
thread. Community posts back the feed; badges/points back the dashboard's
community card. Writes are owner-scoped; reads are open to authenticated users.
"""

from django.db.models import Count, F, Q
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from courses.models import Tag
from engagement.models import (
    Badge,
    CommunityPost,
    CommunityProfile,
    DiscussionReply,
    DiscussionThread,
    UserBadge,
    Vote,
)
from engagement.serializers import (
    BadgeSerializer,
    CommunityPostSerializer,
    CommunityProfileSerializer,
    DiscussionReplySerializer,
    DiscussionThreadDetailSerializer,
    DiscussionThreadSerializer,
    ForumTagSerializer,
    LeaderboardEntrySerializer,
    UserBadgeSerializer,
)
from engagement.services import apply_vote, award_points

ALL_ROLES = ("student", "trainer", "admin", "institution")


def _is_admin(user):
    return getattr(user, "role", None) == "admin"


def _filter_by(qs, request, param, field=None):
    value = request.query_params.get(param)
    if value:
        qs = qs.filter(**{field or param: value})
    return qs


def _vote_value(request):
    """Read and validate a vote body: ``1`` (up) or ``-1`` (down)."""
    raw = request.data.get("value")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({"value": "Send 1 to upvote or -1 to downvote."})
    if value not in (Vote.UP, Vote.DOWN):
        raise ValidationError({"value": "Send 1 to upvote or -1 to downvote."})
    return value


def visible_threads(user):
    """Threads ``user`` may read.

    ``public`` is platform-wide but still **login-only** — nothing here is
    anonymous or search-indexable. ``community`` is scoped to the author's
    organization, which is the platform's existing tenant seam.

    Users with **no** organization share one implicit public-platform community
    rather than each being an island. Most learners sign up without an
    organization, so scoping "my community" strictly to a non-null org would
    make the default forum render empty for everyone — every private thread
    visible only to its own author. Real organizations stay isolated from each
    other and from the orgless pool.
    """
    qs = DiscussionThread.objects.all()
    if _is_admin(user):
        return qs
    community = Q(visibility=DiscussionThread.Visibility.COMMUNITY)
    if getattr(user, "organization_id", None):
        community &= Q(author__organization_id=user.organization_id)
    else:
        community &= Q(author__organization__isnull=True)
    visible = (
        Q(visibility=DiscussionThread.Visibility.PUBLIC) | Q(author=user) | community
    )
    return qs.filter(visible)


class DiscussionThreadViewSet(viewsets.ModelViewSet):
    """Forum threads. Filter by ``?course=<id>``, ``?scope=course|community``,
    ``?status=open|resolved``, ``?tag=<slug>``, ``?visibility=``, ``?q=<search>``;
    order with ``?sort=active|votes|new|views``. Any authenticated user can open
    a thread; only the author or an admin can edit/delete it. Reads are limited
    to threads the caller may see (see ``visible_threads``)."""

    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES
    api_roles_by_action = {"vote": ("student", "trainer", "admin")}

    SORTS = {
        "active": ["-is_pinned", "-last_activity_at", "-created_at"],
        "votes": ["-score", "-created_at"],
        "new": ["-created_at"],
        "views": ["-views_count", "-created_at"],
    }

    def get_serializer_class(self):
        if self.action == "retrieve":
            return DiscussionThreadDetailSerializer
        return DiscussionThreadSerializer

    def get_queryset(self):
        qs = (
            visible_threads(self.request.user)
            .select_related("author", "course")
            .prefetch_related("tags", "replies__author")
        )
        params = self.request.query_params
        qs = _filter_by(qs, self.request, "course", "course_id")
        qs = _filter_by(qs, self.request, "scope")
        qs = _filter_by(qs, self.request, "status")
        qs = _filter_by(qs, self.request, "visibility")
        qs = _filter_by(qs, self.request, "tag", "tags__slug")
        if params.get("q"):
            qs = qs.filter(
                Q(title__icontains=params["q"]) | Q(body__icontains=params["q"])
            )
        sort = self.SORTS.get(params.get("sort"))
        return qs.order_by(*sort) if sort else qs

    def retrieve(self, request, *args, **kwargs):
        """Reading a question counts as a view (the list's "340" counter)."""
        thread = self.get_object()
        DiscussionThread.objects.filter(pk=thread.pk).update(
            views_count=F("views_count") + 1
        )
        thread.refresh_from_db(fields=["views_count"])
        return Response(self.get_serializer(thread).data)

    def perform_create(self, serializer):
        thread = serializer.save(
            author=self.request.user, last_activity_at=timezone.now()
        )
        award_points(self.request.user, "ask_question")
        return thread

    @action(detail=True, methods=["post"])
    def vote(self, request, pk=None):
        """Up/down vote a question. Body ``{"value": 1 | -1}``.

        Sending the value you already cast withdraws the vote, so the same
        endpoint powers pressing an arrow and un-pressing it.
        """
        thread = self.get_object()
        value = _vote_value(request)
        if thread.author_id == request.user.id:
            raise ValidationError("You cannot vote on your own question.")
        score, my_vote = apply_vote(request.user, thread, value)
        return Response({"score": score, "my_vote": my_vote})

    def _assert_owner(self, thread):
        user = self.request.user
        if thread.author_id != user.id and not _is_admin(user):
            raise PermissionDenied("You can only modify your own thread.")

    def perform_update(self, serializer):
        self._assert_owner(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._assert_owner(instance)
        instance.delete()


class DiscussionReplyViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Replies to a thread. Filter by ``?thread=<id>``. Creating a reply bumps
    the thread's activity and reply count."""

    serializer_class = DiscussionReplySerializer
    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES

    def get_queryset(self):
        qs = DiscussionReply.objects.select_related("author", "thread")
        return _filter_by(qs, self.request, "thread", "thread_id")

    def perform_create(self, serializer):
        reply = serializer.save(author=self.request.user)
        thread = reply.thread
        DiscussionThread.objects.filter(pk=thread.pk).update(
            reply_count=thread.replies.count(),
            last_activity_at=timezone.now(),
        )

    def _assert_owner(self, reply):
        user = self.request.user
        if reply.author_id != user.id and not _is_admin(user):
            raise PermissionDenied("You can only modify your own reply.")

    def perform_update(self, serializer):
        self._assert_owner(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._assert_owner(instance)
        thread = instance.thread
        instance.delete()
        DiscussionThread.objects.filter(pk=thread.pk).update(
            reply_count=thread.replies.count()
        )

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        """Mark this reply as the accepted answer (thread author, course trainer
        or admin only). Resolves the thread."""
        reply = self.get_object()
        thread = reply.thread
        user = request.user
        is_trainer_owner = (
            thread.course is not None and thread.course.trainer_id == user.id
        )
        if not (thread.author_id == user.id or is_trainer_owner or _is_admin(user)):
            raise PermissionDenied(
                "Only the thread author, course trainer or an admin can accept an answer."
            )
        thread.replies.update(is_accepted_answer=False)
        reply.is_accepted_answer = True
        reply.save(update_fields=["is_accepted_answer", "updated_at"])
        DiscussionThread.objects.filter(pk=thread.pk).update(
            status=DiscussionThread.Status.RESOLVED
        )
        # Stack Overflow pays both sides: the answerer for being right, the
        # asker a smaller amount for closing the loop.
        if reply.author_id != user.id:
            award_points(reply.author, "accepted_answer")
            award_points(user, "accept_answer")
        return Response(self.get_serializer(reply).data)

    @action(detail=True, methods=["post"])
    def vote(self, request, pk=None):
        """Up/down vote an answer. Body ``{"value": 1 | -1}``.

        Downvoting an answer costs the voter a point, as on Stack Overflow —
        withdrawing the downvote refunds it.
        """
        reply = self.get_object()
        value = _vote_value(request)
        if reply.author_id == request.user.id:
            raise ValidationError("You cannot vote on your own answer.")
        score, my_vote = apply_vote(request.user, reply, value)
        return Response({"score": score, "my_vote": my_vote})


class ForumTagViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Tags in use on the forum, with question counts — the ``Python · 42`` bar.

    Only counts threads the caller can actually see, so a tag's number never
    advertises questions they cannot open.
    """

    serializer_class = ForumTagSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES
    pagination_class = None

    def get_queryset(self):
        visible = visible_threads(self.request.user)
        return (
            Tag.objects.filter(threads__in=visible)
            .annotate(thread_count=Count("threads", distinct=True))
            .order_by("-thread_count", "name")
        )


class CommunityPostViewSet(viewsets.ModelViewSet):
    """The community feed. Any authenticated user can post; only the author or an
    admin can edit/delete. ``POST .../{id}/like/`` bumps the like count."""

    serializer_class = CommunityPostSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES

    def get_queryset(self):
        return CommunityPost.objects.select_related("author")

    def perform_create(self, serializer):
        serializer.save(author=self.request.user)

    def _assert_owner(self, post):
        user = self.request.user
        if post.author_id != user.id and not _is_admin(user):
            raise PermissionDenied("You can only modify your own post.")

    def perform_update(self, serializer):
        self._assert_owner(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._assert_owner(instance)
        instance.delete()

    @action(detail=True, methods=["post"])
    def like(self, request, pk=None):
        post = self.get_object()
        CommunityPost.objects.filter(pk=post.pk).update(likes_count=F("likes_count") + 1)
        post.refresh_from_db(fields=["likes_count"])
        return Response({"likes_count": post.likes_count})


class BadgeViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """The catalog of earnable badges (read-only)."""

    queryset = Badge.objects.all()
    serializer_class = BadgeSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES


class CommunityProfileViewSet(viewsets.GenericViewSet):
    """The current user's gamification card (points, level, earned badges)."""

    serializer_class = CommunityProfileSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES

    @action(detail=False, methods=["get"])
    def me(self, request):
        profile, _ = CommunityProfile.objects.get_or_create(user=request.user)
        return Response(self.get_serializer(profile).data)

    @action(detail=False, methods=["get"])
    def my_badges(self, request):
        earned = UserBadge.objects.select_related("badge").filter(user=request.user)
        return Response(UserBadgeSerializer(earned, many=True).data)

    @action(detail=False, methods=["get"])
    def leaderboard(self, request):
        """Top contributors by reputation — the sidebar and its "full" page.

        ``?limit=`` (default 5, max 100) and ``?scope=community`` to rank only
        the caller's own organization. Includes the caller's own rank so the
        UI can show "you are #14" without paging to find them.
        """
        try:
            limit = min(int(request.query_params.get("limit", 5)), 100)
        except (TypeError, ValueError):
            limit = 5

        qs = CommunityProfile.objects.select_related("user").filter(points__gt=0)
        if request.query_params.get("scope") == "community":
            if request.user.organization_id is None:
                qs = qs.filter(user=request.user)
            else:
                qs = qs.filter(user__organization_id=request.user.organization_id)
        qs = qs.order_by("-points", "user_id")

        top = LeaderboardEntrySerializer(qs[:limit], many=True).data
        for position, entry in enumerate(top, start=1):
            entry["rank"] = position

        mine = qs.filter(user=request.user).first()
        my_rank = qs.filter(points__gt=mine.points).count() + 1 if mine else None
        return Response(
            {
                "results": top,
                "my_rank": my_rank,
                "my_points": mine.points if mine else 0,
            }
        )
