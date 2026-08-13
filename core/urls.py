"""Media upload (direct-to-S3 presign) routes. Mounted at ``/api/v1/uploads/``."""

from django.urls import path

from core import views

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
