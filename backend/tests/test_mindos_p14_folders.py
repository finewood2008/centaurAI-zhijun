"""MindOS P14-06 原材料多级目录树测试。

覆盖目录树 ID API 的核心业务规则：
- 创建根/子目录、同 scope+parent 名称唯一
- 重命名/移动（禁止移动到自身或后代）、删除必须明确迁移目标
- 材料移动与上传使用 folderId；folder 仅保留兼容读
- 子树筛选（选中目录 + 全部后代）
- 旧单层文件夹幂等迁移为 RAW 根节点
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException

from mindos.stores import job_store
from mindos.stores.job_store import (
    FolderError,
    FolderNameConflictError,
    FolderNotFoundError,
    SCOPE_RAW,
)
from mindos.services import ingestion
from mindos import uploads


def _store() -> job_store.JobStore:
    return job_store.JobStore.instance()


class FolderNodeStoreTests(unittest.TestCase):
    """store 层：folder_nodes 树结构 + 业务规则。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        job_store.reset_for_tests(Path(self._tmp.name) / "jobs.db")

    def tearDown(self):
        job_store.reset_for_tests()
        self._tmp.cleanup()

    def _tree(self):
        """搭建 工作/2026/预算 三层目录，返回 (root, a, b)。"""
        root = _store().create_folder_node(SCOPE_RAW, "工作")
        a = _store().create_folder_node(SCOPE_RAW, "2026", parent_id=root["id"])
        b = _store().create_folder_node(SCOPE_RAW, "预算", parent_id=a["id"])
        return root, a, b

    def test_create_root_and_child(self):
        root, a, b = self._tree()
        self.assertIsNone(root["parentId"])
        self.assertEqual(a["parentId"], root["id"])
        self.assertEqual(b["parentId"], a["id"])
        self.assertEqual(b["name"], "预算")
        self.assertEqual(b["scope"], SCOPE_RAW)

    def test_duplicate_name_same_parent_rejected(self):
        _store().create_folder_node(SCOPE_RAW, "工作")
        with self.assertRaises(FolderNameConflictError) as ctx:
            _store().create_folder_node(SCOPE_RAW, "工作")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_same_name_different_parent_allowed(self):
        root, a, b = self._tree()  # 工作/2026/预算
        other = _store().create_folder_node(SCOPE_RAW, "项目")
        # "预算" 已存在于 a 下，在 root 下允许同名（root 下现有子节点仅 "2026"）
        d1 = _store().create_folder_node(SCOPE_RAW, "预算", parent_id=root["id"])
        # "2026" 已存在于 root 下，在 other 下允许同名
        d2 = _store().create_folder_node(SCOPE_RAW, "2026", parent_id=other["id"])
        self.assertEqual(d1["parentId"], root["id"])
        self.assertEqual(d2["parentId"], other["id"])
        self.assertNotEqual(d1["id"], d2["id"])

    def test_invalid_name_rejected(self):
        store = _store()
        for bad in ("", "  ", "未分类", "a/b", "a\\b", "x" * 121):
            with self.assertRaises(FolderError, msg=f"name={bad!r}"):
                store.create_folder_node(SCOPE_RAW, bad)

    def test_parent_not_found(self):
        with self.assertRaises(FolderNotFoundError) as ctx:
            _store().create_folder_node(SCOPE_RAW, "孤儿", parent_id=99999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_create_invalid_scope_rejected(self):
        with self.assertRaises(FolderError):
            _store().create_folder_node("OTHER", "非法作用域")
        # 白名单内 scope（KNOWLEDGE 为 P14-07 底座）仍可创建
        node = _store().create_folder_node("KNOWLEDGE", "知识目录")
        self.assertEqual(node["scope"], "KNOWLEDGE")

    def test_create_cross_scope_child_rejected(self):
        """子目录必须与父节点同 scope，禁止跨作用域创建（否则产生前端不可见的孤儿节点）。"""
        knode = _store().create_folder_node("KNOWLEDGE", "知识目录")
        with self.assertRaises(FolderError):
            _store().create_folder_node(SCOPE_RAW, "材料目录", parent_id=knode["id"])
        # RAW 根 → KNOWLEDGE 子同样被拒
        rnode = _store().create_folder_node(SCOPE_RAW, "工作")
        with self.assertRaises(FolderError):
            _store().create_folder_node("KNOWLEDGE", "知识子目录", parent_id=rnode["id"])

    def test_raw_tree_parents_all_in_scope(self):
        """RAW 返回树中所有非根节点父级均在同 scope 列表内（无孤儿）。"""
        self._tree()
        nodes = _store().list_folder_nodes(SCOPE_RAW)
        ids = {n["id"] for n in nodes}
        for n in nodes:
            if n["parentId"] is not None:
                self.assertIn(n["parentId"], ids)

    def test_rename_ok_and_duplicate_rejected(self):
        root, a, _ = self._tree()
        renamed = _store().rename_folder_node(a["id"], "2027")
        self.assertEqual(renamed["name"], "2027")
        # 与 a 同父同级的另一节点重命名为 "2027" 必须冲突
        sibling = _store().create_folder_node(SCOPE_RAW, "其他", parent_id=root["id"])
        with self.assertRaises(FolderNameConflictError):
            _store().rename_folder_node(sibling["id"], "2027")

    def test_move_to_self_rejected(self):
        _, a, _ = self._tree()
        with self.assertRaises(FolderError):
            _store().move_folder_node(a["id"], a["id"])

    def test_move_to_descendant_rejected(self):
        root, a, b = self._tree()
        with self.assertRaises(FolderError):
            _store().move_folder_node(root["id"], b["id"])
        with self.assertRaises(FolderError):
            _store().move_folder_node(a["id"], b["id"])

    def test_move_scope_mismatch_rejected(self):
        root, a, _ = self._tree()
        knode = _store().create_folder_node("KNOWLEDGE", "知识目录")
        with self.assertRaises(FolderError):
            _store().move_folder_node(a["id"], knode["id"])

    def test_move_reparents(self):
        root, a, b = self._tree()
        moved = _store().move_folder_node(b["id"], root["id"])
        self.assertEqual(moved["parentId"], root["id"])
        nodes = _store().list_folder_nodes(SCOPE_RAW)
        b_after = next(n for n in nodes if n["id"] == b["id"])
        self.assertEqual(b_after["parentId"], root["id"])

    def test_delete_requires_target_or_move_to_root(self):
        _, a, _ = self._tree()
        with self.assertRaises(FolderError):
            _store().delete_folder_node(a["id"])
        # move_to_root=true 允许
        result = _store().delete_folder_node(a["id"], move_to_root=True)
        self.assertIn("movedMaterials", result)

    def test_delete_moves_materials_and_reparents_children(self):
        root, a, b = self._tree()
        store = _store()
        store.register("mindos_f1", "p1.pdf", "document", "/tmp/p1.pdf", folder_id=a["id"])
        store.register("mindos_f2", "p2.pdf", "document", "/tmp/p2.pdf", folder_id=b["id"])
        result = store.delete_folder_node(a["id"], target_folder_id=root["id"])
        self.assertEqual(result["movedMaterials"], 1)  # 仅 a 直接归类的资料
        self.assertEqual(result["reparentedFolders"], 1)  # b 提升到 root 下
        nodes = store.list_folder_nodes(SCOPE_RAW)
        b_after = next(n for n in nodes if n["id"] == b["id"])
        self.assertEqual(b_after["parentId"], root["id"])
        self.assertEqual(store.get("mindos_f1")["folder_id"], root["id"])
        self.assertEqual(store.get("mindos_f2")["folder_id"], b["id"])
        self.assertEqual(store.folder_path(b["id"]), "工作/预算")

    def test_delete_target_self_or_descendant_rejected(self):
        root, a, b = self._tree()
        with self.assertRaises(FolderError):
            _store().delete_folder_node(a["id"], target_folder_id=a["id"])
        with self.assertRaises(FolderError):
            _store().delete_folder_node(a["id"], target_folder_id=b["id"])
        with self.assertRaises(FolderNotFoundError):
            _store().delete_folder_node(a["id"], target_folder_id=99999)

    def _delete_conflict_tree(self):
        """结构：工作/待删除/预算 + 工作/目标/预算（目标下与待删除子目录同名）。"""
        store = _store()
        root = store.create_folder_node(SCOPE_RAW, "工作")
        doomed = store.create_folder_node(SCOPE_RAW, "待删除", parent_id=root["id"])
        b = store.create_folder_node(SCOPE_RAW, "预算", parent_id=doomed["id"])
        target = store.create_folder_node(SCOPE_RAW, "目标", parent_id=root["id"])
        store.create_folder_node(SCOPE_RAW, "预算", parent_id=target["id"])
        store.register("mindos_cc1", "p.pdf", "document", "/tmp/p.pdf", folder_id=doomed["id"])
        return root, doomed, b, target

    def test_delete_conflict_with_target_sibling_rejected(self):
        _, doomed, b, target = self._delete_conflict_tree()
        with self.assertRaises(FolderNameConflictError) as ctx:
            _store().delete_folder_node(doomed["id"], target_folder_id=target["id"])
        self.assertEqual(ctx.exception.status_code, 409)
        # 原子性：失败后树与资料归属均不变
        store = _store()
        self.assertEqual(store.get("mindos_cc1")["folder_id"], doomed["id"])
        nodes = {n["id"]: n for n in store.list_folder_nodes(SCOPE_RAW)}
        self.assertIsNotNone(nodes.get(doomed["id"]))
        self.assertEqual(nodes[b["id"]]["parentId"], doomed["id"])

    def test_delete_conflict_with_root_sibling_rejected(self):
        root, doomed, b, _ = self._delete_conflict_tree()
        # 顶级（parent_id=NULL）已有同名「预算」：move_to_root 提升子目录也应冲突
        _store().create_folder_node(SCOPE_RAW, "预算")
        with self.assertRaises(FolderNameConflictError) as ctx:
            _store().delete_folder_node(doomed["id"], move_to_root=True)
        self.assertEqual(ctx.exception.status_code, 409)
        # 失败后目录与资料保持原状
        store = _store()
        self.assertEqual(store.get("mindos_cc1")["folder_id"], doomed["id"])
        b_after = next(n for n in store.list_folder_nodes(SCOPE_RAW) if n["name"] == "预算" and n["parentId"] == doomed["id"])
        self.assertEqual(b_after["id"], b["id"])

    def test_delete_move_to_root_clears_legacy_folder_and_survives_restart(self):
        """删除并迁移到根目录：兼容列 folder 置「未分类」，重启后目录不复活。"""
        store = _store()
        node = store.create_folder_node(SCOPE_RAW, "待删除")
        store.register("mindos_gone1", "p.pdf", "document", "/tmp/p.pdf", folder_id=node["id"])
        result = store.delete_folder_node(node["id"], move_to_root=True)
        self.assertEqual(result["movedMaterials"], 1)
        rec = store.get("mindos_gone1")
        self.assertIsNone(rec["folder_id"])
        self.assertEqual(rec["folder"], "未分类")
        # 模拟重启：同一 DB 重新初始化会重跑 _migrate_legacy_folders
        job_store.reset_for_tests(Path(self._tmp.name) / "jobs.db")
        store = _store()
        self.assertEqual([n["name"] for n in store.list_folder_nodes(SCOPE_RAW)], [])
        rec = store.get("mindos_gone1")
        self.assertIsNone(rec["folder_id"])
        self.assertEqual(rec["folder"], "未分类")

    def test_delete_move_to_target_syncs_legacy_folder(self):
        """删除并迁移到指定目标：直接资料的兼容列 folder 同步为目标目录名。"""
        store = _store()
        doomed = store.create_folder_node(SCOPE_RAW, "待删除")
        target = store.create_folder_node(SCOPE_RAW, "目标")
        store.register("mindos_gone2", "p.pdf", "document", "/tmp/p.pdf", folder_id=doomed["id"])
        result = store.delete_folder_node(doomed["id"], target_folder_id=target["id"])
        self.assertEqual(result["movedMaterials"], 1)
        rec = store.get("mindos_gone2")
        self.assertEqual(rec["folder_id"], target["id"])
        self.assertEqual(rec["folder"], "目标")

    def test_update_material_folder_id(self):
        root, a, _ = self._tree()
        store = _store()
        store.register("mindos_m1", "p.pdf", "document", "/tmp/p.pdf")
        self.assertIsNone(store.get("mindos_m1")["folder_id"])
        # 移到目录
        rec = store.update_material_folder_id("mindos_m1", a["id"])
        self.assertEqual(rec["folder_id"], a["id"])
        self.assertEqual(rec["folder"], "2026")
        # 移回未分类
        rec = store.update_material_folder_id("mindos_m1", None)
        self.assertIsNone(rec["folder_id"])
        # 无效目录
        with self.assertRaises(FolderNotFoundError):
            store.update_material_folder_id("mindos_m1", 99999)
        # 原材料只能归入 RAW
        knode = store.create_folder_node("KNOWLEDGE", "知识目录")
        with self.assertRaises(FolderError):
            store.update_material_folder_id("mindos_m1", knode["id"])
        # 材料不存在
        self.assertIsNone(store.update_material_folder_id("mindos_missing", a["id"]))

    def test_register_with_folder_id(self):
        root, _, _ = self._tree()
        store = _store()
        store.register("mindos_r1", "p.pdf", "document", "/tmp/p.pdf", folder_id=root["id"])
        rec = store.get("mindos_r1")
        self.assertEqual(rec["folder_id"], root["id"])
        self.assertEqual(rec["folder"], "工作")  # 兼容读字段来自节点名

    def test_list_folder_nodes_counts(self):
        root, a, b = self._tree()
        store = _store()
        store.register("mindos_c1", "p1.pdf", "document", "/tmp/p1.pdf", folder_id=b["id"])
        store.register("mindos_c2", "p2.pdf", "document", "/tmp/p2.pdf", folder_id=root["id"])
        nodes = {n["name"]: n for n in store.list_folder_nodes(SCOPE_RAW)}
        self.assertEqual(nodes["预算"]["materialCount"], 1)
        self.assertEqual(nodes["预算"]["subtreeMaterialCount"], 1)
        self.assertEqual(nodes["2026"]["materialCount"], 0)
        self.assertEqual(nodes["2026"]["subtreeMaterialCount"], 1)
        self.assertEqual(nodes["工作"]["materialCount"], 1)
        self.assertEqual(nodes["工作"]["subtreeMaterialCount"], 2)

    def test_folder_descendants(self):
        root, a, b = self._tree()
        store = _store()
        self.assertEqual(store.folder_descendants(root["id"]), {root["id"], a["id"], b["id"]})
        self.assertEqual(store.folder_descendants(a["id"]), {a["id"], b["id"]})
        self.assertEqual(store.folder_descendants(b["id"]), {b["id"]})
        self.assertEqual(store.folder_descendants(99999), set())

    def test_folder_path(self):
        root, a, b = self._tree()
        store = _store()
        self.assertEqual(store.folder_path(b["id"]), "工作/2026/预算")
        self.assertEqual(store.folder_path(a["id"]), "工作/2026")
        self.assertEqual(store.folder_path(root["id"]), "工作")
        self.assertEqual(store.folder_path(None), "")
        self.assertEqual(store.folder_path(99999), "")

    def test_legacy_folder_migration_to_root_nodes(self):
        db = Path(self._tmp.name) / "legacy.db"
        job_store.reset_for_tests(db)
        store = job_store.JobStore.instance()
        store.register("mindos_legacy_1", "old.pdf", "document", "/tmp/old.pdf", folder="旧目录")
        # 模拟旧单层 folders 表
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO folders(name) VALUES ('旧目录')")
        conn.commit()
        conn.close()
        # 重建触发幂等迁移
        job_store.reset_for_tests(db)
        nodes = job_store.JobStore.instance().list_folder_nodes(SCOPE_RAW)
        root = next(n for n in nodes if n["name"] == "旧目录")
        self.assertIsNone(root["parentId"])
        rec = job_store.JobStore.instance().get("mindos_legacy_1")
        self.assertEqual(rec["folder_id"], root["id"])
        self.assertEqual(rec["folder"], "旧目录")
        # 幂等：再次重建不产生重复根节点
        job_store.reset_for_tests(db)
        nodes2 = job_store.JobStore.instance().list_folder_nodes(SCOPE_RAW)
        self.assertEqual(sum(1 for n in nodes2 if n["name"] == "旧目录"), 1)

    def test_legacy_migration_from_job_records_folder_only(self):
        """目录名只存在于 job_records.folder（旧 folders 表无此名称）时也能迁移为根节点。"""
        db = Path(self._tmp.name) / "folder-only.db"
        job_store.reset_for_tests(db)
        store = job_store.JobStore.instance()
        store.register("mindos_only_1", "a.pdf", "document", "/tmp/a.pdf", folder="仅存在于记录")
        store.register("mindos_only_2", "b.pdf", "document", "/tmp/b.pdf", folder="仅存在于记录")
        # folders 表为空，仅 job_records 残留目录名
        conn = sqlite3.connect(str(db))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0], 0)
        conn.close()
        # 重建触发迁移：应依据 job_records.folder 创建根节点并回填
        job_store.reset_for_tests(db)
        nodes = job_store.JobStore.instance().list_folder_nodes(SCOPE_RAW)
        roots = [n for n in nodes if n["parentId"] is None]
        self.assertEqual([n["name"] for n in roots], ["仅存在于记录"])
        rec1 = job_store.JobStore.instance().get("mindos_only_1")
        rec2 = job_store.JobStore.instance().get("mindos_only_2")
        self.assertEqual(rec1["folder_id"], roots[0]["id"])
        self.assertEqual(rec2["folder_id"], roots[0]["id"])
        # 迁移不会误建「未分类」根节点；重复执行仍幂等
        self.assertEqual(sum(1 for n in nodes if n["name"] == "未分类"), 0)
        job_store.reset_for_tests(db)
        nodes2 = job_store.JobStore.instance().list_folder_nodes(SCOPE_RAW)
        self.assertEqual(sum(1 for n in nodes2 if n["name"] == "仅存在于记录"), 1)


class FolderIngestionTests(unittest.TestCase):
    """ingestion 层：public_record 的 folderId 与子树筛选。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        job_store.reset_for_tests(Path(self._tmp.name) / "jobs.db")
        self.store = _store()
        self.root, self.a, self.b = (
            self.store.create_folder_node(SCOPE_RAW, "工作"),
            None,
            None,
        )
        self.a = self.store.create_folder_node(SCOPE_RAW, "2026", parent_id=self.root["id"])
        self.b = self.store.create_folder_node(SCOPE_RAW, "预算", parent_id=self.a["id"])
        self.store.register("mindos_s1", "p1.pdf", "document", "/tmp/p1.pdf", folder_id=self.root["id"])
        self.store.register("mindos_s2", "p2.mp3", "audio", "/tmp/p2.mp3", folder_id=self.b["id"])
        self.store.register("mindos_s3", "p3.png", "image", "/tmp/p3.png")  # 未分类

    def tearDown(self):
        job_store.reset_for_tests()
        self._tmp.cleanup()

    def test_public_record_exposes_folder_id_and_legacy_folder(self):
        record = self.store.get("mindos_s2")
        public = ingestion.public_record(record, ingestion.ST_AVAILABLE, None)
        self.assertEqual(public["folderId"], self.b["id"])
        self.assertEqual(public["folder"], "预算")
        record_uncat = self.store.get("mindos_s3")
        public_uncat = ingestion.public_record(record_uncat, ingestion.ST_AVAILABLE, None)
        self.assertIsNone(public_uncat["folderId"])

    def test_list_materials_subtree_filter(self):
        def fake_status(material_id):
            return {
                "materialId": material_id,
                "status": "available",
                "fileName": material_id,
                "createdAt": "2026-01-01T00:00:00+00:00",
            }

        with patch("mindos.services.ingestion.status_of", side_effect=fake_status):
            root_ids = {it["materialId"] for it in ingestion.list_materials(folder_id=self.root["id"])}
            self.assertEqual(root_ids, {"mindos_s1", "mindos_s2"})  # 含后代
            a_ids = {it["materialId"] for it in ingestion.list_materials(folder_id=self.a["id"])}
            self.assertEqual(a_ids, {"mindos_s2"})
            b_ids = {it["materialId"] for it in ingestion.list_materials(folder_id=self.b["id"])}
            self.assertEqual(b_ids, {"mindos_s2"})
            # 无效目录不返回任何材料
            self.assertEqual(ingestion.list_materials(folder_id=99999), [])

    def test_start_ingestion_with_folder_id(self):
        with patch("mindos.services.ingestion.submit_index", return_value=True):
            ingestion.start_ingestion(
                "mindos_s4", "p4.pdf", "document", "/tmp/p4.pdf",
                folder_id=self.a["id"],
            )
        rec = self.store.get("mindos_s4")
        self.assertEqual(rec["folder_id"], self.a["id"])


class FolderApiHandlerTests(unittest.TestCase):
    """API 处理器层：FolderError → HTTP 状态码映射与 folderId 写路径。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        job_store.reset_for_tests(Path(self._tmp.name) / "jobs.db")

    def tearDown(self):
        job_store.reset_for_tests()
        self._tmp.cleanup()

    def test_folder_create_handlers_map_errors(self):
        # 父目录不存在 → 404
        req = uploads.FolderCreateRequest(name="孤儿", parentId=99999)
        with self.assertRaises(HTTPException) as ctx:
            uploads.mindos_folder_create(req)
        self.assertEqual(ctx.exception.status_code, 404)
        # 同级重名 → 409
        uploads.mindos_folder_create(uploads.FolderCreateRequest(name="工作"))
        with self.assertRaises(HTTPException) as ctx:
            uploads.mindos_folder_create(uploads.FolderCreateRequest(name="工作"))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_folder_delete_handler_requires_target(self):
        node = uploads.mindos_folder_create(uploads.FolderCreateRequest(name="工作"))
        with self.assertRaises(HTTPException) as ctx:
            uploads.mindos_folder_delete(node["id"])
        self.assertEqual(ctx.exception.status_code, 400)
        result = uploads.mindos_folder_delete(node["id"], move_to_root=True)
        self.assertEqual(result["movedMaterials"], 0)

    def test_folder_move_handler_rejects_descendant(self):
        root = uploads.mindos_folder_create(uploads.FolderCreateRequest(name="工作"))
        child = uploads.mindos_folder_create(
            uploads.FolderCreateRequest(name="2026", parentId=root["id"])
        )
        with self.assertRaises(HTTPException) as ctx:
            uploads.mindos_folder_move(root["id"], uploads.FolderMoveRequest(parentId=child["id"]))
        self.assertEqual(ctx.exception.status_code, 400)
        moved = uploads.mindos_folder_move(
            child["id"], uploads.FolderMoveRequest(parentId=None)
        )
        self.assertIsNone(moved["parentId"])

    def test_material_move_handler_uses_folder_id(self):
        _store().register("mindos_hm1", "p.pdf", "document", "/tmp/p.pdf")
        node = uploads.mindos_folder_create(uploads.FolderCreateRequest(name="目标"))
        res = uploads.mindos_material_move("mindos_hm1", uploads.MaterialMoveRequest(folderId=node["id"]))
        self.assertEqual(res["folderId"], node["id"])
        # 无效 folderId → 404
        with self.assertRaises(HTTPException) as ctx:
            uploads.mindos_material_move("mindos_hm1", uploads.MaterialMoveRequest(folderId=99999))
        self.assertEqual(ctx.exception.status_code, 404)
        # 移回未分类
        res = uploads.mindos_material_move("mindos_hm1", uploads.MaterialMoveRequest(folderId=None))
        self.assertIsNone(res["folderId"])
        self.assertEqual(res["folder"], "未分类")  # 兼容字段同步，避免旧调用方读到历史目录名

    def test_folder_list_handler(self):
        uploads.mindos_folder_create(uploads.FolderCreateRequest(name="工作"))
        result = uploads.mindos_folder_list(scope=SCOPE_RAW)
        self.assertEqual([n["name"] for n in result["items"]], ["工作"])

    def test_folder_create_route_declares_201(self):
        """POST /mindos/folders 契约与实现一致：创建目录返回 201。"""
        from fastapi import FastAPI

        uploads.configure_write_guard(lambda: True)
        app = FastAPI()
        app.include_router(uploads.router)
        spec = app.openapi()
        post = spec["paths"]["/api/mindos/folders"]["post"]
        self.assertIn("201", post["responses"])


if __name__ == "__main__":
    unittest.main(verbosity=2)