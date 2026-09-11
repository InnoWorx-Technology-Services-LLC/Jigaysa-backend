"""S3 / S3-compatible object storage helpers for direct browser uploads.

Flow (never proxies bytes through Django):

1. Frontend  ── POST /api/v1/uploads/presign/  {filename, content_type, purpose}
2. API       ── returns a short-lived *private* presigned PUT url (+ headers)
3. Frontend  ── PUTs the file bytes straight to S3/R2 with those headers
4. Frontend  ── PATCHes the owning resource with the returned ``key``
5. To view   ── GET /api/v1/uploads/download/?key=…  → presigned GET url

Presigned PUT (not POST) is used because Cloudflare R2 does not implement
presigned POST. PUT can't cap size in the signature, so ``max_bytes`` is
advisory — enforce it client-side and/or when persisting the key.

Keys are namespaced by purpose and owner so one user can't guess/scribble over
another's objects: ``{purpose}/{user_id}/{yyyy}/{mm}/{uuid}-{safe-filename}``.
"""

import os
import uuid
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

# Allowlisted upload purposes → the key prefix used for each. Requests for any
# other purpose are rejected so callers can't write outside these namespaces.
UPLOAD_PURPOSES = {
    "avatar": "avatars",
    "course_thumbnail": "course-thumbnails",
    "course_intro_video": "course-intros",
    "lesson_video": "lesson-videos",
    "lesson_resource": "lesson-resources",
    "library_video": "library/videos",
    "library_file": "library/files",
    "library_thumbnail": "library/thumbnails",
    "assignment": "submissions",
    "recording": "recordings",
    "message_attachment": "attachments",
    # Social/promo artwork. Kept out of "course-thumbnails" because a banner
    # composed for one post is not the course's cover: filing it there would
    # leave the two indistinguishable the next time somebody cleans the bucket.
    "promo_banner": "promo-banners",
    "social_image": "social-images",
}


class StorageNotConfigured(RuntimeError):
    """Raised when an upload is attempted but no bucket is configured."""


class StorageError(RuntimeError):
    """Wraps a boto/S3 failure so callers can turn it into a clean 502."""


def is_configured() -> bool:
    return bool(settings.AWS_STORAGE_BUCKET_NAME)


def _client():
    if not is_configured():
        raise StorageNotConfigured(
            "Object storage is not configured (set AWS_STORAGE_BUCKET_NAME)."
        )
    kwargs = {
        "region_name": settings.AWS_S3_REGION_NAME,
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.AWS_S3_ADDRESSING_STYLE},
        ),
    }
    if settings.AWS_S3_ENDPOINT_URL:
        kwargs["endpoint_url"] = settings.AWS_S3_ENDPOINT_URL
    if settings.AWS_ACCESS_KEY_ID:
        kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return boto3.client("s3", **kwargs)


def build_object_key(purpose: str, filename: str, user_id) -> str:
    """Build a collision-free, owner-namespaced object key for ``purpose``."""
    if purpose not in UPLOAD_PURPOSES:
        raise ValueError(f"Unknown upload purpose: {purpose!r}")
    prefix = UPLOAD_PURPOSES[purpose]
    base, ext = os.path.splitext(filename or "")
    safe_base = slugify(base)[:80] or "file"
    safe_ext = "".join(c for c in ext.lower() if c.isalnum() or c == ".")[:12]
    now = timezone.now()
    unique = uuid.uuid4().hex[:12]
    return f"{prefix}/{user_id}/{now:%Y/%m}/{unique}-{safe_base}{safe_ext}"


def generate_presigned_upload(key: str, content_type: str, expires: int = None) -> str:
    """A presigned PUT url the browser uploads the raw file body to.

    The client must send the same ``Content-Type`` header it declared here, or
    the signature won't match. Works on both AWS S3 and Cloudflare R2 (which does
    not support presigned POST).
    """
    expires = expires or settings.AWS_S3_UPLOAD_EXPIRY
    try:
        return _client().generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.AWS_STORAGE_BUCKET_NAME,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=expires,
        )
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - network
        raise StorageError(str(exc)) from exc


def generate_presigned_download(key: str, expires: int = None) -> str:
    """A short-lived presigned GET url for a private object."""
    expires = expires or settings.AWS_S3_DOWNLOAD_EXPIRY
    try:
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.AWS_STORAGE_BUCKET_NAME, "Key": key},
            ExpiresIn=expires,
        )
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - network
        raise StorageError(str(exc)) from exc


def storage_hosts() -> set:
    """Every hostname that can serve an object out of our own bucket."""
    hosts = set()
    if settings.AWS_S3_CUSTOM_DOMAIN:
        hosts.add(settings.AWS_S3_CUSTOM_DOMAIN.split("/")[0].lower())
    if settings.AWS_S3_ENDPOINT_URL:
        endpoint = urlparse(settings.AWS_S3_ENDPOINT_URL).netloc.lower()
        if endpoint:
            hosts.add(endpoint)
    bucket = settings.AWS_STORAGE_BUCKET_NAME
    # Only when we are actually on AWS. With an endpoint override the bucket
    # lives on R2 (or MinIO), and allowlisting the S3 hostname for that bucket
    # name would trust a host we do not own — on R2 the region is "auto", so
    # the entry is not even a real hostname.
    if bucket and not settings.AWS_S3_ENDPOINT_URL:
        hosts.add(f"{bucket}.s3.{settings.AWS_S3_REGION_NAME}.amazonaws.com".lower())
    return hosts


def is_own_storage_url(url: str) -> bool:
    """Whether ``url`` addresses an object in our own bucket.

    Anything that takes a URL from a client and later hands it to a third
    party — a social network fetching a post image, say — has to ask this
    first. An unchecked URL is both an SSRF and a way to make our own posts
    carry somebody else's content, and neither is visible in review once the
    field is merely a ``URLField``.
    """
    try:
        parts = urlparse(url or "")
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    return parts.netloc.lower() in storage_hosts()


def public_url(key: str) -> str:
    """Best-effort public URL (only resolvable for public-read objects/CDN)."""
    if settings.AWS_S3_CUSTOM_DOMAIN:
        return f"https://{settings.AWS_S3_CUSTOM_DOMAIN}/{key}"
    bucket = settings.AWS_STORAGE_BUCKET_NAME
    if settings.AWS_S3_ENDPOINT_URL:
        return f"{settings.AWS_S3_ENDPOINT_URL.rstrip('/')}/{bucket}/{key}"
    return f"https://{bucket}.s3.{settings.AWS_S3_REGION_NAME}.amazonaws.com/{key}"
