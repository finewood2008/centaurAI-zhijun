"""P1-4 测试环境强制隔离验证（索引可靠性方案 §P1-4）。

验证根 conftest 已在任何业务模块 import 前把可变数据根指向临时目录：
- runtime_paths.DATA_ROOT 不等于生产 data 根（PROJECT_ROOT/data）；
- Chroma / watch_folder / db 等全部落在隔离的数据根下；
- 测试环境不再允许用生产数据根（一旦回退到生产路径即失败）。

用例依赖 conftest 在收集期设置 CENTAURAI_DATABASE_DATA_ROOT 后加载本文件。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runtime_paths
import config
import vector_store


class TestDataRootIsolation(unittest.TestCase):
    def setUp(self):
        self.data_root = Path(runtime_paths.DATA_ROOT).resolve()
        self.project_root = Path(runtime_paths.PROJECT_ROOT).resolve()
        self.production_root = (self.project_root / "data").resolve()

    def test_data_root_is_isolated_from_production(self):
        """可变数据根必须落在临时目录，绝不等于/绝不嵌套于生产 data/。"""
        self.assertNotEqual(self.data_root, self.production_root)
        self.assertTrue(
            "mindos-test-data" in self.data_root.name
            and not self.production_root in self.data_root.parents,
            f"数据根未隔离: {self.data_root}",
        )

    def test_chroma_dir_follows_isolated_data_root(self):
        chroma = Path(config.CHROMA_DATA_DIR).resolve()
        self.assertTrue(
            chroma == self.data_root or self.data_root in chroma.parents,
            f"Chroma 目录未落在隔离数据根: {chroma}",
        )
        self.assertNotEqual(chroma, self.production_root / "chroma_data")

    def test_watch_and_db_follow_isolated_data_root(self):
        for name in (runtime_paths.WATCH_FOLDER, runtime_paths.DB_ROOT, runtime_paths.MEMORY_DIR):
            p = Path(name).resolve()
            self.assertTrue(
                p == self.data_root or self.data_root in p.parents,
                f"{name} 未落在隔离数据根: {p}",
            )

    def test_vector_store_uses_isolated_chroma_dir(self):
        """vector_store 读取的 Chroma 数据目录必须与隔离数据根一致。"""
        self.assertEqual(
            Path(vector_store.CHROMA_DATA_DIR).resolve(),
            Path(config.CHROMA_DATA_DIR).resolve(),
        )

    def test_env_var_is_set(self):
        self.assertIn("CENTAURAI_DATABASE_DATA_ROOT", os.environ)
        self.assertEqual(
            Path(os.environ["CENTAURAI_DATABASE_DATA_ROOT"]).resolve(), self.data_root
        )


if __name__ == "__main__":
    unittest.main()