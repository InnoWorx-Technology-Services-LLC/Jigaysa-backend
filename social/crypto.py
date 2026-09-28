"""Encryption for the OAuth token columns.

A connected ``SocialAccount`` holds a credential that can post to a trainer's
audience in their name. In plaintext, a database dump is an impersonation
incident — so the token columns are encrypted at rest with Fernet, keyed from
``SOCIAL_TOKEN_KEY``.

The key lives in the environment and **only** there. Storing it in the database
it protects would put the ciphertext and its key in the same dump and reduce the
encryption to decoration.

The machinery is ``core.crypto``, shared with the payout fields; this module is
the social-specific binding — its key, and its wording for what a lost key
costs. ``EncryptedTextField`` is used instead of encrypting at the call sites so
that no future code path can forget.

Rotating ``SOCIAL_TOKEN_KEY`` makes every stored token undecryptable and forces
every trainer to reconnect. There is no way around that — it is why the key is
a deployment concern and not a settings-screen field.
"""

from core import crypto
from core.crypto import PREFIX  # noqa: F401 — re-exported; part of this module's API

KEY_SETTING = "SOCIAL_TOKEN_KEY"

LOST_KEY_MESSAGE = (
    "A stored OAuth token could not be decrypted. SOCIAL_TOKEN_KEY has most "
    "likely changed; affected accounts must reconnect."
)


def encrypt(value: str) -> str:
    """Plaintext → ``enc:v1:<ciphertext>``. Empty stays empty."""
    return crypto.encrypt(value, KEY_SETTING)


def decrypt(value: str) -> str:
    """``enc:v1:<ciphertext>`` → plaintext. Unprefixed values pass through."""
    return crypto.decrypt(value, KEY_SETTING, lost_message=LOST_KEY_MESSAGE)


class EncryptedTextField(crypto.EncryptedTextField):
    """Ciphertext in the database, plaintext in Python. See ``core.crypto``."""

    key_setting = KEY_SETTING
    lost_message = LOST_KEY_MESSAGE
