"""P15-02/03：原材料归档影响预览与版本链生命周期。"""
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gbrain_store
import wiki_store
from fastapi import FastAPI, Request

from mindos import knowledge, uploads
from mindos import derived as derived_svc
from mindos.services import ingestion
from mindos.stores import derived_store, governance_store, job_store, card_ledger_store


class LifecycleTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        job_store.reset_for_tests(root / "jobs.db")
        governance_store.reset_for_tests(root / "governance.db")
        derived_store.reset_for_tests(root / "derived.db")
        card_ledger_store.reset_for_tests(root / "cards.db")
        self.old_wiki_dir, self.old_wiki_db = wiki_store.WIKI_DIR, wiki_store.WIKI_DB_PATH
        wiki_store.WIKI_DIR = str(root / "wiki")
        wiki_store.WIKI_DB_PATH = str(root / "wiki" / "wiki.sqlite3")
        wiki_store._SCHEMA_READY = False
        self.sync = patch("gbrain_store.sync_wiki_page", return_value={"success": True})
        self.sync.start()
        self.store = job_store.JobStore.instance()
        self.governance = governance_store.instance()
        self.states: dict[str, str] = {}
        self.status = patch.object(ingestion, "status_of", side_effect=self._status_of)
        self.status.start()

    def tearDown(self):
        self.status.stop()
        self.sync.stop()
        wiki_store.WIKI_DIR, wiki_store.WIKI_DB_PATH = self.old_wiki_dir, self.old_wiki_db
        wiki_store._SCHEMA_READY = False
        job_store.reset_for_tests()
        card_ledger_store.reset_for_tests()
        derived_store.reset_for_tests()
        self.tmp.cleanup()

    def _status_of(self, material_id: str, device_scope="global"):
        if device_scope != "global":
            return None
        record = self.store.get(material_id)
        if record is None:
            return None
        return ingestion.public_record(record, self.states.get(material_id, "available"), None)

    def _material(self, material_id: str, family: str | None = None, supersedes: str | None = None):
        return self.store.register(
            material_id, f"{material_id}.md", "document", f"/tmp/{material_id}.md",
            material_family_id=family, supersedes_material_id=supersedes,
        )

    def _card_with_source(self, material_id: str) -> str:
        card_id = knowledge.knowledge_create(
            knowledge.KnowledgeCreate(title="关联卡片", content="独立正文")
        )["item"]["knowledgeId"]
        knowledge.knowledge_update_sources(
            card_id,
            knowledge.KnowledgeSourcesUpdate(
                sourceRefs=[knowledge.KnowledgeSourceRef(sourceType="material", id=material_id)]
            ),
        )
        return card_id


class RecycledSourceImpactTests(LifecycleTestCase):
    def test_recycled_source_record_keeps_card_and_restores_source_availability(self):
        self._material("mindos_v1")
        card_id = self._card_with_source("mindos_v1")
        # A real manual create must register ownership without confirming the
        # draft or allowing it into retrieval merely to make dependencies visible.
        state = card_ledger_store.get(card_id, device_scope="global")
        self.assertIsNotNone(state)
        self.assertEqual(state["approval_state"], "draft")
        self.assertFalse(card_ledger_store.is_rag_eligible(state))
        before = uploads.mindos_material_impact("mindos_v1", Request({"type": "http"}))
        self.assertEqual(before["activeKnowledgeCardCount"], 1)
        self.assertEqual(before["activeKnowledgeCards"][0]["knowledgeId"], card_id)

        # Simulate an existing recycled source record; actual deletion tokens and
        # dependency handling are verified in test_mindos_p15_03_04_05.py.
        self.store.set_recycled("mindos_v1", True)
        card = knowledge.knowledge_detail(card_id)
        self.assertFalse(card["isArchived"])
        self.assertTrue(card["sources"][0]["recycled"])

        self.store.set_recycled("mindos_v1", False)
        self.assertFalse(knowledge.knowledge_detail(card_id)["sources"][0]["recycled"])

    def test_recycled_existing_source_can_be_replaced(self):
        self._material("mindos_v1")
        self._material("mindos_v2", family="mindos_v1", supersedes="mindos_v1")
        card_id = self._card_with_source("mindos_v1")
        self.store.set_recycled("mindos_v1", True)
        result = knowledge.knowledge_update_sources(
            card_id,
            knowledge.KnowledgeSourcesUpdate(
                sourceRefs=[knowledge.KnowledgeSourceRef(sourceType="material", id="mindos_v2")]
            ),
        )
        self.assertEqual(result["sourceRefs"][0]["id"], "mindos_v2")


class MaterialVersionTests(LifecycleTestCase):
    def test_legacy_material_is_migrated_to_single_member_version_family(self):
        legacy_db = Path(self.tmp.name) / "legacy-jobs.db"
        conn = sqlite3.connect(legacy_db)
        conn.executescript(
            """
            CREATE TABLE job_records (
                material_id TEXT PRIMARY KEY, file_name TEXT NOT NULL, file_type TEXT NOT NULL,
                source_path TEXT NOT NULL, job_id TEXT NOT NULL, created_at REAL NOT NULL,
                folder TEXT NOT NULL DEFAULT '未分类', folder_id INTEGER, canceled INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO job_records VALUES ('mindos_legacy', '旧资料.md', 'document', '/tmp/old.md', 'job_old', 1, '未分类', NULL, 0);
            """
        )
        conn.close()
        legacy_store = job_store.reset_for_tests(legacy_db)
        migrated = legacy_store.get("mindos_legacy")
        self.assertEqual(migrated["material_family_id"], "mindos_legacy")
        self.assertEqual(migrated["version_number"], 1)
        self.assertIsNone(migrated["supersedes_material_id"])
        self.store = job_store.reset_for_tests(Path(self.tmp.name) / "jobs.db")

    def test_version_chain_uses_family_and_does_not_change_card_source(self):
        self._material("mindos_v1")
        card_id = self._card_with_source("mindos_v1")
        with patch.object(
            uploads, "_receive_upload", new=AsyncMock(return_value=("v2.md", "document", Path("/tmp/v2.md")))
        ), patch.object(ingestion, "_submit_material_job", return_value=True):
            response = asyncio.run(
                uploads.mindos_material_version_upload("mindos_v1", Request({"type": "http"}), file=object(), versionNote="补充数据", targetFolderId=None)
            )
        new_id = response["newMaterialId"]
        self.assertEqual(response["versionNumber"], 2)
        self.assertEqual(response["materialFamilyId"], "mindos_v1")
        self.assertEqual(knowledge.knowledge_detail(card_id)["sources"][0]["id"], "mindos_v1")

        versions = uploads.mindos_material_versions(new_id, Request({"type": "http"}))["items"]
        self.assertEqual([item["versionNumber"] for item in versions], [2, 1])
        self.assertEqual(self.store.get(new_id)["supersedes_material_id"], "mindos_v1")

    def test_failed_new_version_has_no_impact_and_old_source_stays_usable(self):
        self._material("mindos_v1")
        self._material("mindos_v2", family="mindos_v1", supersedes="mindos_v1")
        card_id = self._card_with_source("mindos_v1")
        self.states["mindos_v2"] = "failed"
        impact = uploads.mindos_material_version_impact("mindos_v2", Request({"type": "http"}))
        self.assertFalse(impact["ready"])
        self.assertEqual(impact["status"], "failed")
        self.assertEqual(knowledge.knowledge_detail(card_id)["sources"][0]["id"], "mindos_v1")
        self.assertIsNone(self.store.get("mindos_v1")["superseded_by_material_id"])

    def test_user_can_explicitly_keep_both_existing_recycled_and_new_versions(self):
        self._material("mindos_v1")
        self._material("mindos_v2", family="mindos_v1", supersedes="mindos_v1")
        card_id = self._card_with_source("mindos_v1")
        self.store.set_recycled("mindos_v1", True)
        result = knowledge.knowledge_update_sources(
            card_id,
            knowledge.KnowledgeSourcesUpdate(sourceRefs=[
                knowledge.KnowledgeSourceRef(sourceType="material", id="mindos_v1"),
                knowledge.KnowledgeSourceRef(sourceType="material", id="mindos_v2"),
            ]),
        )
        self.assertEqual([item["id"] for item in result["sourceRefs"]], ["mindos_v1", "mindos_v2"])

    def test_available_version_impact_contains_cards_corrections_and_drafts(self):
        self._material("mindos_v1")
        self._material("mindos_v2", family="mindos_v1", supersedes="mindos_v1")
        card_id = self._card_with_source("mindos_v1")
        derived = derived_store.DerivedStore.instance()
        derived.create_correction("纠错", "旧说法", "新说法", ["旧"], ["mindos_v1"])
        derived.set_derived_record(
            "generation", "draft_p15", derived_svc.KIND_GENERATED_DRAFT, "ok",
            {"type": "study_note", "sourceRefs": [{"sourceType": "material", "id": "mindos_v1"}]},
            "hash", "test",
        )
        impact = uploads.mindos_material_version_impact("mindos_v2", Request({"type": "http"}))
        self.assertTrue(impact["ready"])
        self.assertEqual(impact["oldMaterialId"], "mindos_v1")
        self.assertEqual(impact["activeKnowledgeCards"][0]["knowledgeId"], card_id)
        self.assertEqual(len(impact["corrections"]), 1)
        self.assertEqual(impact["drafts"][0]["draftId"], "draft_p15")

    def test_version_routes_are_registered(self):
        uploads.configure_write_guard(lambda: True)
        app = FastAPI()
        app.include_router(uploads.router)
        spec = app.openapi()["paths"]
        self.assertIn("/api/mindos/materials/{material_id}/impact", spec)
        self.assertIn("/api/mindos/materials/{material_id}/versions", spec)
        self.assertIn("get", spec["/api/mindos/materials/{material_id}/versions"])
        self.assertIn("post", spec["/api/mindos/materials/{material_id}/versions"])
        self.assertIn("/api/mindos/materials/{material_id}/version-impact", spec)
