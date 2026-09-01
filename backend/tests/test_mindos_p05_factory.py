"""P0-5 上游风险验证与缓解专项测试。

覆盖方案 §P0-5 可编码部分：
- 统一 collection 工厂：全部创建点经 get_or_create_collection，登记集合名供自检枚举。
- 候选 sync_threshold 仅显式启用时注入，默认不改变现有集合 metadata。
- 启动自检（verify_chroma_health）：count + 最小 get 探测；损坏返回 corrupted 与恢复建议。
- 三态读取不受工厂改造影响（P0-3 契约冒烟）。
"""
import unittest
from unittest.mock import MagicMock, patch

import vector_store as vs


class FactoryRegistrationTests(unittest.TestCase):
    """统一工厂：metadata 构造 + 集合登记 + 禁用/启用 sync_threshold。"""

    def setUp(self):
        vs._REGISTERED_COLLECTIONS.clear()

    def test_factory_injects_space_and_registers(self):
        fake_col = MagicMock()
        client = MagicMock()
        client.get_or_create_collection.return_value = fake_col
        with patch.object(vs, "_get_delta_client", return_value=client), \
             patch.object(vs, "CHROMA_SYNC_THRESHOLD_ENABLED", False):
            col = vs.get_or_create_collection("abc")

        self.assertIs(col, fake_col)
        _, kwargs = client.get_or_create_collection.call_args
        self.assertEqual(kwargs["metadata"]["hnsw:space"], "cosine")
        # 默认未启用候选阈值，不注入，避免改动现有集合
        self.assertNotIn("hnsw:sync_threshold", kwargs["metadata"])
        self.assertEqual(vs.registered_collection_names(), ["abc"])

    def test_factory_injects_sync_threshold_when_enabled(self):
        client = MagicMock()
        with patch.object(vs, "_get_delta_client", return_value=client), \
             patch.object(vs, "CHROMA_SYNC_THRESHOLD_ENABLED", True), \
             patch.object(vs, "CHROMA_SYNC_THRESHOLD", "100000"):
            vs.get_or_create_collection("xyz")

        _, kwargs = client.get_or_create_collection.call_args
        self.assertEqual(kwargs["metadata"]["hnsw:sync_threshold"], 100000)

    def test_registered_names_sorted_and_unique(self):
        client = MagicMock()
        with patch.object(vs, "_get_delta_client", return_value=client), \
             patch.object(vs, "CHROMA_SYNC_THRESHOLD_ENABLED", False):
            vs.get_or_create_collection("b")
            vs.get_or_create_collection("a")
            vs.get_or_create_collection("b")
        self.assertEqual(vs.registered_collection_names(), ["a", "b"])


class HealthCheckTests(unittest.TestCase):
    """启动自检（修复5 契约）：按静态受管清单枚举；
    未创建→not_created（健康空库）；已创建 count/get 异常→corrupted + 恢复建议。"""

    def _client(self, existing=None, side_effect_get=None, side_effect_count=None):
        col = MagicMock()
        if side_effect_count is not None:
            col.count.side_effect = side_effect_count
        if side_effect_get is not None:
            col.get.side_effect = side_effect_get
        client = MagicMock()
        client.get_collection.return_value = col
        if existing is not None:
            client.list_collections.return_value = list(existing)
        return client

    def _patch_clients(self, client):
        """同时替换 base/delta client（verify_chroma_health 双代际探测）。"""
        p_base = patch.object(vs, "_get_base_client", return_value=client)
        p_delta = patch.object(vs, "_get_delta_client", return_value=client)
        p_base.start()
        p_delta.start()
        self.addCleanup(p_base.stop)
        self.addCleanup(p_delta.stop)

    @patch.object(vs, "registered_collection_names", return_value=[])
    def test_healthy_reports_ok(self, _):
        names = vs.managed_collection_names()
        client = self._client(existing=names)
        self._patch_clients(client)
        result = vs.verify_chroma_health()
        self.assertTrue(result["ok"])
        self.assertEqual(result["collections"], len(names))
        self.assertTrue(all(e["status"] == vs.HEALTH_OK for e in result["checked"]))
        self.assertEqual(result["issues"], [])

    @patch.object(vs, "registered_collection_names", return_value=[])
    def test_count_failure_reports_corrupted_with_recovery_hint(self, _):
        names = vs.managed_collection_names()
        client = self._client(
            existing=names, side_effect_count=RuntimeError("load hnsw index")
        )
        self._patch_clients(client)
        result = vs.verify_chroma_health()
        self.assertFalse(result["ok"])
        self.assertTrue(
            all(e["status"] == vs.HEALTH_CORRUPTED for e in result["checked"])
        )
        self.assertGreater(len(result["issues"]), 0)
        self.assertIn("恢复流程", result["issues"][0])

    @patch.object(vs, "registered_collection_names", return_value=[])
    def test_not_created_is_healthy_empty_library(self, _):
        """全新数据目录：集合尚未创建 → not_created，绝不误报损坏（修复5）。"""
        client = self._client(existing=[])
        self._patch_clients(client)
        result = vs.verify_chroma_health()
        self.assertTrue(result["ok"])
        self.assertEqual(result["collections"], len(vs.managed_collection_names()))
        self.assertTrue(
            all(e["status"] == vs.HEALTH_NOT_CREATED for e in result["checked"])
        )
        self.assertEqual(result["issues"], [])

    def test_no_registrations_covers_static_managed_collections(self):
        """尚未触发工厂（惰性集合未登记）时，仍按静态受管清单全量探测。"""
        client = self._client(existing=[])
        with patch.object(vs, "registered_collection_names", return_value=[]):
            self._patch_clients(client)
            result = vs.verify_chroma_health()
        self.assertTrue(result["ok"])
        self.assertEqual(result["collections"], len(vs.managed_collection_names()))
        checked = {e["name"] for e in result["checked"]}
        self.assertEqual(checked, set(vs.managed_collection_names()))


if __name__ == "__main__":
    unittest.main()