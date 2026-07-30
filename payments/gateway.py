"""Razorpay gateway adapter (PRD §3.13 payment gateways).

Isolates every Razorpay-specific concern — SDK calls, amount units, signature
verification — so ``payments.services`` stays about money math and fulfilment.

Flow implemented here:

1. ``create_order()``  — server creates a Razorpay order for our local Order.
2. Frontend opens Razorpay Checkout with that ``id`` and the public key.
3. ``verify_checkout_signature()`` — validates the handler payload the browser
   posts back (``razorpay_order_id|razorpay_payment_id`` HMAC'd with the secret).
4. ``verify_webhook_signature()`` — validates the server-to-server webhook, which
   is the *authoritative* confirmation (the browser can always be closed
   mid-payment).

Amounts: Razorpay works in the currency's smallest unit (paise for INR). Our
models store rupees as ``Decimal``. ``to_minor_units`` / ``from_minor_units`` are
the only places that conversion happens.

The SDK import is lazy so the app boots (and the test suite runs) without the
``razorpay`` package installed — exactly like ``core.storage`` does with boto3.
When no keys are configured the whole module reports ``is_configured() is False``
and checkout falls back to the mock path in ``services.pay_order``.
"""

import hashlib
import hmac
from decimal import Decimal

from django.conf import settings


class GatewayNotConfigured(RuntimeError):
    """Raised when a Razorpay call is attempted with no keys set."""


class GatewayError(RuntimeError):
    """Wraps an SDK/network failure so callers can return a clean 502."""


class SignatureMismatch(RuntimeError):
    """Raised when a checkout or webhook signature fails verification."""


def is_configured() -> bool:
    """True when both Razorpay keys are present (test or live)."""
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def is_test_mode() -> bool:
    """Razorpay test keys are prefixed ``rzp_test_``; live keys ``rzp_live_``."""
    return str(settings.RAZORPAY_KEY_ID).startswith("rzp_test_")


def to_minor_units(amount) -> int:
    """Rupees (Decimal) → paise (int). Razorpay rejects fractional paise."""
    return int((Decimal(amount) * 100).to_integral_value())


def from_minor_units(paise) -> Decimal:
    """Paise (int) → rupees (Decimal), for comparing against Order.total."""
    return (Decimal(paise) / 100).quantize(Decimal("0.01"))


def _client():
    if not is_configured():
        raise GatewayNotConfigured(
            "Razorpay is not configured (set RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET)."
        )
    try:
        import razorpay  # lazy: keeps the SDK an optional dependency
    except ImportError as exc:  # pragma: no cover - packaging issue
        raise GatewayNotConfigured(
            "The 'razorpay' package is not installed (pip install razorpay)."
        ) from exc
    return razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )


def create_order(amount, receipt, notes=None, currency="INR") -> dict:
    """Create a Razorpay order for ``amount`` rupees. Returns the raw dict.

    ``receipt`` is our own Order id — it shows up in the Razorpay dashboard and
    on the webhook, which is what makes reconciliation possible.
    """
    try:
        return _client().order.create(
            {
                "amount": to_minor_units(amount),
                "currency": currency,
                "receipt": str(receipt),
                "notes": notes or {},
                # Capture automatically on success; without this the payment is
                # only authorized and would need a separate capture call.
                "payment_capture": 1,
            }
        )
    except GatewayNotConfigured:
        raise
    except Exception as exc:  # SDK raises bare BadRequestError/ServerError
        raise GatewayError(str(exc)) from exc


def fetch_payment(payment_id) -> dict:
    """Fetch a payment from Razorpay — the source of truth for status/amount."""
    try:
        return _client().payment.fetch(payment_id)
    except GatewayNotConfigured:
        raise
    except Exception as exc:
        raise GatewayError(str(exc)) from exc


class RefundNotPossible(RuntimeError):
    """Razorpay refused the refund for a reason retrying cannot fix.

    Distinct from ``GatewayError`` on purpose: a network blip should be retried,
    a payment that is too old to refund never will be. The caller uses this to
    decide between "try again later" and "escalate to a human".
    """


class InsufficientBalance(RuntimeError):
    """Razorpay has no float to refund from — the money is already settled out.

    Retryable, but only after the merchant balance is topped up, which is a
    human action. Kept separate from ``RefundNotPossible`` so the obligation
    stays open instead of being written off.
    """


# Razorpay error descriptions are prose, not codes, so matching is fuzzy by
# necessity. Anything unmatched is treated as retryable — the safe default,
# because a wrongly-terminal refund silently keeps a student's money.
_INSUFFICIENT_BALANCE_HINTS = (
    "insufficient balance",
    "not have sufficient balance",
    "balance is insufficient",
)
_PERMANENT_HINTS = (
    "fully refunded",
    "has already been refunded",
    "is not captured",
    "does not exist",
    "invalid id",
    "cannot be refunded",
    "refund amount is greater",
)


def classify_refund_error(message: str):
    """Map a Razorpay error string to the exception the caller should see."""
    text = (message or "").lower()
    if any(h in text for h in _INSUFFICIENT_BALANCE_HINTS):
        return InsufficientBalance(message)
    if any(h in text for h in _PERMANENT_HINTS):
        return RefundNotPossible(message)
    return GatewayError(message)


def refund_payment(payment_id, amount, notes=None, speed="normal") -> dict:
    """Refund ``amount`` rupees against a captured Razorpay payment.

    ``speed="normal"`` settles in 5-7 working days from the merchant balance;
    ``"optimum"`` is Razorpay's instant refund and costs extra. Returns the raw
    refund dict, whose ``status`` is ``pending`` or ``processed`` — the
    ``refund.processed`` / ``refund.failed`` webhook is what settles it for real,
    exactly like payments.

    Raises ``InsufficientBalance`` / ``RefundNotPossible`` / ``GatewayError``
    so the caller can tell a retry from an escalation.
    """
    try:
        return _client().payment.refund(
            payment_id,
            {
                "amount": to_minor_units(amount),
                "speed": speed,
                "notes": notes or {},
            },
        )
    except GatewayNotConfigured:
        raise
    except Exception as exc:
        raise classify_refund_error(str(exc)) from exc


def fetch_refunds(payment_id) -> list:
    """List Razorpay's refunds for a payment — the source of truth.

    Used before retrying: if a request timed out we cannot know whether the
    refund was created, and asking is the only way to avoid refunding twice.
    """
    try:
        result = _client().payment.fetch_multiple_refund(payment_id)
    except GatewayNotConfigured:
        raise
    except Exception as exc:
        raise GatewayError(str(exc)) from exc
    return (result or {}).get("items", []) or []


def _hmac_sha256(message: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_checkout_signature(razorpay_order_id, razorpay_payment_id, signature):
    """Validate the Checkout handler payload. Raises ``SignatureMismatch``.

    Razorpay signs ``"{order_id}|{payment_id}"`` with the API *secret*.
    """
    if not is_configured():
        raise GatewayNotConfigured("Razorpay is not configured.")
    expected = _hmac_sha256(
        f"{razorpay_order_id}|{razorpay_payment_id}", settings.RAZORPAY_KEY_SECRET
    )
    if not hmac.compare_digest(expected, str(signature or "")):
        raise SignatureMismatch("Payment signature verification failed.")


def verify_webhook_signature(raw_body: bytes, signature: str):
    """Validate a webhook delivery. Raises ``SignatureMismatch``.

    The webhook is signed with the *webhook secret* (set in the Razorpay
    dashboard), which is deliberately different from the API secret — so a leaked
    webhook secret can't be used to call the API.
    """
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret:
        raise GatewayNotConfigured(
            "Razorpay webhook secret is not configured (RAZORPAY_WEBHOOK_SECRET)."
        )
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, str(signature or "")):
        raise SignatureMismatch("Webhook signature verification failed.")
