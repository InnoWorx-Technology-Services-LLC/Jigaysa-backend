"""Social routes: account connections, then the Promote wizard (PRD §3.3).

Mounted at ``/api/v1/`` by the project urlconf. Plain ``APIView``s rather than a
router: the connect half is an OAuth round-trip rather than CRUD, and keeping
the campaign half in the same idiom means one file describes the whole feature.
"""

from django.urls import path

from social import views

app_name = "social"

urlpatterns = [
    path(
        "social/accounts/",
        views.SocialAccountListView.as_view(),
        name="account-list",
    ),
    path(
        "social/accounts/<int:pk>/",
        views.SocialAccountDetailView.as_view(),
        name="account-detail",
    ),
    path(
        "social/connect/<str:provider>/",
        views.SocialConnectView.as_view(),
        name="connect",
    ),
    path(
        "social/callback/<str:provider>/",
        views.SocialCallbackView.as_view(),
        name="callback",
    ),
    # --- Promote wizard --------------------------------------------------- #
    path(
        "social/templates/",
        views.PromotionTemplateListView.as_view(),
        name="template-list",
    ),
    path(
        "social/rewrite/",
        views.PromotionRewriteView.as_view(),
        name="rewrite",
    ),
    path(
        "social/campaigns/",
        views.CampaignListCreateView.as_view(),
        name="campaign-list",
    ),
    path(
        "social/campaigns/<int:pk>/",
        views.CampaignDetailView.as_view(),
        name="campaign-detail",
    ),
    path(
        "social/campaigns/<int:pk>/retry/",
        views.CampaignRetryView.as_view(),
        name="campaign-retry",
    ),
]
