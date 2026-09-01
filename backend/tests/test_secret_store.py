"""secret_store 单元测试（P1 §5.3 / §5.2.1）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos import secret_store as ss
from mindos.secret_store import (
    EncryptedFileSecretStore,
    EncryptedSQLiteSecretStore,
    MemorySecretStore,
    SECRET_PREFIX,
    SecretStore,
    UnavailableSecretStore,
    cleanup_orphan_secrets,
    get_default_secret_store,
    new_secret_ref,
)


class NewSecretRefTest(unittest.TestCase):
    def test_prefix_and_uniqueness(self) -> None:
        a = new_secret_ref()
        b = new_secret_ref()
        self.assertTrue(a.startswith(SECRET_PREFIX))
        self.assertNotEqual(a, b)


class MemorySecretStoreTest(unittest.TestCase):
    def test_roundtrip_delete_list(self) -> None:
        store = MemorySecretStore()
        ref = new_secret_ref()
        store.set_secret(ref, "sk-test")
        self.assertEqual(store.get_secret(ref), "sk-test")
        store.set_secret("other_key", "v")  # 不带前缀不应被列出
        self.assertIn(ref, store.list_secret_refs())
        self.assertNotIn("other_key", store.list_secret_refs())
        store.delete_secret(ref)
        self.assertIsNone(store.get_secret(ref))


class EncryptedFileSecretStoreTest(unittest.TestCase):
    @staticmethod
    def _key() -> str:
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode()

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = EncryptedFileSecretStore(directory=d, master_key_b64=self._key())
            ref = new_secret_ref()
            store.set_secret(ref, "sk-secret")
            self.assertEqual(store.get_secret(ref), "sk-secret")
            self.assertIn(ref, store.list_secret_refs())
            store.delete_secret(ref)
            self.assertIsNone(store.get_secret(ref))

    def test_wrong_key_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = EncryptedFileSecretStore(directory=d, master_key_b64=self._key())
            ref = new_secret_ref()
            store.set_secret(ref, "sk-secret")
            other = EncryptedFileSecretStore(directory=d, master_key_b64=self._key())
            self.assertIsNone(other.get_secret(ref))

    def test_cleanup_honors_valid_refs_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = EncryptedFileSecretStore(directory=d, master_key_b64=self._key())
            keep = new_secret_ref()
            stale = new_secret_ref()
            store.set_secret(keep, "keep")
            store.set_secret(stale, "stale")
            removed = cleanup_orphan_secrets(store, valid_refs={keep}, retention_seconds=0)
            self.assertIn(stale, removed)
            self.assertNotIn(keep, removed)
            self.assertEqual(store.get_secret(keep), "keep")
            self.assertIsNone(store.get_secret(stale))
            # 年龄小于保留期不删
            fresh = new_secret_ref()
            store.set_secret(fresh, "fresh")
            removed2 = cleanup_orphan_secrets(store, valid_refs=set(), retention_seconds=3600)
            self.assertNotIn(fresh, removed2)


class EncryptedSQLiteSecretStoreTest(unittest.TestCase):
    @staticmethod
    def _key() -> str:
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode()

    def test_roundtrip_stores_ciphertext_not_plaintext(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "runtime_settings.db"
            store = EncryptedSQLiteSecretStore(db_path=db_path, master_key_b64=self._key())
            ref = new_secret_ref()
            store.set_secret(ref, "sk-secret")
            self.assertEqual(store.get_secret(ref), "sk-secret")
            conn = sqlite3.connect(str(db_path))
            try:
                ciphertext = conn.execute(
                    "SELECT ciphertext FROM encrypted_secrets WHERE ref=?", (ref,)
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertNotIn(b"sk-secret", bytes(ciphertext))
            store.delete_secret(ref)
            self.assertIsNone(store.get_secret(ref))

    def test_wrong_key_cannot_decrypt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "runtime_settings.db"
            store = EncryptedSQLiteSecretStore(db_path=db_path, master_key_b64=self._key())
            ref = new_secret_ref()
            store.set_secret(ref, "sk-secret")
            other = EncryptedSQLiteSecretStore(db_path=db_path, master_key_b64=self._key())
            self.assertIsNone(other.get_secret(ref))

    def test_managed_key_is_created_once_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as d, patch.dict(
            "os.environ", {"CENTAUR_SECRET_STORE_KEY": ""}, clear=False
        ):
            db_path = Path(d) / "runtime_settings.db"
            store = EncryptedSQLiteSecretStore(db_path=db_path)
            ref = new_secret_ref()
            store.set_secret(ref, "sk-secret")
            restarted = EncryptedSQLiteSecretStore(db_path=db_path)
            self.assertEqual(restarted.get_secret(ref), "sk-secret")


class UnavailableSecretStoreTest(unittest.TestCase):
    def test_set_raises(self) -> None:
        store = UnavailableSecretStore()
        with self.assertRaises(RuntimeError):
            store.set_secret(new_secret_ref(), "x")
        self.assertEqual(store.list_secret_refs(), [])


class GetDefaultSecretStoreTest(unittest.TestCase):
    def test_returns_a_store_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as d, patch.object(
            ss, "RUNTIME_SETTINGS_DB_PATH", Path(d) / "runtime_settings.db"
        ):
            store = get_default_secret_store()
            self.assertIsInstance(store, EncryptedSQLiteSecretStore)


if __name__ == "__main__":
    unittest.main()
