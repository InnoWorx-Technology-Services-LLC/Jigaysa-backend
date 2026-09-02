"""Core routes: media presign, platform settings, and the Institutions admin.

Three groups mounted at three different prefixes by ``Jigaysa.urls`` — presign
under ``/api/v1/uploads/``, the rest at the API root. Splitting them here keeps
that mapping in one file instead of spread across the project urlconf.
"""

from django.urls import path
from rest_framework.routers import DefaultRouter

from core import admin_api, views

app_name = "core"

urlpatterns = [
    path("presign/", views.PresignUploadView.as_view(), name="presign-upload"),
    path("download/", views.PresignDownloadView.as_view(), name="presign-download"),
]

#: Platform settings live at the API root, not under ``uploads/``. Mounted
#: separately in ``Jigaysa.urls``.
settings_urlpatterns = [
    path(
        "platform-settings/",
        views.PlatformSettingView.as_view(),
        name="platform-settings",
    ),
    path(
        "platform-settings/public/",
        views.PublicPlatformSettingView.as_view(),
        name="platform-settings-public",
    ),
]

#: The admin console's Institutions page. Also mounted at the API root.
_org_router = DefaultRouter()
_org_router.register(
    "admin/organizations",
    admin_api.AdminOrganizationViewSet,
    basename="admin-organization",
)
organization_urlpatterns = _org_router.urls
