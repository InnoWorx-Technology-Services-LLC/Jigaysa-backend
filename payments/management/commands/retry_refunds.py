"""Re-send refunds that were recorded but never reached Razorpay (PRD §3.13).

Two things land a refund here:

* **insufficient balance** — the takings were already settled to the bank, so
  there was no float to refund from. Top the Razorpay balance up, then run this.
* **network / unknown** — the call failed without telling us whether it worked.

Both keep the ``Refund`` row ``requested`` with no ``gateway_refund_id``, which
is exactly what this command looks for. It asks Razorpay for the payment's
existing refunds *before* re-sending, so a call that timed out but actually
succeeded is adopted rather than paid twice.

    python manage.py retry_refunds [--dry-run]

Safe to run on a schedule; it is a no-op when there is nothing owed.
"""

from django.core.management.base import BaseCommand

from payments import gateway, services
from payments.models import Refund


class Command(BaseCommand):
    help = "Re-send recorded refunds that never reached the gateway."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be retried without calling the gateway.",
        )

    def handle(self, *args, **options):
        pending = Refund.objects.filter(
            status=Refund.Status.REQUESTED, gateway_refund_id=""
        ).select_related("payment__order")

        if not pending.exists():
            self.stdout.write("Nothing owed — no unsent refunds.")
            return
        if not gateway.is_configured():
            self.stdout.write(
                self.style.WARNING(
                    f"{pending.count()} refund(s) owed but Razorpay is not "
                    "configured; settle them manually."
                )
            )
            return

        sent = held = failed = 0
        for refund in pending:
            label = f"#{refund.pk} ₹{refund.amount} (payment {refund.payment_id})"
            if options["dry_run"]:
                self.stdout.write(f"would retry {label}")
                continue
            services.retry_refund(refund)
            refund.refresh_from_db()
            if refund.status == Refund.Status.FAILED:
                failed += 1
                self.stdout.write(self.style.ERROR(f"permanently failed {label}"))
            elif refund.is_sent or refund.status == Refund.Status.PROCESSED:
                sent += 1
                self.stdout.write(self.style.SUCCESS(f"sent {label}"))
            else:
                held += 1
                reason = (refund.raw or {}).get("outcome", "unknown")
                self.stdout.write(
                    self.style.WARNING(f"still owed {label} — {reason}")
                )

        if options["dry_run"]:
            self.stdout.write(f"{pending.count()} refund(s) would be retried.")
            return
        self.stdout.write(
            self.style.SUCCESS(f"sent {sent}, still owed {held}, failed {failed}")
        )
