"""Field-level encryption at rest, keyed per use.

Some columns hold something that is dangerous to keep in plaintext — an OAuth
token that can post in a trainer's name, a bank account number that can receive
their money. Those are encrypted with Fernet (AES-128-CBC plus an HMAC) and
decrypted on read, so a database dump is not the incident by itself.

**The key is chosen per field, not globally**, because the keys have different
blast radii. Rotating the social key forces trainers to reconnect their
accounts — routine. Rotating a payout key would make every bank account on file
unreadable — not routine. Sharing one key would tie the cheap rotation to the
expensive one.

Values carry an ``enc:v1:`` prefix, which makes ciphertext self-describing: a
row written before the field was encrypted is returned as-is rather than
blowing up, and re-saving it encrypts it. The version gives a future key
rotation or cipher change somewhere to hang.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

#: Marks a value as ciphertext this module produced. Anything without it is
#: treated as legacy plaintext and passed through on read.
PREFIX = "enc:v1:"

GENERATE_HINT = (
    "Generate one with: python -c \"from cryptography.fernet import Fernet; "
    'print(Fernet.generate_key().decode())"'
)


class EncryptionUnavailable(ImproperlyConfigured):
    """Raised when a value must be encrypted but no usable key is configured."""


def fernet_for(key_setting: str):
    """Build the cipher for one key setting, lazily.

    Lazy because the app must import (and the test suite must collect) on a
    machine with no key set — the same courtesy ``core.storage`` extends to a
    missing bucket. The failure surfaces when a value is actually written.
    """
    key = getattr(settings, key_setting, "")
    if not key:
        raise EncryptionUnavailable(
            f"{key_setting} is not set, so this value cannot be stored. "
            f"{GENERATE_HINT}"
        )
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise EncryptionUnavailable(
            "The 'cryptography' package is required to encrypt this value."
        ) from exc

    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise EncryptionUnavailable(
            f"{key_setting} is not a valid Fernet key (expected 32 url-safe "
            "base64-encoded bytes)."
        ) from exc


def encrypt(value: str, key_setting: str) -> str:
    """Plaintext → ``enc:v1:<ciphertext>``. Empty stays empty."""
    if not value:
        return ""
    if value.startswith(PREFIX):  # already encrypted; don't double-wrap
        return value
    return PREFIX + fernet_for(key_setting).encrypt(value.encode()).decode()


def decrypt(value: str, key_setting: str, *, lost_message: str = "") -> str:
    """``enc:v1:<ciphertext>`` → plaintext. Unprefixed values pass through."""
    if not value or not value.startswith(PREFIX):
        return value or ""
    from cryptography.fernet import InvalidToken

    try:
        return fernet_for(key_setting).decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise EncryptionUnavailable(
            lost_message
            or f"A stored value could not be decrypted. {key_setting} has most "
            "likely changed."
        ) from exc


class EncryptedTextField(models.TextField):
    """A ``TextField`` that is ciphertext in the database and plaintext in Python.

    Subclass it and set ``key_setting`` rather than passing the key as a field
    argument — that keeps the choice of key out of migrations, so rotating or
    renaming a setting never needs a schema change.

    Deliberately not searchable: Fernet output is non-deterministic, so two
    encryptions of the same value differ. Never filter or index on this.
    """

    #: Name of the Django setting holding this field's Fernet key.
    key_setting = None
    #: Shown when ciphertext will not decrypt, usually a changed key.
    lost_message = ""

    def from_db_value(self, value, expression, connection):
        return decrypt(value, self.key_setting, lost_message=self.lost_message)

    def get_prep_value(self, value):
        return encrypt(super().get_prep_value(value) or "", self.key_setting)
