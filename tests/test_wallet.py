import json
import tempfile
import unittest
from pathlib import Path

from tabbybot.wallet import (
    WalletError,
    decrypt_wallet,
    encrypt_wallet,
    load_vault,
    normalize_private_key,
    save_vault,
)

# Well-known Hardhat/Anvil dev account #0. Not a real funded user wallet.
TEST_KEY = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


class WalletTests(unittest.TestCase):
    def test_normalize_accepts_0x_prefix(self):
        self.assertEqual(normalize_private_key("0x" + TEST_KEY.upper()), TEST_KEY)

    def test_round_trip(self):
        blob = encrypt_wallet(TEST_KEY, "super-secret")
        wallet = decrypt_wallet(blob, "super-secret")
        self.assertEqual(wallet.private_key, TEST_KEY)
        self.assertEqual(wallet.address.lower(), TEST_ADDRESS.lower())

    def test_wrong_password_fails(self):
        blob = encrypt_wallet(TEST_KEY, "super-secret")
        with self.assertRaises(WalletError):
            decrypt_wallet(blob, "wrong-password")

    def test_key_not_stored_as_plaintext(self):
        blob = encrypt_wallet(TEST_KEY, "super-secret")
        text = blob.decode("utf-8")
        self.assertNotIn(TEST_KEY, text)
        self.assertNotIn(TEST_KEY.upper(), text)
        envelope = json.loads(text)
        self.assertNotIn(TEST_KEY, envelope["ct"])
        self.assertIn("salt", envelope)
        self.assertEqual(envelope["kdf"], "scrypt")

    def test_mnemonic_derives_known_dev_account(self):
        # Official well-known test phrase (Hardhat/Anvil account 0), not a user wallet.
        phrase = "test test test test test test test test test test test junk"
        self.assertEqual(normalize_private_key(phrase), TEST_KEY)
        blob = encrypt_wallet(phrase, "super-secret")
        self.assertNotIn("test test test", blob.decode("utf-8"))
        wallet = decrypt_wallet(blob, "super-secret")
        self.assertEqual(wallet.address.lower(), TEST_ADDRESS.lower())

    def test_invalid_mnemonic_raises_wallet_error(self):
        with self.assertRaises(WalletError):
            normalize_private_key("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu")

    def test_vault_file_round_trip_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wallet.vault"
            address = save_vault(path, "0x" + TEST_KEY, "super-secret")
            self.assertEqual(address.lower(), TEST_ADDRESS.lower())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = load_vault(path, "super-secret")
            self.assertEqual(loaded.private_key, TEST_KEY)


if __name__ == "__main__":
    unittest.main()
