"""MindOS P15-01 知识卡片来源管理回归测试。

覆盖 mindos.knowledge 的独立来源接口 GET/PUT /api/mindos/knowledge/{id}/sources：
- 空来源允许（普通手工卡片可无来源）；
- 材料 / 知识卡片混合来源：添加、移除、替换，刷新后关系正确；
- 按 (sourceType, id) 去重并保留首次顺序；
- sourceType 白名单（400）；来源不存在（404）；
- 默认拒绝新增已归档来源（material / knowledge 均 400）；
- 禁止知识卡片引用自身（400）；禁止卡片循环引用（409）；
- PUT 成功后同步维护 mindos_source_refs + mindos_source_material_ids + updated_at；
- 旧卡片仅含 mindos_source_material_ids 可读取，PUT 后双写两个字段；
- 编辑正文、标签、移动目录不改变来源关系；
- 从资料创建卡片后来源区显示该材料（验收 1）。

依赖项目 .venv，可独立于 server 运行：
    .venv\\Scripts\\python.exe -m unittest test_mindos_p15_01_sources -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wiki_store
import runtime_paths
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mindos import knowledge
from mindos.knowledge import (
    KnowledgeSourceRef,
    KnowledgeSourcesUpdate,
)
from mindos.services import ingestion
from mindos.stores import governance_store, job_store


class KnowledgeSourcesTestCase(unittest.TestCase):
    """隔离环境：临时 jobs.db + 独立 wiki 目录 + 独立 governance db + gbrain 打桩。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        job_store.reset_for_tests(Path(self._tmp.name) / "jobs.db")
        governance_store.reset_for_tests(Path(self._tmp.name) / "gov.db")
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
        self.store = job_store.JobStore.instance()
        self.governance = governance_store.instance()
        # P15-01 仅允许可用资料作为新来源。夹具中已登记的资料默认模拟为
        # available，个别测试可显式覆盖为 processing / failed 等状态。
        self._status = patch.object(ingestion, "status_of", side_effect=self._status_of)
        self._status.start()

    def tearDown(self):
        self._status.stop()
        self._gbrain.stop()
        wiki_store.WIKI_DIR = self._old_dir
        wiki_store.WIKI_DB_PATH = self._old_db
        wiki_store._SCHEMA_READY = False
        job_store.reset_for_tests()
        # 恢复默认 governance 路径后再删临时目录——此前误把 _DB_PATH 重置到
        # 临时路径才 cleanup，泄漏悬空路径给后续测试
        governance_store.reset_for_tests(runtime_paths.GOVERNANCE_DB_PATH)
        self._tmp.cleanup()

    # ---- 辅助方法 ----------------------------------------------------

    def _material(self, mid: str = "mindos_m1", name: str = "需求说明.md") -> str:
        self.store.register(mid, name, "document", f"/tmp/{mid}.pdf")
        return mid

    def _status_of(self, material_id: str, device_scope="global") -> dict | None:
        if device_scope != "global":
            return None
        record = self.store.get(material_id)
        if record is None:
            return None
        return {
            "materialId": material_id,
            "fileName": record["file_name"],
            "fileType": record["file_type"],
            "status": ingestion.ST_AVAILABLE,
        }

    def _card(self, title: str = "知识卡片") -> str:
        """创建一张手工知识卡片并返回 knowledgeId。"""
        return knowledge.knowledge_create(
            knowledge.KnowledgeCreate(title=title, content="正文内容")
        )["item"]["knowledgeId"]

    def _put(self, kid: str, refs: list[dict]):
        return knowledge.knowledge_update_sources(
            kid,
            KnowledgeSourcesUpdate(
                sourceRefs=[KnowledgeSourceRef(**r) for r in refs]
            ),
        )

    def _ref_keys(self, refs: list[dict]) -> list[tuple[str, str]]:
        return [(r["sourceType"], r["id"]) for r in refs]

    def _frontmatter(self, kid: str) -> dict:
        page = knowledge._find(kid)
        meta, _ = wiki_store._parse_frontmatter(str(page["content"]))
        return meta


class KnowledgeSourcesBasicTests(KnowledgeSourcesTestCase):
    """添加 / 移除 / 替换 / 去重 / 混合来源与双字段写入。"""

    def test_create_from_material_shows_source(self):
        """验收 1：从材料 A 创建卡片后，来源区显示 A。"""
        self._material("mindos_from", "来源.md")
        item = knowledge.knowledge_create_from_material("mindos_from")["item"]
        self.assertEqual(
            self._ref_keys(item["sources"]), [("material", "mindos_from")]
        )

    def test_empty_sources_allowed(self):
        """普通手工卡片允许空来源，写入后两个 frontmatter 字段保持一致。"""
        kid = self._card("无来源卡片")
        res = self._put(kid, [])
        self.assertEqual(res["sourceRefs"], [])
        self.assertEqual(self._frontmatter(kid)["mindos_source_refs"], [])
        self.assertEqual(self._frontmatter(kid)["mindos_source_material_ids"], [])

    def test_add_refresh_and_replace(self):
        """验收 2/3：添加 B 刷新后 A、B 均在；A 替换为 C 后仅剩 B、C。"""
        m_a = self._material("mindos_ma", "A.md")
        m_b = self._material("mindos_mb", "B.md")
        m_c = self._material("mindos_mc", "C.md")
        kid = self._card("来源卡片")
        self._put(kid, [{"sourceType": "material", "id": m_a}])
        self.assertEqual(
            self._ref_keys(knowledge.knowledge_sources(kid)["sourceRefs"]),
            [("material", m_a)],
        )
        self._put(
            kid,
            [
                {"sourceType": "material", "id": m_a},
                {"sourceType": "material", "id": m_b},
            ],
        )
        self.assertEqual(
            self._ref_keys(knowledge.knowledge_sources(kid)["sourceRefs"]),
            [("material", m_a), ("material", m_b)],
        )
        self._put(
            kid,
            [
                {"sourceType": "material", "id": m_b},
                {"sourceType": "material", "id": m_c},
            ],
        )
        self.assertEqual(
            self._ref_keys(knowledge.knowledge_sources(kid)["sourceRefs"]),
            [("material", m_b), ("material", m_c)],
        )

    def test_duplicate_dedup_preserves_first_order(self):
        m_a = self._material("mindos_ma", "A.md")
        m_b = self._material("mindos_mb", "B.md")
        kid = self._card("去重卡片")
        res = self._put(
            kid,
            [
                {"sourceType": "material", "id": m_a},
                {"sourceType": "material", "id": m_b},
                {"sourceType": "material", "id": m_a},
                {"sourceType": "material", "id": m_b},
            ],
        )
        self.assertEqual(
            self._ref_keys(res["sourceRefs"]),
            [("material", m_a), ("material", m_b)],
        )
        self.assertEqual(
            self._ref_keys(self._frontmatter(kid)["mindos_source_refs"]),
            [("material", m_a), ("material", m_b)],
        )

    def test_mixed_material_and_knowledge_sources(self):
        """material + knowledge 混合来源；兼容字段仅含 material。"""
        m1 = self._material("mindos_mm", "M.md")
        kid_a = self._card("卡片A")
        kid_b = self._card("卡片B")
        res = self._put(
            kid_b,
            [
                {"sourceType": "material", "id": m1},
                {"sourceType": "knowledge", "id": kid_a},
            ],
        )
        self.assertEqual(
            self._ref_keys(res["sourceRefs"]),
            [("material", m1), ("knowledge", kid_a)],
        )
        meta = self._frontmatter(kid_b)
        self.assertEqual(meta["mindos_source_material_ids"], [m1])
        # 响应中的知识来源带卡片标题与知识Id
        knowledge_item = next(
            s for s in res["sourceRefs"] if s["sourceType"] == "knowledge"
        )
        self.assertEqual(knowledge_item["knowledgeId"], kid_a)
        self.assertEqual(knowledge_item["title"], "卡片B".replace("卡片B", "卡片A"))

    def test_put_updates_updated_at(self):
        m1 = self._material("mindos_login", "L.md")
        kid = self._card("时间卡片")
        before = self._frontmatter(kid)["updated_at"]
        res = self._put(kid, [{"sourceType": "material", "id": m1}])
        self.assertEqual(res["sourceRefs"][0]["id"], m1)
        self.assertNotEqual(self._frontmatter(kid)["updated_at"], before)
        self.assertTrue(res["updatedAt"])


class KnowledgeSourcesRejectionTests(KnowledgeSourcesTestCase):
    """非法来源类型 / 不存在 / 已归档 / 自引用 / 循环引用。"""

    def test_invalid_source_type_rejected(self):
        kid = self._card("类型卡片")
        with self.assertRaises(HTTPException) as ctx:
            self._put(kid, [{"sourceType": "draft", "id": "x"}])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_material_rejected(self):
        kid = self._card("幽灵资料卡片")
        with self.assertRaises(HTTPException) as ctx:
            self._put(
                kid, [{"sourceType": "material", "id": "mindos_ghost_material"}]
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_missing_knowledge_rejected(self):
        kid = self._card("幽灵卡片")
        with self.assertRaises(HTTPException) as ctx:
            self._put(
                kid, [{"sourceType": "knowledge", "id": "knowledge_ghost_card"}]
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_recycled_material_rejected(self):
        m1 = self._material("mindos_ma", "A.md")
        self.store.set_recycled(m1, True)
        kid = self._card("回收资料卡片")
        with self.assertRaises(HTTPException) as ctx:
            self._put(kid, [{"sourceType": "material", "id": m1}])
        self.assertEqual(ctx.exception.status_code, 400)
        # 未写入任何来源（拒绝时 frontmatter 不含新字段）
        self.assertEqual(self._ref_keys(self._frontmatter(kid).get("mindos_source_refs", [])), [])

    def test_not_available_material_rejected(self):
        """来源材料必须已经完成解析并处于 available 状态。"""
        m1 = self._material("mindos_processing", "处理中资料.md")
        kid = self._card("不可用来源卡片")
        with patch.object(
            ingestion,
            "status_of",
            return_value={"materialId": m1, "fileName": "处理中资料.md", "status": "processing"},
        ):
            with self.assertRaises(HTTPException) as ctx:
                self._put(kid, [{"sourceType": "material", "id": m1}])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_recycled_knowledge_rejected(self):
        target = self._card("被回收卡片")
        knowledge._set_recycled(target, True)
        kid = self._card("回收卡片来源")
        with self.assertRaises(HTTPException) as ctx:
            self._put(kid, [{"sourceType": "knowledge", "id": target}])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_self_reference_rejected(self):
        """验收 4：卡片尝试引用自身时返回 400。"""
        kid = self._card("自身卡片")
        with self.assertRaises(HTTPException) as ctx:
            self._put(kid, [{"sourceType": "knowledge", "id": kid}])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_cycle_rejected(self):
        """验收 5：已存在 A → B 时尝试 B → A 返回 409。"""
        kid_a = self._card("卡片A")
        kid_b = self._card("卡片B")
        self._put(kid_a, [{"sourceType": "knowledge", "id": kid_b}])
        with self.assertRaises(HTTPException) as ctx:
            self._put(kid_b, [{"sourceType": "knowledge", "id": kid_a}])
        self.assertEqual(ctx.exception.status_code, 409)
        # 循环来源未被写入（拒绝时 frontmatter 不含新字段）
        self.assertEqual(self._ref_keys(self._frontmatter(kid_b).get("mindos_source_refs", [])), [])

    def test_cycle_rejected_when_source_card_is_beyond_first_500_pages(self):
        """关系校验必须分页，不能因列表展示上限漏掉循环。"""
        kid_a = self._card("分页卡片A")
        kid_b = self._card("分页卡片B")
        self._put(kid_a, [{"sourceType": "knowledge", "id": kid_b}])
        page_a = knowledge._find(kid_a)
        page_b = knowledge._find(kid_b)
        first_page = [{"path": str(page_a["path"])}]
        first_page.extend({"path": f"filler-{n}.md"} for n in range(499))
        def listed_pages(*, limit: int, offset: int = 0, **_kwargs):
            self.assertEqual(limit, 500)
            if offset == 0:
                return {"items": first_page, "total": 501}
            self.assertEqual(offset, 500)
            return {"items": [{"path": str(page_b["path"])}], "total": 501}

        with patch.object(wiki_store, "list_pages", side_effect=listed_pages):
            with self.assertRaises(HTTPException) as ctx:
                self._put(kid_b, [{"sourceType": "knowledge", "id": kid_a}])
        self.assertEqual(ctx.exception.status_code, 409)

    def test_update_sources_rejected_for_recycled_card(self):
        kid = self._card("回收卡片")
        m1 = self._material("mindos_ma", "A.md")
        self._put(kid, [{"sourceType": "material", "id": m1}])
        knowledge._set_recycled(kid, True)
        with self.assertRaises(HTTPException) as ctx:
            self._put(kid, [{"sourceType": "material", "id": m1}])
        self.assertEqual(ctx.exception.status_code, 400)


class KnowledgeSourcesCompatibilityTests(KnowledgeSourcesTestCase):
    """旧 frontmatter 兼容；正文 / 标签 / 目录编辑不破坏来源。"""

    def _card_with_old_frontmatter(self, title: str = "旧卡片") -> str:
        page = wiki_store.create_page(title, folder="Resources", page_type="note")
        body = (
            "---\n"
            f"title: {json.dumps(title, ensure_ascii=False)}\n"
            "mindos_card: true\n"
            'mindos_source_material_ids: ["mindos_old1"]\n'
            'created_at: "2026-01-01T00:00:00+00:00"\n'
            'updated_at: "2026-01-01T00:00:00+00:00"\n'
            "---\n# 旧卡片\n"
        )
        wiki_store.write_page(str(page["path"]), body, source_agent="mindos")
        return knowledge._knowledge_id(str(page["path"]))

    def test_old_frontmatter_readable(self):
        """旧字段 mindos_source_material_ids 读取时转换为 material 来源（P15-06 兼容）。"""
        self._material("mindos_old1", "旧资料.pdf")
        kid = self._card_with_old_frontmatter()
        gotten = knowledge.knowledge_sources(kid)
        self.assertEqual(
            self._ref_keys(gotten["sourceRefs"]),
            [("material", "mindos_old1")],
        )

    def test_put_double_writes_after_old_frontmatter(self):
        """PUT 后双写 refs 与 material_ids；旧字段来源被整表替换。"""
        self._material("mindos_old1", "旧资料.pdf")
        m2 = self._material("mindos_new2", "新资料.pdf")
        kid = self._card_with_old_frontmatter()
        self._put(kid, [{"sourceType": "material", "id": m2}])
        meta = self._frontmatter(kid)
        self.assertEqual(
            meta["mindos_source_refs"],
            [{"sourceType": "material", "id": m2}],
        )
        self.assertEqual(meta["mindos_source_material_ids"], [m2])
        self.assertEqual(
            self._ref_keys(knowledge.knowledge_sources(kid)["sourceRefs"]),
            [("material", m2)],
        )

    def test_body_tags_folder_do_not_clear_sources(self):
        """验收 6：编辑正文、标签或移动目录后，来源关系保持不变。"""
        m1 = self._material("mindos_ma", "A.md")
        kid = self._card("正文不受影响")
        self._put(kid, [{"sourceType": "material", "id": m1}])
        knowledge.knowledge_update(
            kid,
            knowledge.KnowledgeUpdate(
                title="正文不受影响", content="新正文", tags=["新标签"]
            ),
        )
        knowledge.knowledge_tags(
            kid, knowledge.KnowledgeTagRequest(tags=["再加"], action="add")
        )
        knowledge.knowledge_move(
            kid, knowledge.KnowledgeMoveRequest(folderId=None)
        )
        self.assertEqual(
            self._ref_keys(self._frontmatter(kid)["mindos_source_refs"]),
            [("material", m1)],
        )
        self.assertEqual(
            self._ref_keys(knowledge.knowledge_sources(kid)["sourceRefs"]),
            [("material", m1)],
        )


class KnowledgeSourcesRouteContractTests(KnowledgeSourcesTestCase):
    """P15-01 真实路由及 FastAPI OpenAPI 契约。"""

    def test_get_and_put_sources_over_http(self):
        material_id = self._material("mindos_http", "HTTP来源.md")
        knowledge_id = self._card("HTTP来源卡片")
        knowledge.configure_write_guard(lambda: True)
        app = FastAPI()
        app.include_router(knowledge.router)
        client = TestClient(app)

        put = client.put(
            f"/api/mindos/knowledge/{knowledge_id}/sources",
            json={"sourceRefs": [{"sourceType": "material", "id": material_id}]},
        )
        self.assertEqual(put.status_code, 200)
        self.assertEqual(self._ref_keys(put.json()["sourceRefs"]), [("material", material_id)])

        gotten = client.get(f"/api/mindos/knowledge/{knowledge_id}/sources")
        self.assertEqual(gotten.status_code, 200)
        self.assertEqual(self._ref_keys(gotten.json()["sourceRefs"]), [("material", material_id)])

    def test_sources_routes_and_request_schema_are_in_openapi(self):
        knowledge.configure_write_guard(lambda: True)
        app = FastAPI()
        app.include_router(knowledge.router)
        spec = app.openapi()
        path = "/api/mindos/knowledge/{knowledge_id}/sources"
        self.assertIn(path, spec["paths"])
        self.assertIn("get", spec["paths"][path])
        self.assertIn("put", spec["paths"][path])
        body = spec["paths"][path]["put"]["requestBody"]
        self.assertTrue(body["required"])
        self.assertIn("application/json", body["content"])
        self.assertIn("KnowledgeSourcesUpdate", spec["components"]["schemas"])
