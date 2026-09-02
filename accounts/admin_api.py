"""The admin console's **Users** page (PRD §3.1 role-based access).

Everything on one screen: the trainer application queue, four counters, and a
searchable, filterable, **paginated** roster with per-row role changes and
suspension.

Kept out of ``accounts.views`` because the audience is different. That module is
a user acting on themselves — register, log in, edit my own profile. This one is
an admin acting on *other people's* accounts, which needs a different permission
posture and a different set of things it refuses to do.

### Refusing to lock the platform out of itself

Two moves are rejected outright rather than merely discouraged:

* **You cannot suspend yourself.** One misclick would end the session that was
  about to undo it.
* **You cannot demote yourself.** The same failure, slower — you keep the
  session but lose the page.

They are `409`s, not `403`s: the caller has every right to the action, the
platform's state is what makes it a bad idea.

:func:`_last_admin` backs both up. Note that it is a **backstop, not a rule an
admin will meet in normal use**: an active admin acting on a *different* admin
always leaves themselves behind, so the target is never the last one, and an
admin acting on themselves is caught by the self-rules above first. It earns its
keep only where those do not reach — a session that outlives its own
suspension, a fixture, a shell — which is exactly when the guard matters and
nothing else is watching.
"""

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.models import Role, TrainerProfile
from accounts.serializers import (
    AdminUserSerializer,
    RoleChangeSerializer,
    SuspendSerializer,
    UserStatsSerializer,
)
from core.permissions import IsAdmin
from notifications.models import NotificationCategory
from notifications.services import notify

User = get_user_model()

#: Roles the console may assign. ``student`` is included so a promotion can be
#: undone — a "Make Trainer" with no way back is a one-way door.
ASSIGNABLE_ROLES = (Role.STUDENT, Role.TRAINER, Role.ADMIN, Role.INSTITUTION)


def _last_admin(user) -> bool:
    """True when removing this account would leave the platform with no admin."""
    if user.role != Role.ADMIN:
        return False
    return not (
        User.objects.filter(role=Role.ADMIN, is_active=True)
        .exclude(pk=user.pk)
        .exists()
    )


class AdminUserViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """The Users roster: search, filter, change role, suspend.

    Paginated by the platform default (20 per page, `?page_size=` up to 100).
    The roster is the one list here that grows without bound — every account
    ever created — so it is the one that must never be returned whole.
    """

    serializer_class = AdminUserSerializer
    permission_classes = [IsAdmin]
    api_roles = ("admin",)

    def get_queryset(self):
        queryset = User.objects.select_related("organization").order_by(
            "-created_at"
        )

        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(full_name__icontains=search) | Q(email__icontains=search)
            )

        role = self.request.query_params.get("role", "").strip()
        if role and role in dict(Role.choices):
            queryset = queryset.filter(role=role)

        state = self.request.query_params.get("status", "").strip().lower()
        if state == "active":
            queryset = queryset.filter(is_active=True)
        elif state in ("suspended", "inactive"):
            queryset = queryset.filter(is_active=False)

        return queryset

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "search", str, OpenApiParameter.QUERY,
                description="Matches name or email, case-insensitive.",
            ),
            OpenApiParameter(
                "role", str, OpenApiParameter.QUERY,
                description="student | trainer | admin | institution",
            ),
            OpenApiParameter(
                "status", str, OpenApiParameter.QUERY,
                description="active | suspended",
            ),
        ],
        responses=AdminUserSerializer(many=True),
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(responses=UserStatsSerializer)
    @action(detail=False, methods=["get"])
    def stats(self, request):
        """The four counters above the table.

        One grouped query rather than four counts — the numbers are read
        together and shown together, so they should also be *true* together.
        Four separate queries can disagree with each other if a signup lands
        between them.
        """
        by_role = {
            row["role"]: row["n"]
            for row in User.objects.values("role").annotate(n=Count("id"))
        }
        totals = User.objects.aggregate(
            total=Count("id"),
            suspended=Count("id", filter=Q(is_active=False)),
        )
        return Response(
            UserStatsSerializer(
                {
                    "total_users": totals["total"],
                    "trainers": by_role.get(Role.TRAINER, 0),
                    "students": by_role.get(Role.STUDENT, 0),
                    "institutions": by_role.get(Role.INSTITUTION, 0),
                    "admins": by_role.get(Role.ADMIN, 0),
                    "suspended": totals["suspended"],
                }
            ).data
        )

    @extend_schema(request=RoleChangeSerializer, responses=AdminUserSerializer)
    @action(detail=True, methods=["patch"], url_path="role")
    def change_role(self, request, pk=None):
        """The "Make Trainer / Make Admin / Make Institution" menu.

        Promoting to trainer creates the teaching profile immediately, but
        leaves it **unapproved** — appointing someone a trainer and listing them
        as a bookable mentor are two different decisions, and the mentor list
        should not gain a name nobody reviewed.
        """
        target = self.get_object()
        body = RoleChangeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        new_role = body.validated_data["role"]

        if target.pk == request.user.pk and new_role != target.role:
            return Response(
                {"detail": "You can't change your own role — you'd lose access "
                           "to this page. Ask another admin."},
                status=status.HTTP_409_CONFLICT,
            )
        if new_role != Role.ADMIN and _last_admin(target):
            return Response(
                {"detail": "This is the only active admin. Promote someone else "
                           "first, or there will be nobody who can."},
                status=status.HTTP_409_CONFLICT,
            )

        if target.role == new_role:
            return Response(self.get_serializer(target).data)

        was = target.get_role_display()
        target.role = new_role
        target.save(update_fields=["role", "updated_at"])

        if new_role == Role.TRAINER:
            TrainerProfile.objects.get_or_create(user=target)

        notify(
            target,
            NotificationCategory.SYSTEM,
            title="Your account role changed",
            body=f"An admin changed your role from {was} to "
                 f"{target.get_role_display()}.",
            link="/",
        )
        return Response(self.get_serializer(target).data)

    @extend_schema(request=SuspendSerializer, responses=AdminUserSerializer)
    @action(detail=True, methods=["post"])
    def suspend(self, request, pk=None):
        """Block sign-in without deleting anything.

        Suspension is ``is_active=False``, which is what the auth backend
        already checks — so it takes effect on the next login attempt, not
        mid-session. An admin expecting someone kicked out *now* should be told
        that; it is a lock on the door, not a bouncer.
        """
        target = self.get_object()
        body = SuspendSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        if target.pk == request.user.pk:
            return Response(
                {"detail": "You can't suspend your own account."},
                status=status.HTTP_409_CONFLICT,
            )
        if _last_admin(target):
            return Response(
                {"detail": "This is the only active admin. Suspending them would "
                           "leave nobody who can undo it."},
                status=status.HTTP_409_CONFLICT,
            )

        target.is_active = False
        target.save(update_fields=["is_active", "updated_at"])
        notify(
            target,
            NotificationCategory.SYSTEM,
            title="Your account has been suspended",
            body=body.validated_data.get("reason")
            or "Contact support if you think this is a mistake.",
            link="/",
        )
        return Response(self.get_serializer(target).data)

    @extend_schema(request=None, responses=AdminUserSerializer)
    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):
        """Undo a suspension."""
        target = self.get_object()
        if target.is_active:
            return Response(self.get_serializer(target).data)

        target.is_active = True
        target.save(update_fields=["is_active", "updated_at"])
        notify(
            target,
            NotificationCategory.SYSTEM,
            title="Your account is active again",
            body="You can sign in as usual.",
            link="/",
        )
        return Response(self.get_serializer(target).data)
