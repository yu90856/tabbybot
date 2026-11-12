"""Local wallet storage: scrypt + AES-GCM. Keys never leave this module unencrypted."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from eth_account import Account

_VAULT_VERSION = 1
_SALT_LEN = 16
_NONCE_LEN = 12
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_MIN_PASSWORD = 8
_HEX_KEY = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")


class WalletError(Exception):
    pass


def default_vault_path() -> Path:
    return Path.home() / ".tabbybot" / "wallet.vault"


def _hex_from_bytes(value: object) -> str:
    raw = value.hex() if hasattr(value, "hex") else str(value)
    if raw.lower().startswith("0x"):
        raw = raw[2:]
    return raw.lower()


def normalize_private_key(value: str) -> str:
    key = value.strip()
    words = key.split()
    index = 0  # optional trailing number = which sub-wallet (1 = first, like MetaMask Account 1)
    if len(words) - 1 in (12, 15, 18, 21, 24) and words[-1].isdigit():
        index = max(0, int(words[-1]) - 1)
        words = words[:-1]
    if len(words) in (12, 15, 18, 21, 24):
        try:
            Account.enable_unaudited_hdwallet_features()
            account = Account.from_mnemonic(" ".join(words).lower(), account_path=f"m/44'/60'/0'/0/{index}")
        except WalletError:
            raise
        except Exception as exc:
            raise WalletError("助記詞無效，請確認是 12 到 24 個英文單字。") from exc
        return _hex_from_bytes(account.key)
    if not _HEX_KEY.match(key):
        raise WalletError("私鑰或助記詞格式無效。請貼上 64 位十六進位私鑰，或 12 到 24 個英文單字的助記詞。")
    if key.lower().startswith("0x"):
        key = key[2:]
    return key.lower()


def address_from_key(private_key: str) -> str:
    return Account.from_key(normalize_private_key(private_key)).address


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=salt, length=_KEY_LEN, n=n, r=r, p=p)
    return kdf.derive(password.encode("utf-8"))


def _secure_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


@dataclass(frozen=True)
class StoredWallet:
    address: str
    private_key: str

    def cleared(self) -> None:
        # Best-effort documentation: callers should drop references after signing.
        return None


def encrypt_wallet(private_key: str, password: str) -> bytes:
    if len(password) < _MIN_PASSWORD:
        raise WalletError(f"主密碼至少需要 {_MIN_PASSWORD} 個字元。")
    key_hex = normalize_private_key(private_key)
    address = Account.from_key(key_hex).address
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    aes_key = _derive(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    aes = AESGCM(aes_key)
    plaintext = json.dumps({"address": address, "private_key": key_hex}, separators=(",", ":")).encode("utf-8")
    ciphertext = aes.encrypt(nonce, plaintext, None)
    envelope = {
        "v": _VAULT_VERSION,
        "kdf": "scrypt",
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "ct": ciphertext.hex(),
    }
    return (json.dumps(envelope, indent=2) + "\n").encode("utf-8")


def decrypt_wallet(blob: bytes, password: str) -> StoredWallet:
    try:
        envelope = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WalletError("保險庫檔案損壞或不是有效的 JSON。") from exc
    if not isinstance(envelope, dict) or envelope.get("v") != _VAULT_VERSION:
        raise WalletError("不支援的保險庫版本。")
    try:
        salt = bytes.fromhex(envelope["salt"])
        nonce = bytes.fromhex(envelope["nonce"])
        ciphertext = bytes.fromhex(envelope["ct"])
        n, r, p = int(envelope["n"]), int(envelope["r"]), int(envelope["p"])
    except (KeyError, ValueError, TypeError) as exc:
        raise WalletError("保險庫欄位不完整。") from exc
    aes_key = _derive(password, salt, n, r, p)
    try:
        plaintext = AESGCM(aes_key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise WalletError("主密碼不正確，或保險庫已被改動。") from exc
    try:
        payload = json.loads(plaintext.decode("utf-8"))
        key_hex = normalize_private_key(payload["private_key"])
        address = Account.from_key(key_hex).address
    except (KeyError, ValueError, TypeError, json.JSONDecodeError, WalletError) as exc:
        raise WalletError("保險庫內容無法解析。") from exc
    if payload.get("address") and str(payload["address"]).lower() != address.lower():
        raise WalletError("保險庫中的地址與私鑰不符。")
    return StoredWallet(address=address, private_key=key_hex)


def save_vault(path: Path, private_key: str, password: str) -> str:
    blob = encrypt_wallet(private_key, password)
    _secure_write(path, blob)
    wallet = decrypt_wallet(path.read_bytes(), password)
    if wallet.private_key != normalize_private_key(private_key):
        raise WalletError("寫入後校驗失敗，未信任此次保存。")
    return wallet.address


def load_vault(path: Path, password: str) -> StoredWallet:
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise WalletError(f"無法讀取保險庫：{path}") from exc
    return decrypt_wallet(blob, password)
