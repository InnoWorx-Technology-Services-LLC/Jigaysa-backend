"""The admin console's **Institutions** page (PRD §2.4 multi-tenancy).

Until now an ``Organization`` could only be created in Django admin, and the
link between a user and their institution was settable nowhere but there — so
onboarding one institution meant three visits to a screen meant for developers,
and the platform has had zero organisations since launch as a result.

Two objects, deliberately kept apart:

* **The organisation** — the tenant record. Courses, batches, classrooms and
  the community feed all scope by it.
* **Its members** — ordinary users pointed at it by ``User.organization``.
  Being a member is not a role: a student at a college and the administrator
  who bought the seats are both members, and only the second has
  ``role="institution"``.

Conflating those two is the mistake this module exists to avoid. Adding someone
to an institution never changes what they are allowed to do; that is the role
endpoint's job, and it stays a separate, deliberate call.
"""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.utils.text import slugify
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.models import Organization
from core.pagination import DefaultPagination
from core.permissions import IsAdmin
from core.serializers import (
    OrganizationMemberSerializer,
    OrganizationOnboardSerializer,
    OrganizationSerializer,
    OrganizationWriteSerializer,
)

User = get_user_model()


def unique_slug(name, exclude_pk=None) -> str:
    """A slug that is free, suffixing a counter when it is not.

    ``Organization.save()`` derives a slug from the name with no de-duplication
    and the column is unique — so two institutions genuinely called "St. Xavier
    College" would raise an IntegrityError and surface as a 500. Generating the
    slug here, explicitly, means the model's fallback never fires and a second
    identical name is a normal thing that works.
    """
    base = slugify(name) or "organization"
    candidate, n = base, 2
    taken = Organization.objects.exclude(pk=exclude_pk) if exclude_pk else Organization.objects.all()
    while taken.filter(slug=candidate).exists():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


class AdminOrganizationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Institutions and corporate clients: list, create, edit, deactivate.

    Paginated by the platform default. Rows carry their member and course
    counts, annotated in one query — an institutions table exists to answer
    "how big is this one", and a count per row would be a query per row.
    """

    permission_classes = [IsAdmin]
    api_roles = ("admin",)

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return OrganizationWriteSerializer
        return OrganizationSerializer

    def get_queryset(self):
        queryset = Organization.objects.annotate(
            member_count=Count("members", distinct=True),
            course_count=Count("courses", distinct=True),
        ).order_by("name")

        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(slug__icontains=search)
            )

        org_type = self.request.query_params.get("type", "").strip()
        if org_type in dict(Organization.OrgType.choices):
            queryset = queryset.filter(type=org_type)

        state = self.request.query_params.get("status", "").strip().lower()
        if state == "active":
            queryset = queryset.filter(is_active=True)
        elif state in ("inactive", "archived"):
            queryset = queryset.filter(is_active=False)

        return queryset

    @extend_schema(
        parameters=[
            OpenApiParameter("search", str, OpenApiParameter.QUERY,
                             description="Matches name or slug."),
            OpenApiParameter("type", str, OpenApiParameter.QUERY,
                             description="institution | corporate"),
            OpenApiParameter("status", str, OpenApiParameter.QUERY,
                             description="active | inactive"),
        ],
        responses=OrganizationSerializer(many=True),
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def _read(self, instance):
        """Re-read through the annotated queryset so writes answer in the same
        shape as reads — a create that returns a row missing the counts the
        table renders forces the client to refetch immediately."""
        return OrganizationSerializer(
            self.get_queryset().get(pk=instance.pk)
        ).data

    @extend_schema(
        request=OrganizationWriteSerializer, responses=OrganizationSerializer
    )
    def create(self, request, *args, **kwargs):
        body = self.get_serializer(data=request.data)
        body.is_valid(raise_exception=True)
        org = body.save(slug=unique_slug(body.validated_data["name"]))
        return Response(self._read(org), status=status.HTTP_201_CREATED)

    @extend_schema(
        request=OrganizationWriteSerializer, responses=OrganizationSerializer
    )
    def update(self, request, *args, **kwargs):
        org = self.get_object()
        body = self.get_serializer(
            org, data=request.data, partial=kwargs.pop("partial", False)
        )
        body.is_valid(raise_exception=True)
        # Renaming does not re-slug. The slug is in URLs and any link already
        # shared would break; a rename is a label change, not a new tenant.
        body.save()
        return Response(self._read(org))

    @extend_schema(responses={204: None})
    def destroy(self, request, *args, **kwargs):
        """Deactivate. Institutions are never deleted.

        Courses, batches, classrooms and community scoping all hang off this
        row, and members point at it. Removing it would either cascade through
        somebody's course catalogue or quietly orphan every member. Deactivating
        keeps the history and takes it out of the active list — the same choice
        the Users page makes with suspension, so the console has one rule rather
        than two.
        """
        org = self.get_object()
        if org.is_active:
            org.is_active = False
            org.save(update_fields=["is_active", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=None, responses=OrganizationSerializer)
    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """Undo a deactivation."""
        org = self.get_object()
        if not org.is_active:
            org.is_active = True
            org.save(update_fields=["is_active", "updated_at"])
        return Response(self._read(org))

    # --- members ---------------------------------------------------------- #

    @extend_schema(
        parameters=[
            OpenApiParameter("role", str, OpenApiParameter.QUERY,
                             description="Filter members by role."),
        ],
        responses=OrganizationMemberSerializer(many=True),
    )
    @action(detail=True, methods=["get"])
    def members(self, request, pk=None):
        """GET ``/admin/organizations/{id}/members/`` — who belongs to it.

        **Paginated.** An institution's roll grows without bound; this is the
        list on the page that must never come back whole.
        """
        org = self.get_object()
        people = User.objects.filter(organization=org).order_by(
            "role", "full_name", "email"
        )
        role = request.query_params.get("role", "").strip()
        if role:
            people = people.filter(role=role)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(people, request, view=self)
        return paginator.get_paginated_response(
            OrganizationMemberSerializer(page, many=True).data
        )

    @extend_schema(
        request=OrganizationMemberSerializer,
        responses=OrganizationMemberSerializer(many=True),
    )
    @action(detail=True, methods=["post"], url_path="members/add")
    def add_members(self, request, pk=None):
        """POST ``/admin/organizations/{id}/members/add/`` — attach users.

        Body: ``{"users": [12, 19]}``.

        **This does not change anyone's role.** Membership says which tenant
        someone belongs to; role says what they may do. A student joining a
        college is still a student, and promoting their administrator is a
        separate, deliberate call to the role endpoint — bundling the two here
        would make "add 400 students" silently able to mint 400 institution
        accounts.

        Moving a user who already belongs to another institution is allowed and
        is a plain reassignment; it is reported back so the console can say so.
        """
        org = self.get_object()
        ids = request.data.get("users") or []
        if not isinstance(ids, list) or not ids:
            return Response(
                {"detail": "Send a non-empty \"users\" list of user ids."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        people = list(User.objects.filter(pk__in=ids))
        found = {u.pk for u in people}
        missing = [i for i in ids if i not in found]
        if missing:
            return Response(
                {"detail": f"No such user(s): {', '.join(str(m) for m in missing)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        moved = [
            u.email for u in people
            if u.organization_id not in (None, org.pk)
        ]
        User.objects.filter(pk__in=found).update(organization=org)

        return Response(
            {
                "added": len(found),
                "reassigned_from_another_organization": moved,
                "members": OrganizationMemberSerializer(
                    User.objects.filter(pk__in=found).order_by("email"), many=True
                ).data,
            }
        )

    @extend_schema(request=None, responses={204: None})
    @action(
        detail=True,
        methods=["delete"],
        url_path=r"members/(?P<user_id>\d+)",
    )
    def remove_member(self, request, pk=None, user_id=None):
        """DELETE ``/admin/organizations/{id}/members/{user_id}/`` — detach.

        Clears the link only. The account keeps its role, its enrolments and its
        history — leaving an institution is not leaving the platform, and a
        student whose college stops paying still owns the courses they took.
        """
        org = self.get_object()
        updated = User.objects.filter(pk=user_id, organization=org).update(
            organization=None
        )
        if not updated:
            return Response(
                {"detail": "That user is not a member of this organization."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    # --- one-call onboarding ------------------------------------------------ #

    @extend_schema(
        request=OrganizationOnboardSerializer,
        responses={201: OrganizationSerializer},
    )
    @action(detail=False, methods=["post"])
    def onboard(self, request):
        """POST ``/admin/organizations/onboard/`` — create a tenant and its
        admin's login in a single call.

        The general flow is three separate calls (create org, promote a user
        to ``institution``, attach them — see the module docstring), because
        membership and role are deliberately independent decisions and the
        user being promoted might already exist. This endpoint is the shortcut
        for the common case: a brand-new institution whose admin has no
        account yet. It always creates a new user — attaching an existing one,
        or a second admin, still goes through ``members/add/`` and the role
        endpoint.

        Atomic: a validation failure on either half leaves neither the org nor
        the user behind.
        """
        from accounts.models import Role  # local: avoid an app-load cycle

        body = OrganizationOnboardSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        with transaction.atomic():
            org = Organization.objects.create(
                name=data["name"],
                type=data["type"],
                slug=unique_slug(data["name"]),
            )
            admin_user = User.objects.create_user(
                email=data["admin_email"],
                password=data["admin_password"],
                full_name=data["admin_full_name"],
                phone=data.get("admin_phone", ""),
                role=Role.INSTITUTION,
                organization=org,
            )

        return Response(
            {
                "organization": self._read(org),
                "admin": OrganizationMemberSerializer(admin_user).data,
            },
            status=status.HTTP_201_CREATED,
        )
