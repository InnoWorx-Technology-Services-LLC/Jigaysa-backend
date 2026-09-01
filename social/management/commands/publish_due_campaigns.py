"""Publish scheduled promo posts whose time has come (PRD §3.3).

The "Schedule for later" half of the Promote wizard is only real if something
runs. There is no Celery on this deployment, so this is a management command on
a timer — cron, or Task Scheduler on Windows. Every five minutes is plenty; the
wizard's time picker is minute-granular and nobody schedules a course launch to
the second.

    python manage.py publish_due_campaigns [--dry-run] [--limit N]

Safe to overlap with itself and with an inline "publish now": every post is
claimed with a conditional UPDATE before it is sent, so a second run finds
nothing to take. See ``social.publishing``.

It also sweeps up posts left ``pending`` by a "publish now" request that timed
out mid-fan-out — those have no schedule at all and are due immediately, which
is what makes a timeout a delay rather than a loss.
"""

from django.core.management.base import BaseCommand

from social import publishing
from social.models import CampaignPost


class Command(BaseCommand):
    help = "Publish scheduled social campaign posts that are due."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be published without calling any network.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=publishing.SWEEP_LIMIT,
            help=(
                "Maximum posts to attempt in one run "
                f"(default {publishing.SWEEP_LIMIT})."
            ),
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            return self._report(options["limit"])

        attempted = publishing.run_due(limit=options["limit"])
        if not attempted:
            self.stdout.write("Nothing due.")
            return

        published = sum(
            1
            for post in CampaignPost.objects.filter(
                pk__in=[p.pk for p in attempted]
            )
            if post.status == CampaignPost.Status.PUBLISHED
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Attempted {len(attempted)} post(s): {published} published, "
                f"{len(attempted) - published} not."
            )
        )

    def _report(self, limit):
        due = (
            CampaignPost.objects.filter(status=CampaignPost.Status.PENDING)
            .filter(publishing.due_filter())
            .select_related("campaign", "campaign__course")[:limit]
        )
        if not due:
            self.stdout.write("Nothing due.")
            return
        for post in due:
            when = post.campaign.scheduled_for or "now"
            self.stdout.write(
                f"would publish #{post.pk} {post.provider} → "
                f"{post.account_label or '(no label)'} "
                f"for “{post.campaign.course.title}” (due {when})"
            )
