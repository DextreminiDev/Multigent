"""KeyVault: AES-256-GCM encryption of owner API keys (ARCHITECTURE.md §11.3).

- Random 96-bit nonce per record.
- AAD = chat_id bytes || provider, so a ciphertext copied to another chat (or
  relabelled as another provider) fails authentication.
- Multiple master keys by id for rotation; new records use the active id.
- Interface is KMS-ready: callers only see `encrypt`/`decrypt`.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True, slots=True)
class EncryptedKey:
    ciphertext: bytes
    nonce: bytes
    key_id: str

    def __repr__(self) -> str:  # never dump bytes into logs
        return f"EncryptedKey(key_id={self.key_id!r})"


class KeyVaultError(Exception):
    """Raised on any decrypt failure. Message never includes key material."""


class KeyVault(Protocol):
    def encrypt(self, plaintext: str, *, chat_id: uuid.UUID, provider: str) -> EncryptedKey: ...

    def decrypt(self, blob: EncryptedKey, *, chat_id: uuid.UUID, provider: str) -> str: ...

    @property
    def active_key_id(self) -> str: ...


def _aad(chat_id: uuid.UUID, provider: str) -> bytes:
    return chat_id.bytes + b"|" + provider.encode()


class LocalKeyVault:
    def __init__(self, keys: dict[str, bytes], active_id: str) -> None:
        if not keys:
            raise ValueError("KeyVault needs at least one master key")
        if active_id not in keys:
            raise ValueError("active key id not found")
        self._ciphers = {kid: AESGCM(material) for kid, material in keys.items()}
        self._active = active_id

    def __repr__(self) -> str:
        return f"LocalKeyVault(active={self._active!r}, keys={sorted(self._ciphers)!r})"

    @property
    def active_key_id(self) -> str:
        return self._active

    def encrypt(self, plaintext: str, *, chat_id: uuid.UUID, provider: str) -> EncryptedKey:
        nonce = os.urandom(12)
        ct = self._ciphers[self._active].encrypt(nonce, plaintext.encode(), _aad(chat_id, provider))
        return EncryptedKey(ciphertext=ct, nonce=nonce, key_id=self._active)

    def decrypt(self, blob: EncryptedKey, *, chat_id: uuid.UUID, provider: str) -> str:
        cipher = self._ciphers.get(blob.key_id)
        if cipher is None:
            raise KeyVaultError(f"unknown master key id {blob.key_id!r}")
        try:
            return cipher.decrypt(blob.nonce, blob.ciphertext, _aad(chat_id, provider)).decode()
        except InvalidTag:
            raise KeyVaultError("ciphertext failed authentication") from None

    def rewrap(self, blob: EncryptedKey, *, chat_id: uuid.UUID, provider: str) -> EncryptedKey:
        """Re-encrypt under the active key (rotation job)."""
        if blob.key_id == self._active:
            return blob
        return self.encrypt(
            self.decrypt(blob, chat_id=chat_id, provider=provider),
            chat_id=chat_id,
            provider=provider,
        )
