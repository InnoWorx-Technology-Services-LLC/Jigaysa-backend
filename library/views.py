"""Free / premium learning library API (PRD §3.10).

Students browse and search the library (keyword, category, format, trainer,
popularity) and bookmark items. Trainers/admins author resources. Reads are open
to any authenticated user; premium items expose metadata to everyone but the
gated ``file_url``/``video_url`` are only returned to premium/enrolled users is
left to a later phase — for now access_level is advisory and surfaced as a field.
"""

from django.db.models import F, Q
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from library.models import LibraryBookmark, LibraryResource
from library.serializers import (
    LibraryBookmarkSerializer,
    LibraryResourceSerializer,
)

ALL_ROLES = ("student", "trainer", "admin", "institution")
AUTHOR_WRITE = ("trainer", "admin")


def _filter_by(qs, request, param, field=None):
    value = request.query_params.get(param)
    if value:
        qs = qs.filter(**{field or param: value})
    return qs


def _is_author_role(user):
    return getattr(user, "role", None) in AUTHOR_WRITE


class LibraryResourceViewSet(viewsets.ModelViewSet):
    """The training library catalog.

    Filters (query params): ``format``, ``category`` (id), ``access_level``,
    ``author`` (trainer id), ``course`` (id), ``q`` (title/description search).
    Ordering via ``ordering`` among ``popularity_score``, ``published_at``,
    ``views_count``, ``created_at`` (prefix ``-`` for descending; default is the
    model's popularity ordering).
    """

    serializer_class = LibraryResourceSerializer
    lookup_field = "slug"
    api_roles = ALL_ROLES
    api_roles_by_action = {
        "list": ("public",),
        "retrieve": ("public",),
        "create": AUTHOR_WRITE,
        "update": AUTHOR_WRITE,
        "partial_update": AUTHOR_WRITE,
        "destroy": AUTHOR_WRITE,
    }

    def get_permissions(self):
        # The free library is public by definition (PRD §3.10), so browsing it
        # must work logged-out. Bookmarking and authoring stay authenticated.
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAuthenticated()]

    def _assert_can_author(self, resource=None):
        user = self.request.user
        if not _is_author_role(user):
            raise PermissionDenied("Only trainers or admins can manage library resources.")
        if (
            resource is not None
            and resource.author_id != user.id
            and getattr(user, "role", None) != "admin"
        ):
            raise PermissionDenied("You do not own this resource.")

    def get_queryset(self):
        qs = LibraryResource.objects.select_related("author", "category", "course")
        params = self.request.query_params
        qs = _filter_by(qs, self.request, "format")
        qs = _filter_by(qs, self.request, "category", "category_id")
        qs = _filter_by(qs, self.request, "access_level")
        qs = _filter_by(qs, self.request, "author", "author_id")
        qs = _filter_by(qs, self.request, "course", "course_id")
        if params.get("q"):
            term = params["q"]
            qs = qs.filter(
                Q(title__icontains=term) | Q(description__icontains=term)
            )
        ordering = params.get("ordering")
        allowed = {
            "popularity_score", "-popularity_score",
            "published_at", "-published_at",
            "views_count", "-views_count",
            "created_at", "-created_at",
        }
        if ordering in allowed:
            qs = qs.order_by(ordering)
        return qs

    def perform_create(self, serializer):
        self._assert_can_author()
        serializer.save(author=self.request.user)

    def perform_update(self, serializer):
        self._assert_can_author(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._assert_can_author(instance)
        instance.delete()

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        # Count the view (PRD §3.10 popularity ranking) without racing.
        LibraryResource.objects.filter(pk=instance.pk).update(
            views_count=F("views_count") + 1,
            popularity_score=F("popularity_score") + 1,
        )
        instance.refresh_from_db(fields=["views_count", "popularity_score"])
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def bookmark(self, request, slug=None):
        """Toggle the current user's bookmark on this resource."""
        resource = self.get_object()
        existing = LibraryBookmark.objects.filter(
            user=request.user, resource=resource
        ).first()
        if existing:
            existing.delete()
            return Response({"bookmarked": False})
        LibraryBookmark.objects.create(user=request.user, resource=resource)
        return Response({"bookmarked": True}, status=status.HTTP_201_CREATED)


class LibraryBookmarkViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """The current user's saved library items (Library "Saved" tab)."""

    serializer_class = LibraryBookmarkSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES

    def get_queryset(self):
        return LibraryBookmark.objects.select_related(
            "resource", "resource__author", "resource__category"
        ).filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource = serializer.validated_data["resource"]
        bookmark, _ = LibraryBookmark.objects.get_or_create(
            user=request.user, resource=resource
        )
        out = self.get_serializer(bookmark)
        return Response(out.data, status=status.HTTP_201_CREATED)
