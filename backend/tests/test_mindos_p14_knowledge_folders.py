"""MindOS P14-07 知识成品多级目录树测试。

覆盖知识卡片（Wiki frontmatter）与 KNOWLEDGE 目录树的绑定规则：
- 创建/从资料生成卡片默认归入 KNOWLEDGE「Resources」根节点（写 mindos_folder_id）
- 创建/更新/移动时指定 folderId 必须存在且 scope=KNOWLEDGE（RAW 目录 → 400）
- 卡片 ID 在移动/删目录迁移中保持稳定（路径、链接、来源关系不变）
- update 缺省保留当前目录；move 的 null = 移回知识根目录
- 删除 KNOWLEDGE 目录时同步迁移卡片 frontmatter（target / moveToRoot）
- KNOWLEDGE 树与 RAW 树彻底隔离
- _public() 返回 folderId / folderPath / 兼容 folder 字段
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gbrain_store
import wiki_store
from fastapi import FastAPI, HTTPException

from mindos import knowledge, uploads
from mindos.stores import job_store
from mindos.stores.job_store import FolderError, SCOPE_KNOWLEDGE, SCOPE_RAW
from mindos.services import ingestion


def _store() -> job_store.JobStore:
    return job_store.JobStore.instance()


def _resources_root_id() -> int | None:
    """KNOWLEDGE 树的「Resources」根节点 ID（测试辅助，不存在返回 None）。"""
    for node in _store().list_folder_nodes(SCOPE_KNOWLEDGE):
        if node["parentId"] is None and node["name"] == "Resources":
            return node["id"]
    return None


class KnowledgeFolderTestCase(unittest.TestCase):
    """隔离环境：临时 jobs.db + 临时 wiki 目录 + gbrain 增量同步打桩。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        job_store.reset_for_tests(Path(self._tmp.name) / "jobs.db")
        self._old_dir = wiki_store.WIKI_DIR
        self._old_db = wiki_store.WIKI_DB_PATH
        wiki_store.WIKI_DIR = str(Path(self._tmp.name) / "wiki")
        wiki_store.WIKI_DB_PATH = str(Path(self._tmp.name) / "wiki" / "wiki.sqlite3")
        wiki_store._SCHEMA_READY = False
        self._gbrain = patch(
            "gbrain_store.sync_wiki_page",
            return_value={"success": True, "slug": "test"},
        )
        self._gbrain.start()
        # 本套测试关注 Wiki/目录契约，不加载实际嵌入模型或 Chroma。
        self._card_index = patch("mindos.knowledge.knowledge_index.index_card", return_value=True)
        self._card_index_mock = self._card_index.start()
        self._card_index_remove = patch("mindos.knowledge.knowledge_index.remove_card", return_value=None)
        self._card_index_remove.start()
        self._card_index_search = patch("mindos.knowledge.knowledge_index.search_cards", return_value=[])
        self._card_index_search.start()

    def tearDown(self):
        self._card_index_search.stop()
        self._card_index_remove.stop()
        self._card_index.stop()
        self._gbrain.stop()
        wiki_store.WIKI_DIR = self._old_dir
        wiki_store.WIKI_DB_PATH = self._old_db
        wiki_store._SCHEMA_READY = False
        job_store.reset_for_tests()
        self._tmp.cleanup()

    # ---- 辅助方法 ----------------------------------------------------

    def _create_card(self, title: str, folder_id: int | None = None, content: str = "正文内容"):
        res = knowledge.knowledge_create(
            knowledge.KnowledgeCreate(title=title, content=content, folderId=folder_id)
        )
        return res["item"]

    def _detail(self, item: dict) -> dict:
        return knowledge._find(item["knowledgeId"])

    def _cards_folder_id(self, item: dict) -> int | None:
        return knowledge._card_folder_id(self._detail(item))

    def _set_merged(self, item: dict) -> None:
        """把卡片标记为已合并（mindos_merged_into），用于移动/编辑被拒断言。"""
        page = self._detail(item)
        content = str(page["content"])
        meta, body = wiki_store._parse_frontmatter(content)
        meta["mindos_merged_into"] = "knowledge_other"
        wiki_store.write_page(
            str(page["path"]),
            knowledge._render_frontmatter(meta) + "\n" + body,
            source_agent="mindos",
        )


class KnowledgeFolderCreateTests(KnowledgeFolderTestCase):
    """创建卡片与目录目标的绑定规则。"""

    def test_create_defaults_to_resources_root(self):
        """不传 folderId：自动归入 KNOWLEDGE「Resources」根节点。"""
        store = _store()
        item = self._create_card("默认卡片")
        root_id = _resources_root_id()
        self.assertIsNotNone(root_id)
        # KNOWLEDGE 树中存在 Resources 根节点
        tree = {n["id"]: n for n in store.list_folder_nodes(SCOPE_KNOWLEDGE)}
        self.assertEqual(tree[root_id]["name"], "Resources")
        self.assertIsNone(tree[root_id]["parentId"])
        # frontmatter 写入 mindos_folder_id 且指向该根节点
        self.assertEqual(self._cards_folder_id(item), root_id)
        self.assertEqual(item["folderId"], root_id)
        self.assertEqual(item["folderPath"], "Resources")
        self.assertEqual(item["folder"], "Resources")

    def test_create_with_knowledge_folder(self):
        """指定 KNOWLEDGE 目录（含多级路径）成功且路径正确。"""
        store = _store()
        root = store.create_folder_node(SCOPE_KNOWLEDGE, "知识库")
        sub = store.create_folder_node(SCOPE_KNOWLEDGE, "专题", parent_id=root["id"])
        item = self._create_card("专题卡片", folder_id=sub["id"])
        self.assertEqual(self._cards_folder_id(item), sub["id"])
        self.assertEqual(item["folderId"], sub["id"])
        self.assertEqual(item["folderPath"], "知识库/专题")
        self.assertEqual(item["folder"], "专题")

    def test_create_syncs_substantive_body_to_knowledge_vector_index(self):
        """草稿保存不入索引，确认流程才负责提交独立知识索引。"""
        item = self._create_card("产品定义", content="MindOS 是面向个人用户的多模态知识库。")
        self._card_index_mock.assert_not_called()
        self.assertEqual(item["approvalState"], "draft")

    def test_create_rejects_raw_folder(self):
        """原材料（RAW）目录不能作为知识卡片目标。"""
        raw = _store().create_folder_node(SCOPE_RAW, "原材料目录")
        with self.assertRaises(HTTPException) as ctx:
            self._create_card("非法目录卡片", folder_id=raw["id"])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_create_rejects_unknown_folder(self):
        """不存在的目录 ID → 404。"""
        with self.assertRaises(HTTPException) as ctx:
            self._create_card("悬空目录卡片", folder_id=99999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_create_from_material_goes_to_resources_root(self):
        """从资料生成的卡片默认归入「Resources」根节点。"""
        store = _store()
        store.register("mindos_p147_1", "p.pdf", "document", "/tmp/p.pdf")
        item = knowledge.knowledge_create_from_material("mindos_p147_1")["item"]
        root_id = _resources_root_id()
        self.assertEqual(self._cards_folder_id(item), root_id)
        self.assertEqual(item["folderId"], root_id)
        self.assertEqual(item["folderPath"], "Resources")

    def test_create_from_material_can_prefill_generated_summary_as_editable_draft(self):
        """只有显式选择时才把已生成摘要写入正文；默认仍保持空白引用卡片。"""
        store = _store()
        store.register("mindos_p147_summary", "p.pdf", "document", "/tmp/p-summary.pdf")
        material = {
            "fileName": "p.pdf", "tags": ["需求"],
            "summary": {"status": "ok", "text": "MindOS 是个人多模态知识库。"},
        }
        with patch("mindos.knowledge.ingestion.detail_of", return_value=material):
            result = knowledge.knowledge_create_from_material(
                "mindos_p147_summary",
                knowledge.KnowledgeFromMaterialCreate(prefillFromSummary=True),
            )
        self.assertTrue(result["prefilled"])
        page = self._detail(result["item"])
        self.assertNotIn("资料摘要（待编辑草稿）", page["content"])
        self.assertIn("MindOS 是个人多模态知识库", page["content"])
        self.assertEqual(
            knowledge._card_body(page),
            "MindOS 是个人多模态知识库。",
        )
        # 材料草稿只在原材料详情中出现，确认前不进入知识成品列表或 RAG。
        listed_ids = {item["knowledgeId"] for item in knowledge.knowledge_list()["items"]}
        self.assertNotIn(result["item"]["knowledgeId"], listed_ids)
        self.assertFalse(knowledge._is_rag_eligible_page(page))

    def test_local_card_search_extracts_keywords_from_natural_language_question(self):
        """GBrain 不可用时，「MindOS 是什么」仍应按 MindOS 召回有正文的卡片。"""
        card = self._create_card(
            "MindOS 产品说明",
            content="MindOS 是一个本地优先的个人多模态知识库。",
        )
        with patch.object(knowledge, "_is_rag_eligible_page", return_value=True), patch.object(
            wiki_store, "search_wiki", side_effect=RuntimeError("gbrain unavailable")
        ):
            rows = knowledge.search_cards("MindOS是什么")

        self.assertEqual([row["knowledgeId"] for row in rows], [card["knowledgeId"]])
        self.assertIn("本地优先", rows[0]["snippet"])
        self.assertNotIn("mindos_card:", rows[0]["snippet"])

    def test_knowledge_tree_isolated_from_raw_tree(self):
        """KNOWLEDGE 与 RAW 目录树互不可见。"""
        store = _store()
        store.create_folder_node(SCOPE_KNOWLEDGE, "知识库")
        store.create_folder_node(SCOPE_RAW, "工作")
        raw_names = {n["name"] for n in store.list_folder_nodes(SCOPE_RAW)}
        knowledge_names = {n["name"] for n in store.list_folder_nodes(SCOPE_KNOWLEDGE)}
        self.assertNotIn("知识库", raw_names)
        self.assertNotIn("工作", knowledge_names)


class KnowledgeFolderUpdateMoveTests(KnowledgeFolderTestCase):
    """更新/移动卡片的目录归属规则。"""

    def setUp(self):
        super().setUp()
        self.store = _store()
        self.root_a = self.store.create_folder_node(SCOPE_KNOWLEDGE, "目录A")
        self.root_b = self.store.create_folder_node(SCOPE_KNOWLEDGE, "目录B")
        self.card = self._create_card("可移动卡片", folder_id=self.root_a["id"])

    def test_update_keeps_folder_by_default(self):
        """更新时不传 folderId：保留当前目录；卡片 ID（路径）不变。"""
        before_id = self.card["knowledgeId"]
        res = knowledge.knowledge_update(
            self.card["knowledgeId"],
            knowledge.KnowledgeUpdate(content="新正文", title="可移动卡片"),
        )
        updated = res["item"]
        self.assertEqual(updated["knowledgeId"], before_id)
        self.assertEqual(updated["folderId"], self.root_a["id"])
        self.assertEqual(updated["folderPath"], "目录A")

    def test_update_redirects_folder(self):
        """更新时传入新 folderId：卡片迁移到新目录。"""
        res = knowledge.knowledge_update(
            self.card["knowledgeId"],
            knowledge.KnowledgeUpdate(content="新正文", folderId=self.root_b["id"]),
        )
        updated = res["item"]
        self.assertEqual(updated["folderId"], self.root_b["id"])
        self.assertEqual(updated["folderPath"], "目录B")

    def test_update_rejects_raw_folder(self):
        """更新时传入 RAW 目录 → 400。"""
        raw = self.store.create_folder_node(SCOPE_RAW, "原材料目录")
        with self.assertRaises(HTTPException) as ctx:
            knowledge.knowledge_update(
                self.card["knowledgeId"],
                knowledge.KnowledgeUpdate(content="x", folderId=raw["id"]),
            )
        self.assertEqual(ctx.exception.status_code, 400)
        # 归属未被改变
        self.assertEqual(self._cards_folder_id(self.card), self.root_a["id"])

    def test_move_to_knowledge_folder_keeps_card_id(self):
        """移动只改 frontmatter 的 mindos_folder_id，卡片 ID / 来源关系不变。"""
        before_id = self.card["knowledgeId"]
        res = knowledge.knowledge_move(
            self.card["knowledgeId"], knowledge.KnowledgeMoveRequest(folderId=self.root_b["id"])
        )
        moved = res["item"]
        self.assertEqual(moved["knowledgeId"], before_id)
        self.assertEqual(moved["folderId"], self.root_b["id"])
        self.assertEqual(moved["folderPath"], "目录B")
        page = self._detail(moved)
        self.assertTrue(knowledge._is_mindos_card(page))
        self.assertEqual(knowledge._source_ids(page), [])

    def test_move_null_returns_to_resources_root(self):
        """folderId=null → 移回知识根目录「Resources」（按需创建）。"""
        # 卡片创建时指定了目录，因此 Resources 根尚未建立；移动后会按需创建
        self.assertIsNone(_resources_root_id())
        res = knowledge.knowledge_move(
            self.card["knowledgeId"], knowledge.KnowledgeMoveRequest(folderId=None)
        )
        moved = res["item"]
        root_id = _resources_root_id()
        self.assertIsNotNone(root_id)
        self.assertEqual(moved["folderId"], root_id)
        self.assertEqual(moved["folderPath"], "Resources")
        self.assertEqual(moved["folder"], "Resources")

    def test_move_rejects_raw_and_unknown_folder(self):
        """移动目标同样限制为 KNOWLEDGE 目录：RAW → 400，无效 → 404。"""
        raw = self.store.create_folder_node(SCOPE_RAW, "原材料目录")
        with self.assertRaises(HTTPException) as ctx:
            knowledge.knowledge_move(
                self.card["knowledgeId"], knowledge.KnowledgeMoveRequest(folderId=raw["id"])
            )
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            knowledge.knowledge_move(
                self.card["knowledgeId"], knowledge.KnowledgeMoveRequest(folderId=99999)
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_move_rejects_recycled_and_merged(self):
        """已回收/已合并的卡片不能移动。"""
        archived_id = self._create_card("回收卡片", folder_id=self.root_a["id"])["knowledgeId"]
        knowledge._set_recycled(archived_id, True)
        with self.assertRaises(HTTPException) as ctx:
            knowledge.knowledge_move(archived_id, knowledge.KnowledgeMoveRequest(folderId=None))
        self.assertEqual(ctx.exception.status_code, 400)

        merged_item = self._create_card("合并卡片", folder_id=self.root_a["id"])
        self._set_merged(merged_item)
        with self.assertRaises(HTTPException) as ctx:
            knowledge.knowledge_move(
                merged_item["knowledgeId"], knowledge.KnowledgeMoveRequest(folderId=None)
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_list_returns_folder_fields(self):
        """知识卡片列表返回 folderId / folderPath / 兼容 folder。"""
        with patch.object(knowledge.card_ledger_store, "get", return_value={"approval_state": "confirmed"}):
            result = knowledge.knowledge_list()
        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertEqual(item["folderId"], self.root_a["id"])
        self.assertEqual(item["folderPath"], "目录A")
        self.assertEqual(item["folder"], "目录A")

    def test_list_filters_by_folder_subtree(self):
        """folderId 筛选取选定目录及其全部后代子树内的卡片。"""
        store = self.store
        sub_a = store.create_folder_node(SCOPE_KNOWLEDGE, "子A", parent_id=self.root_a["id"])
        card_b = self._create_card("B卡片", folder_id=self.root_b["id"])
        card_sub = self._create_card("子A卡片", folder_id=sub_a["id"])
        with patch.object(knowledge.card_ledger_store, "get", return_value={"approval_state": "confirmed"}):
            # 目录A 子树（含子A）：self.card + card_sub；目录B 卡片不在内
            result_a = knowledge.knowledge_list(folderId=self.root_a["id"])
            titles = {it["title"] for it in result_a["items"]}
            self.assertEqual(titles, {"可移动卡片", "子A卡片"})
            # 子A 子树：仅 card_sub
            result_sub = knowledge.knowledge_list(folderId=sub_a["id"])
            self.assertEqual([it["title"] for it in result_sub["items"]], ["子A卡片"])
            # 目录B：仅 card_b
            result_b = knowledge.knowledge_list(folderId=self.root_b["id"])
            self.assertEqual([it["title"] for it in result_b["items"]], ["B卡片"])
            # 不存在的目录 → 空
            self.assertEqual(knowledge.knowledge_list(folderId=99999)["items"], [])
            # 不传 folderId → 全部活跃卡片
            self.assertEqual(knowledge.knowledge_list()["total"], 3)

    def test_move_route_registered(self):
        """POST /api/mindos/knowledge/{knowledge_id}/move 已注册。"""
        knowledge.configure_write_guard(lambda: True)
        app = FastAPI()
        app.include_router(knowledge.router)
        spec = app.openapi()
        self.assertIn(
            "/api/mindos/knowledge/{knowledge_id}/move",
            spec["paths"],
        )
        self.assertIn("post", spec["paths"]["/api/mindos/knowledge/{knowledge_id}/move"])


class KnowledgeFolderDeleteTests(KnowledgeFolderTestCase):
    """删除 KNOWLEDGE 目录时的卡片迁移规则。"""

    def test_delete_knowledge_folder_moves_cards_to_target(self):
        """删除 KNOWLEDGE 目录：直接归类的卡片迁往目标目录，子目录中卡片保持原归属。"""
        store = _store()
        doomed = store.create_folder_node(SCOPE_KNOWLEDGE, "待删目录")
        sub = store.create_folder_node(SCOPE_KNOWLEDGE, "子目录", parent_id=doomed["id"])
        target = store.create_folder_node(SCOPE_KNOWLEDGE, "迁移目标")
        card_in_doomed = self._create_card("目录内卡片", folder_id=doomed["id"])
        card_in_sub = self._create_card("子目录卡片", folder_id=sub["id"])

        result = uploads.mindos_folder_delete(doomed["id"], target_folder_id=target["id"])
        self.assertEqual(result["folderId"], doomed["id"])
        self.assertEqual(result["movedCards"], 1)  # 仅 card_in_doomed
        self.assertEqual(result["reparentedFolders"], 1)  # 子目录提升到目标下

        # 直接归类的卡片迁往 target
        migrated = knowledge.knowledge_detail(card_in_doomed["knowledgeId"])
        self.assertEqual(migrated["folderId"], target["id"])
        self.assertEqual(migrated["folderPath"], "迁移目标")
        # 子目录经重挂载后仍保留，其卡片目录 ID 未失效，无需迁移
        kept = knowledge.knowledge_detail(card_in_sub["knowledgeId"])
        self.assertEqual(kept["folderId"], sub["id"])
        # 卡片 ID 全程不变
        self.assertEqual(migrated["knowledgeId"], card_in_doomed["knowledgeId"])

    def test_delete_knowledge_folder_move_to_root(self):
        """删除并 moveToRoot：卡片归回知识根目录「Resources」。"""
        store = _store()
        doomed = store.create_folder_node(SCOPE_KNOWLEDGE, "待删根目录")
        card = self._create_card("回根卡片", folder_id=doomed["id"])

        result = uploads.mindos_folder_delete(doomed["id"], move_to_root=True)
        self.assertEqual(result["movedCards"], 1)
        root_id = _resources_root_id()
        self.assertIsNotNone(root_id)
        moved = knowledge.knowledge_detail(card["knowledgeId"])
        self.assertEqual(moved["folderId"], root_id)
        self.assertEqual(moved["folderPath"], "Resources")
        self.assertEqual(moved["folder"], "Resources")

    def test_delete_raw_folder_does_not_touch_knowledge_cards(self):
        """删除 RAW 目录不迁移知识卡片（movedCards 恒为 0；知识卡片只归 KNOWLEDGE 目录）。"""
        store = _store()
        raw = store.create_folder_node(SCOPE_RAW, "原材料目录")
        store.register(
            "mindos_p147_raw", "p.pdf", "document", "/tmp/p.pdf", folder_id=raw["id"]
        )
        card = self._create_card("不受影响卡片")  # 归 Resources
        before = knowledge.knowledge_detail(card["knowledgeId"])["folderId"]

        result = uploads.mindos_folder_delete(raw["id"], move_to_root=True)
        self.assertEqual(result["movedCards"], 0)
        self.assertEqual(result["movedMaterials"], 1)
        after = knowledge.knowledge_detail(card["knowledgeId"])
        self.assertEqual(after["folderId"], before)

    def test_dangling_folder_falls_back_to_resources(self):
        """frontmatter 指向已删除目录（删目录时未走迁移）→ 读路径归回 Resources，不报错。"""
        store = _store()
        doomed = store.create_folder_node(SCOPE_KNOWLEDGE, "将被绕过删除的目录")
        card = self._create_card("悬空卡片", folder_id=doomed["id"])
        self.assertEqual(self._cards_folder_id(card), doomed["id"])
        # 直接走 store 层删除绕过 uploads 迁移，制造悬空 folder_id
        store.delete_folder_node(doomed["id"], move_to_root=True)
        # detail 读路径触发 _effective_folder_node 自动归回 Resources 根
        detail = knowledge.knowledge_detail(card["knowledgeId"])
        root_id = _resources_root_id()
        self.assertIsNotNone(root_id)
        self.assertEqual(detail["folderId"], root_id)
        self.assertEqual(detail["folderPath"], "Resources")

    # ---- P1 跨存储原子删除：两阶段（先迁移卡片、后删目录）+ 补偿回滚 ----

    def _assert_wiki_failure_keeps_state(self, fail_at: int) -> None:
        """Wiki 写入在第 fail_at 张卡片失败：目录仍存在、所有卡片归属不变。"""
        store = _store()
        tag = f"f{fail_at}"
        doomed = store.create_folder_node(SCOPE_KNOWLEDGE, f"待删目录{tag}")
        card1 = self._create_card(f"甲卡{tag}", folder_id=doomed["id"])
        card2 = self._create_card(f"乙卡{tag}", folder_id=doomed["id"])
        real_write = wiki_store.write_page
        call = {"n": 0}

        def flaky_write(path, content, source_agent="manual"):
            call["n"] += 1
            if call["n"] == fail_at:
                raise RuntimeError("wiki unavailable")
            return real_write(path, content, source_agent)

        with patch.object(wiki_store, "write_page", flaky_write):
            with self.assertRaises(HTTPException) as ctx:
                uploads.mindos_folder_delete(doomed["id"], move_to_root=True)
        self.assertEqual(ctx.exception.status_code, 503)
        # 目录树未提交删除；所有卡片归属不变（已迁移的也补偿回原目录）
        self.assertIsNotNone(store.folder_node(doomed["id"]))
        self.assertEqual(self._cards_folder_id(card1), doomed["id"])
        self.assertEqual(self._cards_folder_id(card2), doomed["id"])

    def test_delete_knowledge_folder_wiki_write_failure_keeps_folder_and_cards(self):
        """迁移第 1 张或第 N 张卡片时 Wiki 写入失败：目录仍存在、所有卡片归属不变（P1）。"""
        self._assert_wiki_failure_keeps_state(fail_at=1)
        self._assert_wiki_failure_keeps_state(fail_at=2)

    def test_delete_knowledge_folder_delete_failure_compensates_and_retry(self):
        """目录树删除失败：卡片归属恢复原状；再次发起删除可正常完成（P1 补偿）。"""
        store = _store()
        doomed = store.create_folder_node(SCOPE_KNOWLEDGE, "待删目录")
        sub = store.create_folder_node(SCOPE_KNOWLEDGE, "子目录", parent_id=doomed["id"])
        target = store.create_folder_node(SCOPE_KNOWLEDGE, "迁移目标")
        card_doomed = self._create_card("主卡片", folder_id=doomed["id"])
        card_sub = self._create_card("子卡片", folder_id=sub["id"])

        real_delete = job_store.JobStore.delete_folder_node
        attempt = {"n": 0}

        def flaky_delete(self, folder_id, target_folder_id=None, move_to_root=False):
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise FolderError("模拟目录删除失败")
            return real_delete(self, folder_id, target_folder_id, move_to_root)

        with patch.object(job_store.JobStore, "delete_folder_node", flaky_delete):
            with self.assertRaises(HTTPException) as ctx:
                uploads.mindos_folder_delete(doomed["id"], target_folder_id=target["id"])
        self.assertEqual(ctx.exception.status_code, 400)

        # 目录仍存在；已迁移卡片补偿回原目录
        self.assertIsNotNone(store.folder_node(doomed["id"]))
        self.assertEqual(self._cards_folder_id(card_doomed), doomed["id"])
        self.assertEqual(self._cards_folder_id(card_sub), sub["id"])

        # 再次发起删除可正常完成
        result = uploads.mindos_folder_delete(doomed["id"], target_folder_id=target["id"])
        self.assertEqual(result["movedCards"], 1)
        self.assertEqual(result["reparentedFolders"], 1)
        self.assertIsNone(store.folder_node(doomed["id"]))
        migrated = knowledge.knowledge_detail(card_doomed["knowledgeId"])
        self.assertEqual(migrated["folderId"], target["id"])
        kept = knowledge.knowledge_detail(card_sub["knowledgeId"])
        self.assertEqual(kept["folderId"], sub["id"])

    def test_delete_resources_root_move_to_root_no_dangling(self):
        """删除 Resources 根并 moveToRoot：替代根先可用再迁移，卡片不留悬空 ID（P1）。"""
        store = _store()
        self._create_card("根卡片")  # 触发 Resources 根创建
        old_root_id = _resources_root_id()
        self.assertIsNotNone(old_root_id)
        card = self._create_card("根内卡片")  # 直接归入 Resources 根
        self.assertEqual(self._cards_folder_id(card), old_root_id)
        sub = store.create_folder_node(SCOPE_KNOWLEDGE, "根下子目录", parent_id=old_root_id)
        sub_card = self._create_card("子目录卡片", folder_id=sub["id"])

        result = uploads.mindos_folder_delete(old_root_id, move_to_root=True)
        self.assertEqual(result["movedCards"], 2)  # 「根卡片」「根内卡片」均在根内
        # 旧根已删；替代 Resources 根已就绪（ID 不同，无悬空）
        new_root_id = _resources_root_id()
        self.assertIsNotNone(new_root_id)
        self.assertNotEqual(new_root_id, old_root_id)
        self.assertIsNone(store.folder_node(old_root_id))
        moved = knowledge.knowledge_detail(card["knowledgeId"])
        self.assertEqual(moved["folderId"], new_root_id)
        self.assertEqual(moved["folderPath"], "Resources")
        # 子目录提升到根下，其内卡片归属不变（目录 ID 未失效，无需迁移）
        kept_node = store.folder_node(sub["id"])
        self.assertIsNotNone(kept_node)
        self.assertIsNone(kept_node["parentId"])
        kept = knowledge.knowledge_detail(sub_card["knowledgeId"])
        self.assertEqual(kept["folderId"], sub["id"])


class KnowledgeMaterialScopeTests(KnowledgeFolderTestCase):
    """原材料与知识卡片目录 scope 互斥（跨资料接口）。"""

    def test_material_cannot_enter_knowledge_folder(self):
        """原材料移动到 KNOWLEDGE 目录被拒。"""
        from mindos.stores.job_store import FolderError

        store = _store()
        store.register("mindos_p147_mat", "p.pdf", "document", "/tmp/p.pdf")
        knode = store.create_folder_node(SCOPE_KNOWLEDGE, "知识库")
        with self.assertRaises(FolderError):
            store.update_material_folder_id("mindos_p147_mat", knode["id"])

    def test_list_folder_nodes_ingestion_scope(self):
        """ingestion.list_folder_nodes 按 scope 隔离。"""
        store = _store()
        store.create_folder_node(SCOPE_KNOWLEDGE, "知识库")
        store.create_folder_node(SCOPE_RAW, "工作")
        raw = ingestion.list_folder_nodes(SCOPE_RAW)
        kn_svc = ingestion.list_folder_nodes(SCOPE_KNOWLEDGE)
        self.assertEqual([n["name"] for n in raw], ["工作"])
        self.assertIn("知识库", [n["name"] for n in kn_svc])


if __name__ == "__main__":
    unittest.main(verbosity=2)
