"""runtime_settings_store 单元测试（P1 §5.2 revision 乐观锁 / §6.1 迁移元数据）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos.stores.runtime_settings_store import (
    SECTION_CHAT,
    SECTION_MATERIAL,
    RevisionConflictError,
    reset_for_tests,
)


class RuntimeSettingsStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db = str(Path(self._tmp.name) / "runtime_settings.db")
        self.store = reset_for_tests(self._db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _payload(self, model: str = "m") -> dict:
        return {"baseUrl": "http://127.0.0.1:11434", "model": model, "timeoutSeconds": 180}

    def test_empty_is_none(self) -> None:
        self.assertIsNone(self.store.get_section(SECTION_MATERIAL))

    def test_first_put_baseline(self) -> None:
        row = self.store.put_section(SECTION_MATERIAL, None, self._payload("qwen3:4b"))
        self.assertEqual(row["revision"], 1)
        self.assertEqual(row["source"], "runtime_settings")
        got = self.store.get_section(SECTION_MATERIAL)
        self.assertEqual(got["payload"]["model"], "qwen3:4b")
        self.assertEqual(got["revision"], 1)

    def test_put_with_explicit_zero_baseline(self) -> None:
        row = self.store.put_section(SECTION_MATERIAL, 0, self._payload())
        self.assertEqual(row["revision"], 1)

    def test_update_increments_revision(self) -> None:
        self.store.put_section(SECTION_MATERIAL, None, self._payload())
        row = self.store.put_section(SECTION_MATERIAL, 1, self._payload("qwen3:8b"))
        self.assertEqual(row["revision"], 2)

    def test_stale_revision_conflict(self) -> None:
        self.store.put_section(SECTION_MATERIAL, None, self._payload())
        with self.assertRaises(RevisionConflictError) as ctx:
            self.store.put_section(SECTION_MATERIAL, 0, self._payload())
        self.assertEqual(ctx.exception.latest["revision"], 1)

    def test_conflict_carries_latest_payload(self) -> None:
        self.store.put_section(SECTION_CHAT, None, {"provider": "ollama"})
        self.store.put_section(SECTION_CHAT, 1, {"provider": "openai"})
        with self.assertRaises(RevisionConflictError) as ctx:
            self.store.put_section(SECTION_CHAT, 1, {"provider": "ollama"})
        self.assertEqual(ctx.exception.latest["payload"]["provider"], "openai")

    def test_secret_ref_roundtrip(self) -> None:
        self.store.put_section(
            SECTION_CHAT, None, {"provider": "openai"}, secret_ref="mindos_rt_x"
        )
        got = self.store.get_section(SECTION_CHAT)
        self.assertEqual(got["secret_ref"], "mindos_rt_x")

    def test_meta_get_put(self) -> None:
        self.assertIsNone(self.store.get_meta("k"))
        self.store.put_meta("k", {"state": "active"})
        self.assertEqual(self.store.get_meta("k")["state"], "active")

    def test_unknown_section_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.get_section("nope")


if __name__ == "__main__":
    unittest.main()
