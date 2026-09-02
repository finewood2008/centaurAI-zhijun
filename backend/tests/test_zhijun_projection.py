"""知君投影：完整视图 vs 可导出子集；USER.md 只在有已确认理解时覆盖。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mindos.stores.ontology_store import OntologyStore
from mindos.zhijun import projection


class ProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = OntologyStore(root / "ontology.db")
        self.memory_dir = root / "memory"
        self._patch = patch.object(projection, "MEMORY_DIR", self.memory_dir)
        self._patch.start()
        # 直接落盘，不经过旧记忆层（避免拉起向量库）。
        self._write = patch.object(projection, "_write", side_effect=lambda rel, content: self._raw_write(rel, content))
        self._write.start()

    def _raw_write(self, rel: str, content: str) -> None:
        target = self.memory_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def tearDown(self) -> None:
        self._write.stop()
        self._patch.stop()
        self._tmp.cleanup()

    def test_render_separates_full_and_exportable(self) -> None:
        self.store.create_claim(
            {"content": "我在做远川项目", "section": "matters", "layer": "self_declared", "export_allowed": True},
            [],
            trust_state="confirmed",
            trust_origin="utterance",
        )
        self.store.create_claim(
            {"content": "我正在处理一段家庭矛盾", "section": "people", "layer": "self_declared", "privacy_level": "sensitive", "export_allowed": True},
            [],
            trust_state="confirmed",
            trust_origin="utterance",
        )
        self.store.create_claim({"content": "我可能偏内向", "section": "who", "layer": "hypothesis"}, [])
        full, export = projection.render(self.store)
        self.assertIn("只有我确认过的内容才会出现在这里", full)
        self.assertIn("我在做远川项目", full)
        self.assertIn("我正在处理一段家庭矛盾", full)
        self.assertNotIn("我可能偏内向", full)
        self.assertIn("我在做远川项目", export)
        self.assertNotIn("我正在处理一段家庭矛盾", export)

    def test_write_projection_skips_user_md_without_confirmed(self) -> None:
        result = projection.write_projection(self.store)
        self.assertTrue((self.memory_dir / "ZHIJUN_PROFILE.md").is_file())
        self.assertFalse((self.memory_dir / "USER.md").exists())
        self.assertIsNone(result["user"])
        self.store.create_claim({"content": "我在做远川项目", "section": "matters", "layer": "self_declared"}, [], trust_state="confirmed", trust_origin="utterance")
        result = projection.write_projection(self.store)
        self.assertEqual(result["user"], "USER.md")
        self.assertIn("没有允许导出", (self.memory_dir / "USER.md").read_text(encoding="utf-8"))
        self.assertIsNotNone(self.store.meta_get("last_projection_at"))


if __name__ == "__main__":
    unittest.main()
