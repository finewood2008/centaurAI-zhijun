"""MindOS P15-03/04/05/06 生命周期闭环回归测试。

覆盖：
- P15-03：版本上传「禁止历史版本分叉」（基于已替代版本 → 409，基于最新版本 → 成功）；
  归档基线 → 400；version-impact 展示纠错本 / 草稿影响。
- P15-04：material / knowledge 删除影响预览（deletion-impact）：返回一次性 confirmToken、
  阻塞依赖与 allowedActions、派生清理统计；预览不返回物理路径。
- P15-05：受控回收 / 恢复 / 永久清除：
  - 缺省 / 过期 / 不匹配 confirmToken → 409；
  - 存在未处理活跃依赖 → 409；
  - 依赖决策生效（archive / replaceSource）；唯一来源活跃卡片禁止仅移除来源（409）；
  - 回收后不出现在活跃列表、出现在回收站列表；恢复后可再次使用；
  - 永久清除后记录、向量映射、列表均不再命中。
- P15-06：兼容迁移幂等（材料家族字段默认补齐）、前端路由 OpenAPI 契约。

依赖项目 .venv，可独立于 server 运行：
    ..\\.venv\\Scripts\\python.exe -m unittest test_mindos_p15_03_04_05 -v
"""
import asyncio
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

from mindos import derived, graph, knowledge, knowledge_index, lifecycle, related, uploads
from mindos.services import ingestion
from mindos.stores import derived_store, governance_store, job_store
from mindos.stores.job_store import JobStore


class LifecycleTestCase(unittest.TestCase):
    """隔离环境：临时 jobs/gov/derived db + 独立 wiki + 临时回收目录 + Chroma/watcher 打桩。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        job_store.reset_for_tests(Path(self._tmp.name) / "jobs.db")
        governance_store.reset_for_tests(Path(self._tmp.name) / "gov.db")
        derived_store.reset_for_tests(Path(self._tmp.name) / "derived.db")
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
        self._status = patch.object(ingestion, "status_of", side_effect=self._status_of)
        self._status.start()
        self._submit_ing = patch.object(ingestion, "submit_index", return_value=True)
        self._submit_ing.start()

        # 受控回收目录：把 lifecycle 的监控目录 / 回收目录指向临时目录。
        self._watch = Path(self._tmp.name) / "watch"
        self._watch.mkdir(parents=True, exist_ok=True)
        self._trash = Path(self._tmp.name) / "trash"
        self._watch_p = patch.object(lifecycle, "WATCH_FOLDER", str(self._watch))
        self._watch_p.start()
        self._trash_p = patch.object(lifecycle, "TRASH_DIR", str(self._trash))
        self._trash_p.start()
        self._audit_p = patch.object(lifecycle, "_audit", lambda *a, **k: None)
        self._audit_p.start()
        # 打桩 Chroma / watcher / annotations，避免真实向量库与文件中心副作用。
        self._del_doc = patch("vector_store.delete_document", return_value=True)
        self._del_doc.start()
        self._list_docs = patch("vector_store.list_all_documents", return_value=[])
        self._list_docs.start()
        self._chunks = patch.object(knowledge_index, "count_card_chunks", return_value=0)
        self._chunks.start()
        self._submit_watcher = patch("watcher.submit_index", return_value=True)
        self._submit_watcher.start()
        self._ann_del = patch("annotations.delete", return_value=False)
        self._ann_del.start()

        self.store = JobStore.instance()
        self.governance = governance_store.instance()
        self.derived = derived_store.DerivedStore.instance()

    def tearDown(self):
        for p in (
            self._ann_del, self._submit_watcher, self._chunks, self._list_docs,
            self._del_doc, self._audit_p, self._trash_p, self._watch_p,
            self._submit_ing, self._status, self._gbrain,
        ):
            p.stop()
        wiki_store.WIKI_DIR = self._old_dir
        wiki_store.WIKI_DB_PATH = self._old_db
        wiki_store._SCHEMA_READY = False
        # 恢复默认存储路径，避免把已删除的临时 DB 路径泄漏给同进程后续测试模块。
        job_store.reset_for_tests()
        governance_store.reset_for_tests(runtime_paths.GOVERNANCE_DB_PATH)
        derived_store.reset_for_tests()
        self._tmp.cleanup()

    # ---- 辅助方法 ----------------------------------------------------

    def _status_of(self, material_id: str) -> dict | None:
        record = self.store.get(material_id)
        if record is None:
            return None
        return {
            "materialId": material_id,
            "fileName": record["file_name"],
            "fileType": record["file_type"],
            "status": ingestion.ST_AVAILABLE,
            "jobId": "job_x",
            "errorMessage": None,
            "folder": record.get("folder", "未分类"),
            "folderId": record.get("folder_id"),
            "createdAt": "2026-08-15T00:00:00Z",
            "materialFamilyId": record["material_family_id"],
            "versionNumber": record["version_number"],
            "supersedesMaterialId": record.get("supersedes_material_id"),
            "supersededByMaterialId": record.get("superseded_by_material_id"),
            "versionNote": record.get("version_note"),
            "recycled": bool(record.get("recycled")),
        }

    def _material(self, mid: str = "mindos_m1", name: str = "需求说明.md") -> str:
        """登记材料并在临时监控目录落盘原文件（供回收移动 / 恢复还原）。"""
        path = self._watch / f"{mid}.pdf"
        path.write_bytes(b"test material content")
        self.store.register(mid, name, "document", str(path))
        return mid

    def _card(self, title: str = "知识卡片") -> str:
        return knowledge.knowledge_create(
            knowledge.KnowledgeCreate(title=title, content="正文内容")
        )["item"]["knowledgeId"]

    def _set_sources(self, kid: str, refs: list[dict]) -> dict:
        return knowledge.knowledge_update_sources(
            kid,
            knowledge.KnowledgeSourcesUpdate(
                sourceRefs=[knowledge.KnowledgeSourceRef(**r) for r in refs]
            ),
        )

    def _version_upload(self, material_id: str, note: str = "版本说明") -> dict:
        async def _run():
            with patch.object(
                uploads, "_receive_upload",
                return_value=("new.pdf", "document", "/tmp/new.pdf"),
            ):
                return await uploads.mindos_material_version_upload(
                    material_id, file=None, versionNote=note, targetFolderId=None,
                )
        return asyncio.run(_run())

    def _materials(self, recycled: bool = False) -> list[dict]:
        """直接调用列表接口（显式传参，绕开 FastAPI Query 默认值）。"""
        resp = uploads.mindos_materials(
            file_type=None, status=None, keyword=None,
            folder=None, folderId=None, tag=None,
            recycled=recycled,
        )
        return resp["items"]

    def _correction(self, source_id: str) -> str:
        return self.derived.create_correction(
            "纠错标题", "错误观点内容", "正确观点内容", ["错误观点内容"], [source_id]
        )["id"]

    def _draft(self, source_id: str, source_type: str = "material") -> str:
        draft_id = "draft_" + source_id[-6:]
        self.derived.set_derived_record(
            "generation", draft_id, derived.KIND_GENERATED_DRAFT, "ok",
            {
                "type": "study_note", "content": "草稿正文",
                "sourceRefs": [{"sourceType": source_type, "id": source_id}],
                "sourceIds": [source_id],
            },
            "input-hash", "test:model",
        )
        return draft_id


# ============ P15-03：版本链（禁止历史版本分叉）+ 纠错本/草稿影响展示 ============

class MaterialVersionChainTests(LifecycleTestCase):
    def test_version_impact_includes_corrections_and_drafts(self):
        """验收：version-impact 展示关联卡片的纠错本与待审草稿。"""
        m1 = self._material("mindos_vi", "来源.md")
        corr_id = self._correction(m1)
        draft_id = self._draft(m1)
        v2 = self._version_upload(m1)
        impact = uploads.mindos_material_version_impact(v2["newMaterialId"])
        self.assertTrue(impact["ready"])
        self.assertEqual(impact["oldMaterialId"], m1)
        self.assertEqual([c["correctionId"] for c in impact["corrections"]], [corr_id])
        self.assertEqual([d["draftId"] for d in impact["drafts"]], [draft_id])

    def test_version_upload_rejects_superseded_baseline(self):
        """P15-03：基于已被替代的历史版本上传 → 409（禁止历史版本分叉）。"""
        m1 = self._material("mindos_v1", "v1.md")
        v2 = self._version_upload(m1)
        # 新版本处理完成后，前代补齐反向指针 superseded_by → 不再是最新版本。
        self.store.finalize_version_link(v2["newMaterialId"])
        with self.assertRaises(HTTPException) as ctx:
            self._version_upload(m1)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_version_upload_from_latest_ok(self):
        """基于最新版本上传 V2 成功，版本号递增且不改写任何卡片来源。"""
        m1 = self._material("mindos_v1b", "v1b.md")
        kid = self._card("来源卡片")
        self._set_sources(kid, [{"sourceType": "material", "id": m1}])
        v2 = self._version_upload(m1)
        self.assertNotEqual(v2["newMaterialId"], m1)
        self.assertEqual(v2["oldMaterialId"], m1)
        self.assertEqual(v2["versionNumber"], 2)
        # 系统不得自动改写正式卡片来源（仍关联 V1）。
        sources = knowledge.knowledge_sources(kid)["sourceRefs"]
        self.assertEqual([(s["sourceType"], s["id"]) for s in sources], [("material", m1)])

    def test_material_family_fields_backfilled_for_legacy(self):
        """P15-06：旧材料默认初始化为独立家族 V1（material_family_id / version_number 幂等补齐）。"""
        m1 = self._material("mindos_legacy", "旧资料.md")
        record = self.store.get(m1)
        self.assertEqual(record["material_family_id"], m1)
        self.assertEqual(record["version_number"], 1)
        public = ingestion.status_of(m1)
        self.assertEqual(public["materialFamilyId"], m1)
        self.assertEqual(public["versionNumber"], 1)


# ============ P15-04：删除影响预览 ============

class DeletionImpactTests(LifecycleTestCase):
    def test_material_impact_no_refs(self):
        m1 = self._material("mindos_imp", "imp.md")
        impact = lifecycle.deletion_impact_material(m1)
        self.assertTrue(impact["canRecycle"])
        self.assertTrue(impact["canPurge"])
        self.assertTrue(impact["confirmToken"])
        self.assertEqual(impact["blockingDependencies"], [])
        self.assertEqual(impact["target"]["id"], m1)
        # 影响预览绝不返回物理路径 / 绝对路径 / 内部 artifact key。
        blob = json.dumps(impact, ensure_ascii=False)
        self.assertNotIn("source_path", blob)
        self.assertNotIn("/tmp/", blob)
        self.assertNotIn("artifact", blob)

    def test_material_impact_with_active_card_blocks(self):
        m1 = self._material("mindos_imp2", "imp2.md")
        kid = self._card("引用卡片")
        self._set_sources(kid, [{"sourceType": "material", "id": m1}])
        impact = lifecycle.deletion_impact_material(m1)
        self.assertFalse(impact["canPurge"])
        self.assertEqual(impact["knowledgeCards"]["activeCount"], 1)
        deps = impact["blockingDependencies"]
        self.assertTrue(any(d["type"] == "knowledge" and d["id"] == kid for d in deps))
        self.assertEqual(deps[0]["allowedActions"], ["archive", "replaceSource", "detachSource"])

    def test_material_impact_includes_correction_draft_governance(self):
        m1 = self._material("mindos_imp3", "imp3.md")
        corr_id = self._correction(m1)
        draft_id = self._draft(m1)
        self.governance.create([{
            "kind": governance_store.KIND_RELATION, "title": "待确认关联", "reason": "测试",
            "snippet": "", "source_knowledge_id": "", "target_knowledge_id": "",
            "material_id": m1, "score": 0.9, "fingerprint": f"relation:{m1}",
        }])
        impact = lifecycle.deletion_impact_material(m1)
        self.assertEqual([c["id"] for c in impact["corrections"]], [corr_id])
        self.assertEqual([d["draftId"] for d in impact["drafts"]], [draft_id])
        self.assertEqual(len(impact["governanceItems"]), 1)
        # 活跃纠错记录是阻塞依赖 → 不能直接清除。
        self.assertFalse(impact["canPurge"])
        self.assertTrue(any(d["type"] == "correction" and d["id"] == corr_id for d in impact["blockingDependencies"]))
        self.assertTrue(any(d["type"] == "draft" and d["id"] == draft_id for d in impact["blockingDependencies"]))

    def test_knowledge_impact_with_referencing_card(self):
        kid_a = self._card("卡片A")
        kid_b = self._card("卡片B")
        self._set_sources(kid_a, [{"sourceType": "knowledge", "id": kid_b}])
        impact = lifecycle.deletion_impact_knowledge(kid_b)
        self.assertFalse(impact["canPurge"])
        self.assertEqual(impact["referencingKnowledgeCards"]["activeCount"], 1)
        self.assertTrue(any(d["type"] == "knowledge" and d["id"] == kid_a for d in impact["blockingDependencies"]))

    def test_lifecycle_routes_in_openapi(self):
        lifecycle.configure_write_guard(lambda: True)
        app = FastAPI()
        app.include_router(lifecycle.router)
        spec = app.openapi()
        for path in [
            "/api/mindos/materials/{material_id}/deletion-impact",
            "/api/mindos/knowledge/{knowledge_id}/deletion-impact",
            "/api/mindos/materials/{material_id}/recycle",
            "/api/mindos/materials/{material_id}/unrecycle",
            "/api/mindos/materials/{material_id}/purge",
            "/api/mindos/knowledge/{knowledge_id}/recycle",
            "/api/mindos/knowledge/{knowledge_id}/unrecycle",
            "/api/mindos/knowledge/{knowledge_id}/purge",
        ]:
            self.assertIn(path, spec["paths"])


# ============ P15-05：回收 / 恢复 / 永久清除 ============

class RecyclePurgeTests(LifecycleTestCase):
    def test_unrelated_dependency_action_is_rejected_without_writing(self):
        """删除请求不得借 dependencyActions 修改与当前目标无关的对象。"""
        m1 = self._material("mindos_safe", "safe.md")
        unrelated = self._card("不相关卡片")
        impact = lifecycle.deletion_impact_material(m1)
        with self.assertRaises(HTTPException) as ctx:
            lifecycle.recycle_material(
                m1, lifecycle.DeletionExecuteRequest(
                    confirmToken=impact["confirmToken"],
                    dependencyActions=[{"type": "knowledge", "id": unrelated, "action": "recycle"}],
                ),
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse(knowledge._is_recycled(knowledge._find(unrelated)))

    def test_invalid_later_action_does_not_partially_apply_earlier_action(self):
        """全部依赖决策先验证；后项无效时前项不得已被回收。"""
        m1 = self._material("mindos_atomic", "atomic.md")
        kid = self._card("引用卡片")
        self._set_sources(kid, [{"sourceType": "material", "id": m1}])
        corr_id = self._correction(m1)
        impact = lifecycle.deletion_impact_material(m1)
        with self.assertRaises(HTTPException) as ctx:
            lifecycle.purge_material(
                m1, lifecycle.DeletionExecuteRequest(
                    confirmToken=impact["confirmToken"],
                    dependencyActions=[
                        {"type": "knowledge", "id": kid, "action": "recycle"},
                        {"type": "correction", "id": corr_id, "action": "discard"},
                    ],
                ),
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse(knowledge._is_recycled(knowledge._find(kid)))
        self.assertEqual(self.derived.get_correction(corr_id)["status"], "active")

    def test_replace_source_accepts_knowledge_card(self):
        """P15-04：来源替换不限于材料，必须支持知识卡片作为新来源。"""
        m1 = self._material("mindos_replace_source", "replace-source.md")
        target_card = self._card("待处理引用卡片")
        replacement = self._card("替代知识来源")
        self._set_sources(target_card, [{"sourceType": "material", "id": m1}])
        impact = lifecycle.deletion_impact_material(m1)
        lifecycle.purge_material(
            m1, lifecycle.DeletionExecuteRequest(
                confirmToken=impact["confirmToken"],
                dependencyActions=[{
                    "type": "knowledge", "id": target_card, "action": "replaceSource",
                    "replacementSource": {"sourceType": "knowledge", "id": replacement},
                }],
            ),
        )
        refs = knowledge.knowledge_sources(target_card)["sourceRefs"]
        self.assertEqual([(x["sourceType"], x["id"]) for x in refs], [("knowledge", replacement)])

    def test_recycle_unrecycle_material(self):
        m1 = self._material("mindos_rc", "rc.md")
        source_path = self.store.get(m1)["source_path"]
        impact = lifecycle.deletion_impact_material(m1)
        res = lifecycle.recycle_material(
            m1, lifecycle.DeletionExecuteRequest(confirmToken=impact["confirmToken"])
        )
        self.assertTrue(res["recycled"])
        self.assertTrue(self.store.is_recycled(m1))
        # 活跃列表隐藏、回收站列表可见。
        active = self._materials()
        self.assertNotIn(m1, [i["materialId"] for i in active])
        recycled = self._materials(recycled=True)
        self.assertIn(m1, [i["materialId"] for i in recycled])
        # 原文件已移入受控回收目录。
        self.assertFalse(Path(source_path).exists())
        # 恢复：原文件还原、重新进入活跃索引、回收站消失。
        lifecycle.unrecycle_material(m1)
        self.assertFalse(self.store.is_recycled(m1))
        self.assertTrue(Path(source_path).exists())
        active = self._materials()
        self.assertIn(m1, [i["materialId"] for i in active])

    def test_recycle_requires_confirm_token(self):
        m1 = self._material("mindos_rc2", "rc2.md")
        with self.assertRaises(HTTPException) as ctx:
            lifecycle.recycle_material(
                m1, lifecycle.DeletionExecuteRequest(confirmToken="bad-token")
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_recycle_blocked_by_active_card(self):
        m1 = self._material("mindos_rc3", "rc3.md")
        kid = self._card("引用卡片")
        self._set_sources(kid, [{"sourceType": "material", "id": m1}])
        impact = lifecycle.deletion_impact_material(m1)
        with self.assertRaises(HTTPException) as ctx:
            lifecycle.recycle_material(
                m1, lifecycle.DeletionExecuteRequest(confirmToken=impact["confirmToken"])
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_purge_blocked_without_dep_handling(self):
        m1 = self._material("mindos_p1", "p1.md")
        kid = self._card("引用卡片")
        self._set_sources(kid, [{"sourceType": "material", "id": m1}])
        impact = lifecycle.deletion_impact_material(m1)
        with self.assertRaises(HTTPException) as ctx:
            lifecycle.purge_material(
                m1, lifecycle.DeletionExecuteRequest(confirmToken=impact["confirmToken"])
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_purge_after_recycling_dep(self):
        m1 = self._material("mindos_p2", "p2.md")
        kid = self._card("引用卡片")
        self._set_sources(kid, [{"sourceType": "material", "id": m1}])
        impact = lifecycle.deletion_impact_material(m1)
        res = lifecycle.purge_material(
            m1, lifecycle.DeletionExecuteRequest(
                confirmToken=impact["confirmToken"],
                dependencyActions=[{"type": "knowledge", "id": kid, "action": "recycle"}],
            )
        )
        self.assertTrue(res["purged"])
        self.assertIsNone(self.store.get(m1))
        self.assertTrue(knowledge._is_recycled(knowledge._find(kid)))
        # 已回收卡片保留为历史对象，但不再保留已永久清除材料的悬空来源 ID。
        self.assertEqual(knowledge.knowledge_sources(kid)["sourceRefs"], [])

    def test_purge_after_replace_source(self):
        m1 = self._material("mindos_p3", "p3.md")
        m2 = self._material("mindos_p3b", "p3b.md")
        kid = self._card("引用卡片")
        self._set_sources(kid, [{"sourceType": "material", "id": m1}])
        impact = lifecycle.deletion_impact_material(m1)
        lifecycle.purge_material(
            m1, lifecycle.DeletionExecuteRequest(
                confirmToken=impact["confirmToken"],
                dependencyActions=[{
                    "type": "knowledge", "id": kid, "action": "replaceSource",
                    "replacementMaterialId": m2,
                }],
            )
        )
        self.assertIsNone(self.store.get(m1))
        sources = knowledge.knowledge_sources(kid)["sourceRefs"]
        self.assertEqual([(s["sourceType"], s["id"]) for s in sources], [("material", m2)])

    def test_detach_single_source_active_card_rejected(self):
        m1 = self._material("mindos_p4", "p4.md")
        kid = self._card("引用卡片")
        self._set_sources(kid, [{"sourceType": "material", "id": m1}])
        impact = lifecycle.deletion_impact_material(m1)
        with self.assertRaises(HTTPException) as ctx:
            lifecycle.purge_material(
                m1, lifecycle.DeletionExecuteRequest(
                    confirmToken=impact["confirmToken"],
                    dependencyActions=[{"type": "knowledge", "id": kid, "action": "detachSource"}],
                )
            )
        self.assertEqual(ctx.exception.status_code, 409)
        # 卡片来源未被改动。
        sources = knowledge.knowledge_sources(kid)["sourceRefs"]
        self.assertEqual([(s["sourceType"], s["id"]) for s in sources], [("material", m1)])

    def test_detach_multi_source_active_card_allowed(self):
        m1 = self._material("mindos_p5", "p5.md")
        m2 = self._material("mindos_p5b", "p5b.md")
        kid = self._card("多来源卡片")
        self._set_sources(kid, [
            {"sourceType": "material", "id": m1},
            {"sourceType": "material", "id": m2},
        ])
        impact = lifecycle.deletion_impact_material(m1)
        lifecycle.purge_material(
            m1, lifecycle.DeletionExecuteRequest(
                confirmToken=impact["confirmToken"],
                dependencyActions=[{"type": "knowledge", "id": kid, "action": "detachSource"}],
            )
        )
        self.assertIsNone(self.store.get(m1))
        sources = knowledge.knowledge_sources(kid)["sourceRefs"]
        self.assertEqual([(s["sourceType"], s["id"]) for s in sources], [("material", m2)])

    def test_knowledge_recycle_unrecycle_purge(self):
        kid = self._card("卡片")
        impact = lifecycle.deletion_impact_knowledge(kid)
        lifecycle.recycle_knowledge(
            kid, lifecycle.DeletionExecuteRequest(confirmToken=impact["confirmToken"])
        )
        self.assertTrue(knowledge._is_recycled(knowledge._find(kid)))
        active = knowledge.knowledge_list()["items"]
        self.assertNotIn(kid, [c["knowledgeId"] for c in active])
        recycled = knowledge.knowledge_list(recycled=True)["items"]
        self.assertIn(kid, [c["knowledgeId"] for c in recycled])
        # 恢复。
        lifecycle.unrecycle_knowledge(kid)
        self.assertFalse(knowledge._is_recycled(knowledge._find(kid)))
        active = knowledge.knowledge_list()["items"]
        self.assertIn(kid, [c["knowledgeId"] for c in active])
        # 永久清除：文件与记录消失。
        impact2 = lifecycle.deletion_impact_knowledge(kid)
        lifecycle.purge_knowledge(
            kid, lifecycle.DeletionExecuteRequest(confirmToken=impact2["confirmToken"])
        )
        with self.assertRaises(HTTPException) as ctx:
            knowledge._find(kid)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_purged_material_gone_from_surfaces(self):
        m1 = self._material("mindos_pg", "pg.md")
        source_path = self.store.get(m1)["source_path"]
        impact = lifecycle.deletion_impact_material(m1)
        lifecycle.purge_material(
            m1, lifecycle.DeletionExecuteRequest(confirmToken=impact["confirmToken"])
        )
        # 记录、列表、向量映射全部不再命中。
        self.assertIsNone(self.store.get(m1))
        self.assertNotIn(m1, [i["materialId"] for i in self._materials()])
        self.assertIsNone(ingestion.material_for_source(source_path))
        # 图谱节点不包含已清除材料。
        nodes = graph._collect_material_nodes()
        self.assertNotIn(m1, nodes)
        with self.assertRaises(HTTPException):
            related.material_related(m1)

    def test_correction_dep_archive_handled(self):
        m1 = self._material("mindos_pc", "pc.md")
        corr_id = self._correction(m1)
        impact = lifecycle.deletion_impact_material(m1)
        self.assertFalse(impact["canPurge"])
        lifecycle.purge_material(
            m1, lifecycle.DeletionExecuteRequest(
                confirmToken=impact["confirmToken"],
                dependencyActions=[{"type": "correction", "id": corr_id, "action": "archive"}],
            )
        )
        self.assertIsNone(self.store.get(m1))
        corr = self.derived.get_correction(corr_id)
        self.assertEqual(corr["status"], "archived")

    def test_draft_dep_discard_handled(self):
        m1 = self._material("mindos_pd", "pd.md")
        draft_id = self._draft(m1)
        impact = lifecycle.deletion_impact_material(m1)
        lifecycle.purge_material(
            m1, lifecycle.DeletionExecuteRequest(
                confirmToken=impact["confirmToken"],
                dependencyActions=[{"type": "draft", "id": draft_id, "action": "discard"}],
            )
        )
        self.assertIsNone(self.store.get(m1))
        rec = self.derived.get_derived_record("generation", draft_id, derived.KIND_GENERATED_DRAFT)
        self.assertEqual(rec["status"], "discarded")


if __name__ == "__main__":
    unittest.main()
