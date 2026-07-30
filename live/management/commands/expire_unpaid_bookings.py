"""Release slots held by 1:1 bookings the student never paid for (PRD §3.6).

An accepted booking holds the trainer's slot while the student pays. Without a
sweep that hold is permanent: an unpaid booking would block a sellable hour for
ever. Run this on a schedule (cron / Task Scheduler, every 15 minutes is
plenty).

    python manage.py expire_unpaid_bookings [--dry-run]
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from live.models import IndividualBooking, TrainerAvailability
from notifications.models import NotificationCategory
from notifications.services import notify


class Command(BaseCommand):
    help = "Cancel awaiting-payment 1:1 bookings past their payment deadline."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be cancelled without changing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        candidates = IndividualBooking.objects.filter(
            status=IndividualBooking.Status.AWAITING_PAYMENT
        ).select_related("order", "student", "trainer")

        expired = [b for b in candidates if b.payment_expired]
        if not expired:
            self.stdout.write("No unpaid bookings past their deadline.")
            return

        for booking in expired:
            label = f"#{booking.pk} {booking.student} ↔ {booking.trainer}"
            if dry_run:
                self.stdout.write(f"would expire {label} (due {booking.payment_due_at})")
                continue
            with transaction.atomic():
                booking.status = IndividualBooking.Status.CANCELLED
                booking.save(update_fields=["status", "updated_at"])
                if booking.start:
                    TrainerAvailability.objects.filter(
                        trainer_id=booking.trainer_id,
                        start=booking.start,
                        is_booked=True,
                    ).update(is_booked=False)
            notify(
                booking.student,
                NotificationCategory.LIVE_CLASS,
                title="1:1 booking expired",
                body=f"“{booking.topic or 'Your session'}” was released because "
                "payment wasn't completed in time.",
                link="/student/sessions",
            )
            self.stdout.write(self.style.WARNING(f"expired {label}"))

        verb = "would expire" if dry_run else "expired"
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(expired)} booking(s)."))
