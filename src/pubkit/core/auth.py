# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Credentials.

Design rule, and it is not negotiable: **pubkit never accepts a password.**

For API platforms the user pastes a token once and it goes into the OS
keychain. For browser platforms the user signs in themselves, in a real visible
browser window, and pubkit persists only the resulting session state — which it
encrypts, with the key in the keychain.

This is not only a security posture. It is also the only thing that works:
platforms increasingly gate login behind CAPTCHA, device confirmation and MFA,
none of which an automation layer should be trying to get past.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

try:  # pragma: no cover - environment dependent
    import keyring

    _HAVE_KEYRING = True
except Exception:  # pragma: no cover
    keyring = None  # type: ignore[assignment]
    _HAVE_KEYRING = False

try:  # pragma: no cover - environment dependent
    from cryptography.fernet import Fernet

    _HAVE_FERNET = True
except Exception:  # pragma: no cover
    Fernet = None  # type: ignore[assignment]
    _HAVE_FERNET = False

SERVICE = "pubkit"


class CredentialError(RuntimeError):
    """Missing or unusable credentials. Never retried — see adapter.NotRetryable."""


@dataclass
class TokenStore:
    """API tokens. Keychain first, encrypted vault as the CI fallback."""

    vault_path: Path = Path(".pubkit/vault.json")
    service: str = SERVICE

    # ------------------------------------------------------------------ read
    def get(self, platform: str, key: str = "token") -> str | None:
        env = os.environ.get(f"PUBKIT_{platform.upper()}_{key.upper()}")
        if env:
            return env
        if _HAVE_KEYRING:
            try:
                val = keyring.get_password(self.service, f"{platform}:{key}")
                if val:
                    return val
            except Exception:
                pass
        return self._vault_get(platform, key)

    def require(self, platform: str, key: str = "token") -> str:
        val = self.get(platform, key)
        if not val:
            raise CredentialError(
                f"no {key} for {platform}. Run `pubkit auth login {platform}`, "
                f"or set PUBKIT_{platform.upper()}_{key.upper()}."
            )
        return val

    # ----------------------------------------------------------------- write
    def set(self, platform: str, value: str, key: str = "token") -> None:
        if _HAVE_KEYRING:
            try:
                keyring.set_password(self.service, f"{platform}:{key}", value)
                return
            except Exception:
                pass
        self._vault_set(platform, key, value)

    def delete(self, platform: str, key: str = "token") -> None:
        if _HAVE_KEYRING:
            try:
                keyring.delete_password(self.service, f"{platform}:{key}")
            except Exception:
                pass
        data = self._vault_read()
        data.pop(f"{platform}:{key}", None)
        self._vault_write(data)

    # ----------------------------------------------------------------- vault
    def _vault_key(self) -> bytes:
        env = os.environ.get("PUBKIT_VAULT_KEY")
        if env:
            return env.encode()
        if _HAVE_KEYRING:
            k = keyring.get_password(self.service, "_vault_key")
            if not k:
                k = Fernet.generate_key().decode() if _HAVE_FERNET else base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
                keyring.set_password(self.service, "_vault_key", k)
            return k.encode()
        raise CredentialError(
            "no keychain and no PUBKIT_VAULT_KEY set; cannot encrypt the vault. "
            "In CI, set PUBKIT_VAULT_KEY from your secret store."
        )

    def _vault_read(self) -> dict[str, str]:
        if not self.vault_path.exists():
            return {}
        blob = self.vault_path.read_bytes()
        if not _HAVE_FERNET:
            raise CredentialError("cryptography is required to read the vault")
        return json.loads(Fernet(self._vault_key()).decrypt(blob))

    def _vault_write(self, data: dict[str, str]) -> None:
        if not _HAVE_FERNET:
            raise CredentialError("cryptography is required to write the vault")
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        blob = Fernet(self._vault_key()).encrypt(json.dumps(data).encode())
        self.vault_path.write_bytes(blob)
        os.chmod(self.vault_path, 0o600)

    def _vault_get(self, platform: str, key: str) -> str | None:
        try:
            return self._vault_read().get(f"{platform}:{key}")
        except Exception:
            return None

    def _vault_set(self, platform: str, key: str, value: str) -> None:
        data = self._vault_read()
        data[f"{platform}:{key}"] = value
        self._vault_write(data)


@dataclass
class SessionStore:
    """Persisted Playwright `storage_state`, encrypted at rest.

    The flow is deliberately human-in-the-loop:

        pubkit auth login medium
          → opens a real browser window at the platform's login page
          → you sign in (password manager, MFA, whatever it takes)
          → pubkit waits for the post-login URL, saves the session, closes

    pubkit sees cookies, never credentials.
    """

    dir: Path = Path(".pubkit/sessions")
    tokens: TokenStore = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.tokens is None:
            self.tokens = TokenStore()

    def _path(self, platform: str) -> Path:
        return self.dir / f"{platform}.session"

    def exists(self, platform: str) -> bool:
        return self._path(platform).exists()

    def save(self, platform: str, storage_state: dict) -> None:
        raw = json.dumps(storage_state).encode()
        if _HAVE_FERNET:
            raw = Fernet(self.tokens._vault_key()).encrypt(raw)
        p = self._path(platform)
        p.write_bytes(raw)
        os.chmod(p, 0o600)

    def load(self, platform: str) -> dict | None:
        p = self._path(platform)
        if not p.exists():
            return None
        raw = p.read_bytes()
        if _HAVE_FERNET:
            try:
                raw = Fernet(self.tokens._vault_key()).decrypt(raw)
            except Exception as exc:  # corrupted or key rotated
                raise CredentialError(
                    f"cannot decrypt the saved {platform} session; "
                    f"run `pubkit auth login {platform}` again ({exc})"
                ) from exc
        return json.loads(raw)

    def forget(self, platform: str) -> None:
        self._path(platform).unlink(missing_ok=True)


class RedactingFilter:
    """Log filter that strips secrets by construction, not by discipline."""

    def __init__(self, secrets_: list[str]) -> None:
        self._secrets = [s for s in secrets_ if s and len(s) >= 8]

    def filter(self, record) -> bool:  # pragma: no cover - trivial
        msg = str(record.getMessage())
        for s in self._secrets:
            if s in msg:
                record.msg = msg.replace(s, "***redacted***")
                record.args = ()
        return True
