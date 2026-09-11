"""Apply (or inspect) the browser-upload CORS policy on the storage bucket.

Presigned PUTs go **browser → bucket** directly; Django is never in that
request. So ``CORS_ALLOWED_ORIGINS`` — which governs calls to this API — has no
bearing on them, and a bucket with no policy of its own fails the preflight
before the upload is even attempted. That is a bucket setting, not application
config, which is exactly why it is easy to leave undone: nothing in the Django
codebase reads it, so nothing in review notices it missing.

This command exists so the policy is written down and reproducible instead of
living in somebody's memory of a dashboard session.

    python manage.py set_bucket_cors --show      # what the bucket has now
    python manage.py set_bucket_cors --dry-run   # what would be written
    python manage.py set_bucket_cors             # write it
"""

import json
from urllib.parse import urlparse

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import storage

#: ``GET``/``HEAD`` are here alongside ``PUT`` because the presigned *download*
#: url is fetched from the browser too — a policy covering only the upload
#: leaves private-object previews failing the same way, one bug later.
METHODS = ["PUT", "GET", "HEAD"]

#: R2 and S3 both reflect the request headers they are asked about; the upload
#: sends ``Content-Type`` because the signature covers it.
HEADERS = ["content-type"]

#: So a client can read back the object version it just wrote.
EXPOSE = ["ETag"]

MAX_AGE = 3600


def _origins() -> list:
    """Every origin the app is served from, deduped, order preserved.

    Derived from the origins this API already trusts rather than a second
    hand-maintained list: a frontend that can call the API but cannot upload
    is the failure this command is here to prevent.
    """
    found = list(settings.CORS_ALLOWED_ORIGINS)
    frontend = getattr(settings, "FRONTEND_URL", "")
    if frontend:
        parts = urlparse(frontend)
        if parts.scheme and parts.netloc:
            found.append(f"{parts.scheme}://{parts.netloc}")
    seen, origins = set(), []
    for origin in found:
        origin = origin.strip().rstrip("/")
        if origin and origin not in seen:
            seen.add(origin)
            origins.append(origin)
    return origins


class Command(BaseCommand):
    help = "Apply the browser-upload CORS policy to the storage bucket."

    def add_arguments(self, parser):
        parser.add_argument(
            "--origin",
            action="append",
            default=[],
            dest="extra_origins",
            help="Extra origin to allow. Repeatable.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the policy that would be written and exit.",
        )
        parser.add_argument(
            "--show",
            action="store_true",
            help="Print the bucket's current policy and exit.",
        )

    def handle(self, *args, **options):
        if not storage.is_configured():
            raise CommandError(
                "Object storage is not configured (set AWS_STORAGE_BUCKET_NAME)."
            )
        bucket = settings.AWS_STORAGE_BUCKET_NAME
        client = storage._client()

        if options["show"]:
            try:
                current = client.get_bucket_cors(Bucket=bucket)["CORSRules"]
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if "NoSuchCORSConfiguration" in code:
                    self.stdout.write(
                        self.style.WARNING(
                            f"{bucket}: no CORS policy set — browser uploads "
                            f"cannot work."
                        )
                    )
                    return
                raise CommandError(str(exc)) from exc
            self.stdout.write(f"{bucket} current CORS policy:")
            self.stdout.write(json.dumps(current, indent=2))
            return

        origins = _origins() + [
            o.strip().rstrip("/") for o in options["extra_origins"] if o.strip()
        ]
        if not origins:
            raise CommandError(
                "No origins to allow. Set CORS_ALLOWED_ORIGINS or pass --origin."
            )
        rules = [
            {
                "AllowedOrigins": origins,
                "AllowedMethods": METHODS,
                "AllowedHeaders": HEADERS,
                "ExposeHeaders": EXPOSE,
                "MaxAgeSeconds": MAX_AGE,
            }
        ]

        if options["dry_run"]:
            self.stdout.write(f"Would write to {bucket}:")
            self.stdout.write(json.dumps(rules, indent=2))
            return

        try:
            client.put_bucket_cors(
                Bucket=bucket, CORSConfiguration={"CORSRules": rules}
            )
        except (BotoCoreError, ClientError) as exc:
            raise CommandError(f"Could not set the CORS policy: {exc}") from exc

        self.stdout.write(self.style.SUCCESS(f"CORS policy applied to {bucket}."))
        for origin in origins:
            self.stdout.write(f"  allowed: {origin}")
