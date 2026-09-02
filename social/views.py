"""Connect-page endpoints, then the Promote wizard.

The round-trip:

1. ``POST /social/connect/{provider}/`` → an ``authorize_url`` to send the
   browser to. The signed ``state`` rides along.
2. The trainer consents at the network, which redirects to
   ``GET /social/callback/{provider}/``.
3. The callback verifies ``state``, exchanges the code, upserts every
   destination it found, and **302s back to the frontend** with the outcome in
   the query string.
4. ``DELETE /social/accounts/{id}/`` revokes upstream, then deletes the row.

Only step 3 is unauthenticated, because a browser redirect cannot carry a JWT.
See ``social.oauth`` for why ``state`` is trustworthy enough to stand in.

Below that, the wizard: templates, the rewrite helper, and campaigns. Those own
no publishing logic of their own — ``social.publishing`` does, so a campaign
created inline and one picked up by the cron sweep take the same path.
"""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import redirect
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.pagination import DefaultPagination
from core.permissions import HasRole
from courses.models import Course
from social import oauth, promotions, providers, publishing
from social.models import Campaign, CampaignPost, Provider, SocialAccount
from social.providers.base import (
    OAuthDenied,
    ProviderError,
    ProviderNotConfigured,
)
from social.serializers import (
    CampaignCreateSerializer,
    CampaignSerializer,
    CampaignUpdateSerializer,
    ConnectRequestSerializer,
    ConnectResponseSerializer,
    PromoteContextSerializer,
    ProviderSerializer,
    RewriteRequestSerializer,
    RewriteResponseSerializer,
    SocialAccountSerializer,
)

User = get_user_model()

#: Trainers own their connections; admins can see and clean up any of them.
TRAINER_OR_ADMIN = HasRole("trainer", "admin")


def _not_configured(provider):
    return Response(
        {
            "detail": f"{Provider(provider).label} is not configured on this "
            "server. Set the client credentials in the environment first."
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


class SocialAccountListView(APIView):
    """GET ``/social/accounts/`` — every provider card, with this trainer's state.

    One request renders the whole page. Providers with no adapter come back
    ``available: false`` rather than being omitted, so the frontend never keeps
    its own copy of the list.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = ProviderSerializer
    api_roles = ("trainer", "admin")

    @extend_schema(responses=ProviderSerializer(many=True))
    def get(self, request):
        mine = SocialAccount.objects.filter(user=request.user)
        by_provider = {}
        for account in mine:
            by_provider.setdefault(account.provider, []).append(account)

        cards = [
            {
                "provider": provider,
                "label": Provider(provider).label,
                "available": providers.adapter_for(provider) is not None,
                "configured": providers.is_configured(provider),
                "accounts": by_provider.get(provider, []),
            }
            for provider in providers.DISPLAY_ORDER
        ]
        return Response(ProviderSerializer(cards, many=True).data)


class SocialConnectView(APIView):
    """POST ``/social/connect/{provider}/`` — start the round-trip.

    Returns the URL to send the browser to. Nothing is written yet: an
    abandoned consent screen leaves no row behind.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = ConnectRequestSerializer
    api_roles = ("trainer", "admin")

    @extend_schema(
        request=ConnectRequestSerializer, responses=ConnectResponseSerializer
    )
    def post(self, request, provider):
        adapter = providers.adapter_for(provider)
        if adapter is None:
            return Response(
                {"detail": f"Connecting {provider} isn't supported yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not adapter.is_configured():
            return _not_configured(provider)

        body = ConnectRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        state = oauth.make_state(
            request.user.id, provider, body.validated_data.get("return_to", "")
        )
        url = adapter.authorize_url(state, oauth.callback_url(request, provider))
        return Response(
            {
                "authorize_url": url,
                "provider": provider,
                "expires_in": oauth.STATE_MAX_AGE,
            }
        )


class SocialCallbackView(APIView):
    """GET ``/social/callback/{provider}/`` — where the network sends the browser.

    Always ends in a redirect to the frontend, never a JSON error: the caller
    here is a browser mid-navigation, and showing it a DRF error page would
    strand the trainer on an API URL. Failures come back as ``?error=``.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    api_roles = ("public",)

    @extend_schema(
        parameters=[
            OpenApiParameter("code", str, OpenApiParameter.QUERY),
            OpenApiParameter("state", str, OpenApiParameter.QUERY),
            OpenApiParameter("error", str, OpenApiParameter.QUERY),
        ],
        responses={302: None},
    )
    def get(self, request, provider):
        state_raw = request.query_params.get("state", "")

        # Read state first, even on the denial path: it carries return_to, and
        # sending someone who cancelled back to the wizard beats dumping them
        # on the settings page.
        try:
            state = oauth.read_state(state_raw)
        except oauth.InvalidState as exc:
            return redirect(oauth.frontend_redirect("", error=str(exc)))

        return_to = state["return_to"]

        if request.query_params.get("error"):
            reason = request.query_params.get(
                "error_description"
            ) or "The connection was cancelled."
            return redirect(
                oauth.frontend_redirect(return_to, provider=provider, error=reason)
            )

        if state["provider"] != provider:
            return redirect(
                oauth.frontend_redirect(
                    return_to, error="That sign-in link was for another network."
                )
            )

        code = request.query_params.get("code", "")
        if not code:
            return redirect(
                oauth.frontend_redirect(
                    return_to, provider=provider,
                    error="The network did not return an authorization code.",
                )
            )

        user = User.objects.filter(pk=state["user_id"], is_active=True).first()
        if user is None:
            return redirect(
                oauth.frontend_redirect(return_to, error="That account is no longer active.")
            )

        adapter = providers.adapter_for(provider)
        if adapter is None or not adapter.is_configured():
            return redirect(
                oauth.frontend_redirect(
                    return_to, provider=provider,
                    error=f"{Provider(provider).label} is not configured on this server.",
                )
            )

        try:
            found = adapter.exchange_code(
                code, oauth.callback_url(request, provider)
            )
        except (ProviderNotConfigured, OAuthDenied, ProviderError) as exc:
            return redirect(
                oauth.frontend_redirect(return_to, provider=provider, error=str(exc))
            )

        saved = self._store(user, found)
        return redirect(
            oauth.frontend_redirect(
                return_to,
                connected=",".join(sorted({a.provider for a in saved})) or provider,
                accounts=str(len(saved)),
            )
        )

    @staticmethod
    @transaction.atomic
    def _store(user, found):
        """Upsert every destination the exchange returned.

        Reconnecting must refresh the existing row rather than add a second one
        — the unique constraint says so, and a trainer who reconnects to fix an
        expired token expects their campaigns to keep pointing somewhere real.
        Re-linking also clears ``last_error`` and puts the row back to
        ``connected``, which is the whole point of pressing the button.
        """
        saved = []
        for item in found:
            account, _ = SocialAccount.objects.update_or_create(
                user=user,
                provider=item.provider,
                provider_account_id=item.provider_account_id,
                defaults={
                    "access_token": item.access_token,
                    "refresh_token": item.refresh_token,
                    "token_expires_at": item.expires_at,
                    "display_name": item.display_name,
                    "handle": item.handle,
                    "avatar_url": item.avatar_url,
                    "scopes": item.scopes,
                    "provider_meta": item.meta,
                    "status": SocialAccount.Status.CONNECTED,
                    "last_error": "",
                },
            )
            saved.append(account)
        return saved


class SocialAccountDetailView(APIView):
    """DELETE ``/social/accounts/{id}/`` — disconnect one destination."""

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = SocialAccountSerializer
    api_roles = ("trainer", "admin")

    def _get_object(self, request, pk):
        queryset = SocialAccount.objects.all()
        if getattr(request.user, "role", None) != "admin":
            queryset = queryset.filter(user=request.user)
        return queryset.filter(pk=pk).first()

    @extend_schema(responses={204: None})
    def delete(self, request, pk):
        account = self._get_object(request, pk)
        if account is None:
            return Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )

        adapter = providers.adapter_for(account.provider)
        if adapter is not None and hasattr(adapter, "revoke"):
            try:
                adapter.revoke(account)
            except (ProviderNotConfigured, ProviderError):
                # Upstream cleanup is a courtesy, not a precondition. Refusing
                # to disconnect because the network is unreachable would trap a
                # trainer with a connection they have already decided to drop.
                pass

        account.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# The Promote wizard
#
# Steps 1 and 2 are one GET; step 3 is the account list above; step 4 is the
# POST that creates the campaign. Nothing is written until the trainer presses
# Publish or Schedule, which is the same promise the connect flow makes about
# an abandoned consent screen.
# --------------------------------------------------------------------------- #


def _course_for(request, slug):
    """The trainer's own course, or ``None``. Admins may promote any course."""
    queryset = Course.objects.select_related("trainer")
    if getattr(request.user, "role", None) != "admin":
        queryset = queryset.filter(trainer=request.user)
    return queryset.filter(slug=slug).first()


class PromotionTemplateListView(APIView):
    """GET ``/social/templates/?course=<slug>`` — steps 1 and 2 in one call.

    Returns all five cards with their copy already rendered from the course, so
    moving from Template to Edit & preview costs no round-trip and the textarea
    is never briefly empty.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = PromoteContextSerializer
    api_roles = ("trainer", "admin")

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "course", str, OpenApiParameter.QUERY, required=True,
                description="Course slug to render the templates for.",
            )
        ],
        responses=PromoteContextSerializer,
    )
    def get(self, request):
        slug = request.query_params.get("course", "")
        if not slug:
            return Response(
                {"detail": "Pass ?course=<slug> to render the templates."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        course = _course_for(request, slug)
        if course is None:
            return Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )

        facts = promotions.course_facts(course)
        return Response(
            PromoteContextSerializer(
                {
                    "course": {
                        "slug": course.slug,
                        "title": course.title,
                        "thumbnail": facts.image_url,
                        "link_url": facts.link_url,
                    },
                    "templates": promotions.render_all(facts),
                    "image_options": promotions.image_options(facts),
                }
            ).data
        )


class PromotionRewriteView(APIView):
    """POST ``/social/rewrite/`` — the Rewrite button in step 2.

    Rule-based, not generated: hashtag placement, the caption ceiling, and what
    happens to the link. See ``social.promotions.rewrite`` — there is no model
    behind this and the response should not be presented as if there were.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = RewriteRequestSerializer
    api_roles = ("trainer", "admin")

    @extend_schema(
        request=RewriteRequestSerializer, responses=RewriteResponseSerializer
    )
    def post(self, request):
        body = RewriteRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        provider = data["provider"]

        return Response(
            {
                "provider": provider,
                "caption": promotions.rewrite(data["caption"], provider),
                "preview": promotions.compose(
                    data["caption"],
                    data.get("hashtags", ""),
                    data.get("link_url", ""),
                    provider,
                ),
                "limit": promotions.CAPTION_LIMIT.get(
                    provider, promotions.DEFAULT_LIMIT
                ),
            }
        )


class CampaignListCreateView(APIView):
    """GET/POST ``/social/campaigns/`` — the Promotions tab, and step 4.

    A POST with no ``publish_at`` publishes inline and answers with what
    actually landed, per destination. That call is as slow as the networks are
    — several seconds is normal — so the wizard should keep its spinner up.
    Anything the request could not finish stays ``pending`` and is picked up by
    the sweep, so a timeout delays a post rather than losing it.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = CampaignSerializer
    api_roles = ("trainer", "admin")

    def _queryset(self, request):
        queryset = Campaign.objects.select_related("course").prefetch_related("posts")
        if getattr(request.user, "role", None) != "admin":
            queryset = queryset.filter(trainer=request.user)
        return queryset

    @extend_schema(
        parameters=[
            OpenApiParameter("course", str, OpenApiParameter.QUERY),
            OpenApiParameter("status", str, OpenApiParameter.QUERY),
        ],
        responses=CampaignSerializer(many=True),
    )
    def get(self, request):
        """Campaign history, **paginated** like every other list on the platform.

        A trainer accumulates a campaign per promotion for the life of their
        account, so this is a list that only grows. It used to return a bare
        array capped at 100, which is the failure that hides rather than
        breaks — the page looks right until the hundred-and-first campaign
        quietly stops appearing.
        """
        queryset = self._queryset(request)
        course = request.query_params.get("course")
        if course:
            queryset = queryset.filter(course__slug=course)
        state = request.query_params.get("status")
        if state:
            queryset = queryset.filter(status=state)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(
            CampaignSerializer(page, many=True).data
        )

    @extend_schema(request=CampaignCreateSerializer, responses=CampaignSerializer)
    def post(self, request):
        body = CampaignCreateSerializer(
            data=request.data, context={"request": request}
        )
        body.is_valid(raise_exception=True)
        data = body.validated_data

        with transaction.atomic():
            campaign = Campaign.objects.create(
                trainer=request.user,
                course=data["course"],
                template=data["template"],
                caption=data["caption"],
                hashtags=data.get("hashtags", ""),
                image_source=data["image_source"],
                image_url=data["_image_url"],
                link_url=data["_link_url"],
                scheduled_for=data.get("publish_at"),
                status=Campaign.Status.SCHEDULED,
            )
            publishing.build_posts(campaign, data["accounts"])

        if campaign.scheduled_for is None:
            campaign = publishing.run_campaign(campaign)

        campaign = (
            self._queryset(request).filter(pk=campaign.pk).first() or campaign
        )
        return Response(
            CampaignSerializer(campaign).data, status=status.HTTP_201_CREATED
        )


class CampaignDetailView(APIView):
    """GET/PATCH/DELETE ``/social/campaigns/{id}/``.

    PATCH edits copy or moves the time; DELETE cancels. Both only while the
    campaign is still scheduled — once a post has reached a network the copy is
    public, and editing our row would only make the record disagree with what
    people can already read.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = CampaignSerializer
    api_roles = ("trainer", "admin")

    def _get_object(self, request, pk):
        queryset = Campaign.objects.select_related("course").prefetch_related("posts")
        if getattr(request.user, "role", None) != "admin":
            queryset = queryset.filter(trainer=request.user)
        return queryset.filter(pk=pk).first()

    @extend_schema(responses=CampaignSerializer)
    def get(self, request, pk):
        campaign = self._get_object(request, pk)
        if campaign is None:
            return Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(CampaignSerializer(campaign).data)

    @extend_schema(request=CampaignUpdateSerializer, responses=CampaignSerializer)
    def patch(self, request, pk):
        campaign = self._get_object(request, pk)
        if campaign is None:
            return Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )
        if campaign.status not in Campaign.EDITABLE_STATUSES:
            return Response(
                {
                    "detail": "This campaign has already started publishing and "
                    "can no longer be edited."
                },
                status=status.HTTP_409_CONFLICT,
            )

        body = CampaignUpdateSerializer(data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        fields = []
        for key in ("caption", "hashtags"):
            if key in data:
                setattr(campaign, key, data[key])
                fields.append(key)
        if "publish_at" in data:
            campaign.scheduled_for = data["publish_at"]
            fields.append("scheduled_for")
        if fields:
            campaign.save(update_fields=fields + ["updated_at"])

        # Moving a schedule to "now" is a publish, not just an edit.
        if "publish_at" in data and campaign.scheduled_for is None:
            campaign = publishing.run_campaign(campaign)

        campaign = self._get_object(request, pk) or campaign
        return Response(CampaignSerializer(campaign).data)

    @extend_schema(responses={204: None})
    def delete(self, request, pk):
        """Cancel a scheduled campaign.

        Cancels rather than deletes: the posts that already went out are the
        trainer's own publishing history, and a row that quietly vanishes takes
        the permalinks with it. A campaign with nothing published is removed
        outright, because there is no history to keep.
        """
        campaign = self._get_object(request, pk)
        if campaign is None:
            return Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )

        published = campaign.posts.filter(
            status=CampaignPost.Status.PUBLISHED
        ).exists()
        if published:
            return Response(
                {
                    "detail": "Part of this campaign is already public and can't "
                    "be unpublished from here. Delete the post on the network "
                    "itself."
                },
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            campaign.posts.filter(
                status__in=(
                    CampaignPost.Status.PENDING,
                    CampaignPost.Status.PUBLISHING,
                )
            ).update(
                status=CampaignPost.Status.CANCELLED, updated_at=timezone.now()
            )
            campaign.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CampaignRetryView(APIView):
    """POST ``/social/campaigns/{id}/retry/`` — try the failed destinations again.

    Only the failed ones, and only those with attempts left. A destination that
    already published is never re-sent: the retry button exists to finish a
    partial campaign, not to post twice.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = CampaignSerializer
    api_roles = ("trainer", "admin")

    @extend_schema(request=None, responses=CampaignSerializer)
    def post(self, request, pk):
        queryset = Campaign.objects.select_related("course")
        if getattr(request.user, "role", None) != "admin":
            queryset = queryset.filter(trainer=request.user)
        campaign = queryset.filter(pk=pk).first()
        if campaign is None:
            return Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )

        retryable = [
            post
            for post in campaign.posts.select_related("account")
            if post.can_retry
        ]
        if not retryable:
            return Response(
                {"detail": "Nothing on this campaign can be retried."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Back to pending so the shared claim/publish path owns them, exactly as
        # a first attempt would.
        CampaignPost.objects.filter(pk__in=[p.pk for p in retryable]).update(
            status=CampaignPost.Status.PENDING, error="", updated_at=timezone.now()
        )
        campaign.status = Campaign.Status.PUBLISHING
        campaign.save(update_fields=["status", "updated_at"])

        campaign = publishing.run_campaign(campaign)
        campaign = (
            Campaign.objects.select_related("course")
            .prefetch_related("posts")
            .get(pk=campaign.pk)
        )
        return Response(CampaignSerializer(campaign).data)
