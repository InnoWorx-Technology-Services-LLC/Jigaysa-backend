"""Media upload endpoints (direct-to-S3 via presigned URLs) and platform settings.

The upload endpoints never handle file bytes: they only mint short-lived
credentials. The frontend uploads straight to the bucket, then stores the
returned ``key`` on the owning resource (e.g. ``LibraryResource.file_url``,
``Course.thumbnail``). To render a private object later, call the download
endpoint for a presigned GET.
"""

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import storage
from core.models import PlatformSetting
from core.permissions import IsAdmin
from core.serializers import (
    PlatformSettingSerializer,
    PresignDownloadSerializer,
    PresignUploadSerializer,
    PublicPlatformSettingSerializer,
)

ALL_ROLES = ("student", "trainer", "admin", "institution")


def _service_unavailable():
    return Response(
        {"detail": "Object storage is not configured on this server."},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


class PresignUploadView(APIView):
    """POST → a private presigned PUT url the browser uploads the file to.

    Body: ``{filename, content_type, purpose}``. Returns ``{method, url, headers,
    key, public_url, expires_in, max_bytes}``. Upload with an HTTP ``PUT`` to
    ``url``, sending the raw file as the body and every entry in ``headers`` (the
    ``Content-Type`` must match what you declared), then save ``key`` on the
    owning record.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = PresignUploadSerializer
    api_roles = ALL_ROLES

    def post(self, request):
        if not storage.is_configured():
            return _service_unavailable()
        serializer = PresignUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        key = storage.build_object_key(
            purpose=data["purpose"],
            filename=data["filename"],
            user_id=request.user.id,
        )
        try:
            url = storage.generate_presigned_upload(key, data["content_type"])
        except storage.StorageError:
            return Response(
                {"detail": "Could not generate an upload URL. Try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {
                "method": "PUT",
                "url": url,
                "headers": {"Content-Type": data["content_type"]},
                "key": key,
                "public_url": storage.public_url(key),
                "expires_in": settings.AWS_S3_UPLOAD_EXPIRY,
                "max_bytes": settings.AWS_S3_MAX_UPLOAD_BYTES,
            },
            status=status.HTTP_201_CREATED,
        )


class PresignDownloadView(APIView):
    """GET ``?key=…`` (or POST ``{key}``) → a short-lived presigned GET url for a
    private object."""

    permission_classes = [IsAuthenticated]
    serializer_class = PresignDownloadSerializer
    api_roles = ALL_ROLES

    def get(self, request):
        return self._presign(request.query_params)

    def post(self, request):
        return self._presign(request.data)

    def _presign(self, source):
        if not storage.is_configured():
            return _service_unavailable()
        serializer = PresignDownloadSerializer(data=source)
        serializer.is_valid(raise_exception=True)
        try:
            url = storage.generate_presigned_download(serializer.validated_data["key"])
        except storage.StorageError:
            return Response(
                {"detail": "Could not generate a download URL. Try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {"download_url": url, "expires_in": settings.AWS_S3_DOWNLOAD_EXPIRY}
        )


class PlatformSettingView(APIView):
    """GET / PATCH ``/api/v1/platform-settings/`` — admin-only, the whole row.

    A singleton, so there is no id in the path and no list. ``PATCH`` with just
    the fields you changed; the gateway secrets are write-only (see
    ``PlatformSettingSerializer``).
    """

    permission_classes = [IsAdmin]
    serializer_class = PlatformSettingSerializer
    api_roles = ("admin",)

    @extend_schema(responses=PlatformSettingSerializer)
    def get(self, request):
        return Response(PlatformSettingSerializer(PlatformSetting.get_solo()).data)

    @extend_schema(
        request=PlatformSettingSerializer, responses=PlatformSettingSerializer
    )
    def patch(self, request):
        # ``get_solo`` may hand back an unsaved default when the seed migration
        # hasn't run; saving it is what materialises the singleton.
        serializer = PlatformSettingSerializer(
            PlatformSetting.get_solo(), data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class PublicPlatformSettingView(APIView):
    """GET ``/api/v1/platform-settings/public/`` — branding + feature flags.

    Open to anonymous callers: the landing page needs the platform name before
    anyone has signed in, and the student UI needs the flags to know whether to
    draw the coupon box. Deliberately excludes the tax rate, the commission
    split and every credential.
    """

    permission_classes = [AllowAny]
    serializer_class = PublicPlatformSettingSerializer
    api_roles = ("public",)

    @extend_schema(responses=PublicPlatformSettingSerializer)
    def get(self, request):
        return Response(
            PublicPlatformSettingSerializer(PlatformSetting.get_solo()).data
        )
