import subprocess
import unittest
from types import SimpleNamespace

from tabbybot.keychain import DarwinKeychain, KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE, MemoryKeychain


class MemoryKeychainTests(unittest.TestCase):
    def test_save_load_delete(self):
        store = MemoryKeychain()
        self.assertFalse(store.has_password())
        store.save("secret-pass")
        self.assertTrue(store.has_password())
        self.assertEqual(store.load(), "secret-pass")
        store.delete()
        self.assertFalse(store.has_password())
        self.assertIsNone(store.load())


class DarwinKeychainTests(unittest.TestCase):
    def test_uses_requested_security_commands(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(list(args))
            self.assertTrue(kwargs.get("capture_output"))
            self.assertTrue(kwargs.get("text"))
            self.assertFalse(kwargs.get("check"))
            if args[1] == "find-generic-password":
                return SimpleNamespace(returncode=0, stdout="stored-secret\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        store = DarwinKeychain(runner=runner)
        store.save("stored-secret")
        self.assertEqual(store.load(), "stored-secret")
        store.delete()

        add = next(args for args in calls if args[1] == "add-generic-password")
        self.assertEqual(add[:7], [
            "security", "add-generic-password",
            "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w",
        ])
        self.assertEqual(add[7], "stored-secret")
        find = next(args for args in calls if args[1] == "find-generic-password")
        self.assertEqual(find, [
            "security", "find-generic-password",
            "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w",
        ])
        delete = next(args for args in calls if args[1] == "delete-generic-password")
        self.assertEqual(delete, [
            "security", "delete-generic-password",
            "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
        ])

    def test_missing_item_is_none(self):
        def runner(args, **kwargs):
            return SimpleNamespace(returncode=44, stdout="", stderr="not found")

        store = DarwinKeychain(runner=runner)
        self.assertIsNone(store.load())
        self.assertFalse(store.has_password())

    def test_timeout_does_not_include_secret_in_error(self):
        def runner(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="security", timeout=8)

        store = DarwinKeychain(runner=runner)
        with self.assertRaises(Exception) as caught:
            store.save("super-secret-password")
        self.assertNotIn("super-secret-password", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
