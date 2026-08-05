"""
URL configuration for Jigayasa project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
"""
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("admin/", admin.site.urls),

    # API v1
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/", include("accounts.api_urls")),
    path("api/v1/", include("courses.urls")),
    path("api/v1/", include("certificates.urls")),
    path("api/v1/", include("library.urls")),
    path("api/v1/", include("live.urls")),
    path("api/v1/", include("assessments.urls")),
    path("api/v1/", include("engagement.urls")),
    path("api/v1/", include("notifications.urls")),
    path("api/v1/", include("payments.urls")),
    # Recordings (§3.11) built but parked — mount when ready:
    # path("api/v1/", include("recordings.urls")),
    path("api/v1/uploads/", include("core.urls")),

    # OpenAPI schema + interactive docs
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("", lambda request: HttpResponse("hello from server 411")),
]
