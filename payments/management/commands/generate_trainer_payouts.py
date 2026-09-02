"""Sweep trainers' earned-but-unpaid ledger lines into payouts (PRD §3.3).

The Earnings page shows what a trainer has earned the moment a sale settles.
This is the other half: turning those lines into a payout row per trainer, for
a period, so someone can actually pay them.

    python manage.py generate_trainer_payouts [--dry-run] [--as-of YYYY-MM-DD]

Run it monthly — the Earnings page tells trainers payouts land on the last day
of each month, so a cron entry on that day is what makes the copy true.

**Nothing here moves money.** There is no payout processor integration; this
records what is owed and for which period. Marking a payout `paid` is a
separate, deliberate act once it has been settled by other means.

Only earnings older than ``TRAINER_PAYOUT_HOLD_DAYS`` are swept, so a refund
inside the usual window reverses a line that has not been committed to a payout
yet. Safe to run twice: a line already attached to a payout is never picked up
again.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_date

from payments import earnings


class Command(BaseCommand):
    help = "Group trainers' unpaid earnings into payout records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be paid out without writing anything.",
        )
        parser.add_argument(
            "--as-of",
            type=str,
            default="",
            help="Cut-off date (YYYY-MM-DD). Defaults to now.",
        )

    def handle(self, *args, **options):
        as_of = timezone.now()
        if options["as_of"]:
            parsed = parse_date(options["as_of"])
            if parsed is None:
                self.stderr.write("--as-of must be YYYY-MM-DD.")
                return
            as_of = timezone.make_aware(
                timezone.datetime.combine(parsed, timezone.datetime.min.time()),
                timezone.get_current_timezone(),
            )

        if options["dry_run"]:
            return self._report(as_of)

        payouts = earnings.generate_payouts(as_of=as_of)
        if not payouts:
            self.stdout.write(
                f"Nothing payable (earnings must age "
                f"{earnings.hold_days()} days first)."
            )
            return

        total = sum(p.net for p in payouts)
        for payout in payouts:
            self.stdout.write(
                f"  {payout.trainer} → {payout.net} "
                f"({payout.period_start} … {payout.period_end})"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Created {len(payouts)} payout(s) totalling {total}. "
                "Nothing has been sent — mark them paid once settled."
            )
        )

    def _report(self, as_of):
        payable = earnings.payable_earnings(as_of).select_related("trainer")
        by_trainer = {}
        for line in payable:
            by_trainer.setdefault(str(line.trainer), []).append(line)

        if not by_trainer:
            self.stdout.write(
                f"Nothing payable (earnings must age "
                f"{earnings.hold_days()} days first)."
            )
            return
        for trainer, lines in by_trainer.items():
            total = sum(line.net for line in lines)
            self.stdout.write(
                f"would pay {trainer}: {total} across {len(lines)} line(s)"
            )
