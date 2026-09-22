"""Live-app test setup.

Mirrors ``payments/tests/conftest.py``: the real ``.env`` carries Razorpay test
keys, and 1:1 booking tests settle orders through the mock ``POST
/orders/{id}/pay/`` path, which 409s whenever a gateway is configured. Blank the
keys so those tests exercise the mock branch deliberately.

``JITSI_APP_SECRET`` is blanked for the same reason. Once a deployment's
``.env`` carries it, ``join`` starts minting tokens — so without this, whether
the join tests exercise the Jitsi branch or the fallback would depend on which
machine they run on. Tests that want the bridge turn it on explicitly; see
``test_meeting.py``.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_gateway_by_default(settings):
    settings.RAZORPAY_KEY_ID = ""
    settings.RAZORPAY_KEY_SECRET = ""
    settings.RAZORPAY_WEBHOOK_SECRET = ""
    settings.JITSI_APP_SECRET = ""
    return settings
