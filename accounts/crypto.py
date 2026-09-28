"""Encryption for the trainer bank account column.

A full account number is a payout instrument: with it and an IFSC, money moves.
In plaintext, a database dump is a fraud kit — so the number is encrypted at
rest with Fernet, keyed from ``PAYOUT_ENCRYPTION_KEY``.

The machinery is ``core.crypto``; this module is the payout-specific binding.
The key is deliberately **not** ``SOCIAL_TOKEN_KEY``: rotating that one merely
forces trainers to reconnect their social accounts, while losing this one makes
every bank account on file unreadable and every trainer has to re-enter it.

Only the account number is encrypted. The IFSC is a public branch code and the
last four digits are a label shown back to the trainer, so encrypting either
would buy nothing and make them unsearchable.
"""

from core import crypto

KEY_SETTING = "PAYOUT_ENCRYPTION_KEY"

LOST_KEY_MESSAGE = (
    "A stored bank account number could not be decrypted. "
    "PAYOUT_ENCRYPTION_KEY has most likely changed; affected trainers must "
    "re-enter their payout details."
)


class PayoutEncryptedTextField(crypto.EncryptedTextField):
    """Ciphertext in the database, plaintext in Python. See ``core.crypto``."""

    key_setting = KEY_SETTING
    lost_message = LOST_KEY_MESSAGE
