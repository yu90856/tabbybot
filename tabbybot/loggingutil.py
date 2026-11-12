"""Logging helpers that never print wallet keys or session tokens."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

_SECRET_KEYS = frozenset({
    "token", "wallettoken", "wallet_token", "signature", "private_key",
    "privatekey", "privkey", "authorization", "password", "passphrase",
    "master_password", "key_material", "attackticket",
    "mnemonic", "seed", "seed_phrase", "secret",
})

_HEX_KEY = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")
_BEARER = re.compile(r"Bearer\s+\S+", re.IGNORECASE)


def token_fingerprint(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return f"len={len(token)} sha256_12={digest}"


def redact_text(text: str) -> str:
    text = _BEARER.sub("Bearer <redacted>", text)
    return _HEX_KEY.sub("<redacted-hex>", text)


def redact_mapping(data: Any) -> Any:
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            if str(key).lower().replace("-", "_") in _SECRET_KEYS:
                out[key] = "<redacted>"
            else:
                out[key] = redact_mapping(value)
        return out
    if isinstance(data, list):
        return [redact_mapping(item) for item in data]
    if isinstance(data, str):
        return redact_text(data)
    return data


def short_address(address: str) -> str:
    if len(address) < 12:
        return address
    return f"{address[:8]}…{address[-6:]}"


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
