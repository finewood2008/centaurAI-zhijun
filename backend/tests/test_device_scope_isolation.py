"""阶段 2：业务数据按真实 device_id 作用域隔离（F3）。

覆盖：device_scope 映射、card_ledger 卡片账本与 material_pipeline 材料任务
的 device_scope 写入/读取隔离——不同设备作用域互不可见；默认 global 作用域
与既有调用兼容。
"""

from __future__ import annotations

import unittest

from mindos.device_context import SCOPE_GLOBAL, scope_for_device
from mindos.stores import card_ledger_store
from mindos.stores.material_pipeline_store import MaterialPipelineStore


class DeviceScopeIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        card_ledger_store._init()
        self.store = MaterialPipelineStore.instance()

    def test_scope_for_device_maps_id_to_device_scope(self):
        self.assertEqual(scope_for_device("dev_a"), "device:dev_a")
        self.assertEqual(scope_for_device(None), SCOPE_GLOBAL)
        self.assertEqual(scope_for_device(""), SCOPE_GLOBAL)

    def test_card_ledger_rows_are_isolated_by_device_scope(self):
        card_ledger_store.ensure("card_iso_global", "iso/global.md", "rev1", "active", device_scope=SCOPE_GLOBAL)
        card_ledger_store.ensure("card_iso_a", "iso/a.md", "rev1", "active", device_scope="device:dev_a")
        card_ledger_store.ensure("card_iso_b", "iso/b.md", "rev1", "active", device_scope="device:dev_b")

        # 各作用域只能读到自己的卡片
        self.assertEqual(card_ledger_store.get("card_iso_a", device_scope="device:dev_a")["rel_path"], "iso/a.md")
        self.assertIsNone(card_ledger_store.get("card_iso_a", device_scope="device:dev_b"))
        self.assertIsNone(card_ledger_store.get("card_iso_a"))
        self.assertEqual(card_ledger_store.get("card_iso_b", device_scope="device:dev_b")["rel_path"], "iso/b.md")
        # 默认 get 只读全局作用域
        self.assertEqual(card_ledger_store.get("card_iso_global")["rel_path"], "iso/global.md")

    def test_material_jobs_carry_device_scope(self):
        owner = "material_iso_scope"
        job_a = self.store.enqueue_material_job(owner, 1, "iso/a.txt", device_scope="device:dev_a")
        job_b = self.store.enqueue_material_job(owner, 2, "iso/b.txt", device_scope="device:dev_b")
        self.assertEqual(job_a["device_scope"], "device:dev_a")
        self.assertEqual(job_b["device_scope"], "device:dev_b")
        self.assertEqual(self.store.material_job(owner, 1)["device_scope"], "device:dev_a")
        self.assertEqual(self.store.material_job(owner, 2)["device_scope"], "device:dev_b")

    def test_default_device_scope_is_global(self):
        job = self.store.enqueue_material_job("material_iso_default", 1, "iso/default.txt")
        self.assertEqual(job["device_scope"], SCOPE_GLOBAL)


if __name__ == "__main__":
    unittest.main()
