"""知君资料 → 观察型理解：从派生记录生成 observed 工作理解；资料删除后脱钩。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mindos.stores.ontology_store import OntologyStore
from mindos.zhijun import materials


ENTITY_RECORD = {
    "status": "ok",
    "content": {"items": [{"type": "person", "name": "林岚", "confidence": 0.9}, {"type": "organization", "name": "远川科技", "confidence": 0.8}, {"type": "term", "name": "新流程X", "confidence": 0.7}]},
}
RELATION_RECORD = {
    "status": "ok",
    "content": {
        "items": [
            {"relationId": "rel:1", "subject": {"type": "person", "name": "林岚"}, "predicate": "任职于", "object": {"type": "organization", "name": "远川科技"}, "confidence": 0.9, "evidence": "林岚现任远川科技产品负责人"},
            {"relationId": "rel:2", "subject": {"type": "term", "name": "新流程X"}, "predicate": "替代", "object": {"type": "term", "name": "旧流程Y"}, "confidence": 0.8, "evidence": "新流程X 将替代旧流程Y"},
        ]
    },
}


class MaterialClaimsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OntologyStore(Path(self._tmp.name) / "ontology.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_records_become_observed_working_claims(self) -> None:
        report = materials.run("mat_1", store=self.store, entity_record=ENTITY_RECORD, relation_record=RELATION_RECORD)
        self.assertEqual(report["entities"], 3)
        self.assertEqual(len(report["created"]), 2)
        claims = {c["content"]: c for c in self.store.list_claims(trust_states=("working",))}
        people = claims["林岚 任职于 远川科技"]
        self.assertEqual((people["section"], people["layer"], people["trustOrigin"]), ("people", "observed", "material"))
        self.assertEqual(people["evidence"][0]["kind"], "material_span")
        self.assertEqual(people["evidence"][0]["materialId"], "mat_1")
        self.assertEqual(people["subjectName"], "林岚")
        self.assertEqual(people["objectName"], "远川科技")
        matters = claims["新流程X 替代 旧流程Y"]
        self.assertEqual(matters["section"], "matters")
        self.assertEqual(self.store.find_entity("旧流程Y")["type"], "topic")
        again = materials.run("mat_1", store=self.store, entity_record=ENTITY_RECORD, relation_record=RELATION_RECORD)
        self.assertEqual(again["created"], [])
        self.assertEqual(again["reaffirmed"], 2)

    def test_detach_material_retracts_single_source_working_claims(self) -> None:
        materials.run("mat_1", store=self.store, entity_record=ENTITY_RECORD, relation_record=RELATION_RECORD)
        shared = self.store.list_claims(trust_states=("working",))[0]
        self.store.add_evidence(shared["id"], [{"kind": "conversation_turn", "conversation_id": "c1", "message_id": "m1", "quote": "我也提过"}])
        result = self.store.detach_material("mat_1")
        self.assertEqual(len(result["retracted"]), 1)
        self.assertEqual(len(result["kept"]), 1)
        kept = self.store.get_claim(shared["id"])
        self.assertEqual(kept["trustState"], "working")
        self.assertTrue(all(ev["materialId"] != "mat_1" for ev in kept["evidence"]))
        retracted = self.store.get_claim(result["retracted"][0])
        self.assertEqual(retracted["retractionReason"], "evidence_purged")

    def test_not_ok_records_are_ignored(self) -> None:
        report = materials.run("mat_2", store=self.store, entity_record={"status": "failed", "content": {"items": []}}, relation_record={"status": "skipped", "content": {"items": []}})
        self.assertEqual(report["created"], [])
        self.assertEqual(self.store.stats()["claims"]["working"], 0)


if __name__ == "__main__":
    unittest.main()
