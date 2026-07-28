"""Razorpay webhook receiver (PRD §3.13).

This is the **authoritative** payment confirmation. The browser handler
(``POST /orders/{id}/verify/``) is a convenience so the student sees success
immediately — but a customer can pay and close the tab before it fires, and then
only the webhook tells us the money arrived. Both paths converge on
``services.confirm_webhook_payment`` / ``confirm_checkout``, which are idempotent,
so whichever lands first wins and the other is a no-op.

Setup: Razorpay dashboard → Settings → Webhooks → add
``https://<api-host>/api/v1/payments/webhook/razorpay/`` with the
``payment.captured`` and ``payment.failed`` events, then copy the webhook secret
into ``RAZORPAY_WEBHOOK_SECRET``.

Security: the request is unauthenticated (Razorpay has no credentials of ours),
so the HMAC over the **raw body** is the only thing establishing authenticity —
it is verified before the payload is parsed or trusted. Never move that check
below the parse.
"""

import json
import logging

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from payments import gateway, services
from payments.models import Payment

logger = logging.getLogger(__name__)

HANDLED_EVENTS = ("payment.captured", "payment.failed")


class RazorpayWebhookView(APIView):
    """POST /api/v1/payments/webhook/razorpay/ — gateway → server callback.

    Always returns 200 for events we understand but choose not to act on;
    Razorpay retries non-2xx responses, and retrying an event for an unknown
    order would never succeed.
    """

    permission_classes = [AllowAny]
    authentication_classes = []  # no session/JWT: this caller is Razorpay
    api_roles = ("public",)

    def post(self, request):
        signature = request.headers.get("X-Razorpay-Signature", "")
        try:
            gateway.verify_webhook_signature(request.body, signature)
        except gateway.SignatureMismatch:
            logger.warning("Razorpay webhook rejected: bad signature")
            return Response(
                {"detail": "Invalid signature."}, status=status.HTTP_400_BAD_REQUEST
            )
        except gateway.GatewayNotConfigured:
            logger.error("Razorpay webhook received but no webhook secret is set")
            return Response(
                {"detail": "Webhook is not configured on this server."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            event = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Response(
                {"detail": "Malformed payload."}, status=status.HTTP_400_BAD_REQUEST
            )

        event_type = event.get("event")
        if event_type not in HANDLED_EVENTS:
            return Response({"status": "ignored", "event": event_type})

        entity = (
            event.get("payload", {}).get("payment", {}).get("entity", {}) or {}
        )
        razorpay_order_id = entity.get("order_id")
        razorpay_payment_id = entity.get("id")
        if not razorpay_order_id:
            return Response({"status": "ignored", "reason": "no order_id"})

        if event_type == "payment.failed":
            payment = Payment.objects.filter(
                gateway_order_id=razorpay_order_id
            ).first()
            if payment:
                services.mark_failed(payment.order, razorpay_order_id, entity)
            return Response({"status": "recorded", "event": event_type})

        try:
            payment = services.confirm_webhook_payment(
                razorpay_order_id=razorpay_order_id,
                razorpay_payment_id=razorpay_payment_id,
                remote=entity,
            )
        except ValidationError as exc:
            # Permanent: the payment is real but doesn't match the order (wrong
            # amount, not captured). Retrying can never resolve it, so ack the
            # delivery and leave a loud log — this needs manual reconciliation.
            logger.error(
                "Razorpay webhook rejected (order=%s payment=%s): %s",
                razorpay_order_id, razorpay_payment_id, exc.detail,
            )
            return Response({"status": "rejected", "reason": str(exc.detail)})
        except Exception:
            # Transient (DB, fulfilment bug): 500 asks Razorpay to retry, and
            # the ids are logged so it can be replayed by hand if retries lapse.
            logger.exception(
                "Razorpay webhook fulfilment failed (order=%s payment=%s)",
                razorpay_order_id,
                razorpay_payment_id,
            )
            return Response(
                {"detail": "Fulfilment failed."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if payment is None:
            logger.warning(
                "Razorpay webhook for unknown order %s", razorpay_order_id
            )
            return Response({"status": "unknown_order"})
        return Response({"status": "ok", "order": payment.order_id})
