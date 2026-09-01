"""Encryption for the OAuth token columns.

A connected ``SocialAccount`` holds a credential that can post to a trainer's
audience in their name. In plaintext, a database dump is an impersonation
incident — so the token columns are encrypted at rest with Fernet (AES-128-CBC
plus an HMAC), keyed from ``SOCIAL_TOKEN_KEY``.

The key lives in the environment and **only** there. Storing it in the database
it protects would put the ciphertext and its key in the same dump and reduce the
encryption to decoration.

``EncryptedTextField`` is used instead of encrypting at the call sites so that
no future code path can forget. Values carry a ``enc:v1:`` prefix, which makes
an encrypted value self-describing: a row written before this field existed (or
loaded from a fixture) is returned as-is rather than blowing up, and re-saving
it encrypts it. The prefix also carries a version, so a future key rotation or
cipher change has somewhere to hang.

Rotating ``SOCIAL_TOKEN_KEY`` makes every stored token undecryptable and forces
every trainer to reconnect. There is no way around that — it is why the key is
a deployment concern and not a settings-screen field.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

#: Marks a value as ciphertext this module produced. Anything without it is
#: treated as legacy plaintext and passed through on read.
PREFIX = "enc:v1:"


class TokenEncryptionUnavailable(ImproperlyConfigured):
    """Raised when a token must be encrypted but no usable key is configured."""


def _fernet():
    """Build the cipher, lazily.

    Lazy because the app must import (and the test suite must collect) on a
    machine with no key set — the same courtesy ``core.storage`` extends to a
    missing bucket. The failure surfaces when a token is actually written.
    """
    key = getattr(settings, "SOCIAL_TOKEN_KEY", "")
    if not key:
        raise TokenEncryptionUnavailable(
            "SOCIAL_TOKEN_KEY is not set, so OAuth tokens cannot be stored. "
            "Generate one with: python -c \"from cryptography.fernet import "
            'Fernet; print(Fernet.generate_key().decode())"'
        )
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise TokenEncryptionUnavailable(
            "The 'cryptography' package is required to store OAuth tokens."
        ) from exc

    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise TokenEncryptionUnavailable(
            "SOCIAL_TOKEN_KEY is not a valid Fernet key (expected 32 url-safe "
            "base64-encoded bytes)."
        ) from exc


def encrypt(value: str) -> str:
    """Plaintext → ``enc:v1:<ciphertext>``. Empty stays empty."""
    if not value:
        return ""
    if value.startswith(PREFIX):  # already encrypted; don't double-wrap
        return value
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """``enc:v1:<ciphertext>`` → plaintext. Unprefixed values pass through."""
    if not value or not value.startswith(PREFIX):
        return value or ""
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise TokenEncryptionUnavailable(
            "A stored OAuth token could not be decrypted. SOCIAL_TOKEN_KEY has "
            "most likely changed; affected accounts must reconnect."
        ) from exc


class EncryptedTextField(models.TextField):
    """A ``TextField`` that is ciphertext in the database and plaintext in Python.

    Deliberately not searchable: Fernet output is non-deterministic, so two
    encryptions of the same token differ. Never filter or index on this.
    """

    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def get_prep_value(self, value):
        return encrypt(super().get_prep_value(value) or "")
