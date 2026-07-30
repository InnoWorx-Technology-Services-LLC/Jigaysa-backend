"""Live-app test setup.

Mirrors ``payments/tests/conftest.py``: the real ``.env`` carries Razorpay test
keys, and 1:1 booking tests settle orders through the mock ``POST
/orders/{id}/pay/`` path, which 409s whenever a gateway is configured. Blank the
keys so those tests exercise the mock branch deliberately.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_gateway_by_default(settings):
    settings.RAZORPAY_KEY_ID = ""
    settings.RAZORPAY_KEY_SECRET = ""
    settings.RAZORPAY_WEBHOOK_SECRET = ""
    return settings
