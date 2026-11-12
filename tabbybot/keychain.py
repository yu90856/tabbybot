"""Opt-in master-password storage. Production uses macOS Keychain only."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import Callable, Optional

log = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "tabbybot"
KEYCHAIN_ACCOUNT = "vault"


class KeychainError(Exception):
    pass


class KeychainStore:
    """Store the vault master password. Implementations must never log the secret."""

    def save(self, password: str) -> None:
        raise NotImplementedError

    def load(self) -> Optional[str]:
        raise NotImplementedError

    def delete(self) -> None:
        raise NotImplementedError

    def has_password(self) -> bool:
        return bool(self.load())


class MemoryKeychain(KeychainStore):
    """In-memory store for tests. Does not touch the OS keychain."""

    def __init__(self) -> None:
        self._password: Optional[str] = None

    def save(self, password: str) -> None:
        if not password:
            raise KeychainError("主密碼是空的，無法記住。")
        self._password = password

    def load(self) -> Optional[str]:
        return self._password

    def delete(self) -> None:
        self._password = None


class NoopKeychain(KeychainStore):
    """Used when macOS Keychain is not available."""

    def save(self, password: str) -> None:
        raise KeychainError("這台電腦沒有 macOS 鑰匙圈，無法記住主密碼。")

    def load(self) -> Optional[str]:
        return None

    def delete(self) -> None:
        return None


class DarwinKeychain(KeychainStore):
    """macOS Keychain via /usr/bin/security. Password is never written to logs."""

    def __init__(
        self,
        service: str = KEYCHAIN_SERVICE,
        account: str = KEYCHAIN_ACCOUNT,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        security_bin: str = "security",
    ) -> None:
        self.service = service
        self.account = account
        self._runner = runner or subprocess.run
        self.security_bin = security_bin

    def _run(self, args: list[str], *, timeout: int = 8) -> subprocess.CompletedProcess:
        # Never log args: they may include the password after -w.
        try:
            return self._runner(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise KeychainError("讀寫鑰匙圈逾時。") from exc
        except OSError as exc:
            raise KeychainError("無法執行鑰匙圈指令。") from exc

    def save(self, password: str) -> None:
        if not password:
            raise KeychainError("主密碼是空的，無法記住。")
        self.delete()
        result = self._run([
            self.security_bin,
            "add-generic-password",
            "-s", self.service,
            "-a", self.account,
            "-w", password,
        ])
        if result.returncode != 0:
            raise KeychainError("無法把主密碼寫入鑰匙圈。")

    def load(self) -> Optional[str]:
        result = self._run([
            self.security_bin,
            "find-generic-password",
            "-s", self.service,
            "-a", self.account,
            "-w",
        ])
        if result.returncode != 0:
            return None
        value = result.stdout or ""
        if value.endswith("\n"):
            value = value[:-1]
        return value or None

    def delete(self) -> None:
        self._run([
            self.security_bin,
            "delete-generic-password",
            "-s", self.service,
            "-a", self.account,
        ])

    def has_password(self) -> bool:
        return self.load() is not None


def keychain_available() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def default_keychain() -> KeychainStore:
    if keychain_available():
        return DarwinKeychain()
    return NoopKeychain()
