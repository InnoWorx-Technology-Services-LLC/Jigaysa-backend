"""Shared payments test setup.

The real ``.env`` carries Razorpay test keys, which would otherwise leak into
the suite and flip ``POST /orders/{id}/pay/`` into its 409 "gateway configured"
branch. Blank the keys by default so each test states which gateway mode it
means to exercise: the mock path needs nothing, the Razorpay path opts in with
the ``live_keys`` fixture.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_gateway_by_default(settings):
    settings.RAZORPAY_KEY_ID = ""
    settings.RAZORPAY_KEY_SECRET = ""
    settings.RAZORPAY_WEBHOOK_SECRET = ""
    return settings
