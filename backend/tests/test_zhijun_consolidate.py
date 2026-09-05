"""知君整合器：实体合并候选、矛盾裁决、等价并入、原则张力提醒、多来源晋升、衰减；导出与全量删除。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import nudges, ontology
from mindos.stores import conversation_store as conversation_store_module
from mindos.stores import ontology_store as ontology_store_module
from mindos.zhijun import consolidate


def _ev(conv: str, msg: str = "m") -> list[dict]:
    # Real disposable origins are required by device-scoped API reads. Unknown
    # origin IDs represent deleted/unavailable sources, not legacy global data.
    store = conversation_store_module.ConversationStore.instance()
    title = "synthetic-origin-" + conv
    origin = next((c for c in store.list_conversations(status="all") if c["title"] == title), None)
    origin = origin or store.create_conversation(title=title)
    message = next((m for m in store.list_messages(origin["id"]) if m.get("meta", {}).get("fixtureMessage") == msg), None)
    message = message or store.append_message(origin["id"], "user", "x", meta={"fixtureMessage": msg})
    return [{"kind": "conversation_turn", "conversation_id": origin["id"], "message_id": message["id"], "quote": "x"}]


class ConsolidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.onto = ontology_store_module.reset_for_tests(root / "ontology.db")
        self.convs = conversation_store_module.reset_for_tests(root / "conversations.db")
        self._env = patch.dict(os.environ, {"ZHIJUN_PROVIDER": "fake"})
        self._env.start()
        app = FastAPI()
        app.include_router(ontology.router)
        app.include_router(nudges.router)
        self.client = TestClient(app)
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_heuristic_verdicts(self) -> None:
        self.assertEqual(consolidate.heuristic_verdict("我坚持先看数据再拍板", "我不坚持先看数据再拍板"), "contradict")
        self.assertEqual(consolidate.heuristic_verdict("我在做远川项目", "我在做远川项目。"), "equivalent")
        self.assertEqual(consolidate.heuristic_verdict("我喜欢早起", "我在做远川项目"), "unrelated")

    def test_entity_merge_proposal_and_accept(self) -> None:
        lin = self.onto.upsert_entity("林岚", "person")
        lan = self.onto.upsert_entity("岚姐", "person")
        # 通过别名把两者关联起来：给「岚姐」加别名「林岚」→ 别名相同 → 合并候选
        self.onto.upsert_entity("岚姐", "person", aliases=["林岚"])
        claim = self.onto.create_claim({"content": "我和岚姐合作远川", "section": "people", "predicate": "works_with", "layer": "self_declared", "object_entity_id": lan["id"]}, _ev("c1"), trust_state="confirmed", trust_origin="utterance")
        report = consolidate.run(store=self.onto, conv_store=self.convs, provider=None, now=self.now)
        self.assertEqual(report["mergeProposals"], 1)
        proposals = self.client.get("/api/mindos/ontology/proposals").json()
        self.assertEqual(len(proposals["merges"]), 1)
        merge = proposals["merges"][0]
        self.assertEqual({merge["fromName"], merge["intoName"]}, {"林岚", "岚姐"})
        res = self.client.post(f"/api/mindos/ontology/proposals/merges/{merge['id']}/resolve", json={"accept": True})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["status"], "accepted")
        survivor = self.onto.get_entity(merge["intoEntityId"])
        merged = self.onto.get_entity(merge["fromEntityId"])
        self.assertEqual(merged["status"], "merged")
        self.assertEqual(self.onto.get_claim(claim["id"])["objectEntityId"], survivor["id"])
        self.assertEqual(self.client.post(f"/api/mindos/ontology/proposals/merges/{merge['id']}/resolve", json={"accept": False}).status_code, 409)
        self.assertEqual(consolidate.run(store=self.onto, conv_store=self.convs, provider=None, now=self.now)["mergeProposals"], 0)

    def test_working_vs_confirmed_contradiction_challenges_working_and_decays(self) -> None:
        confirmed = self.onto.create_claim({"content": "我坚持先看数据再拍板", "section": "principles", "layer": "self_declared"}, _ev("c1"), trust_state="confirmed", trust_origin="utterance")
        working = self.onto.create_claim({"content": "我不坚持先看数据再拍板", "section": "principles", "layer": "hypothesis"}, _ev("c2"))
        report = consolidate.run(store=self.onto, conv_store=self.convs, provider=None, now=self.now)
        self.assertEqual(report["challenged"], 1)
        refreshed = self.onto.get_claim(working["id"])
        self.assertTrue(refreshed["challenged"])
        self.assertNotIn(working["id"], [c["id"] for c in self.onto.inbox()])
        self.assertNotIn(working["id"], [c["id"] for c in self.onto.search_claims("数据", k=5, trust_states=("working",))])
        later = self.now + timedelta(days=31)
        report = consolidate.run(store=self.onto, conv_store=self.convs, provider=None, now=later)
        self.assertEqual(report["decayed"], 1)
        self.assertEqual(self.onto.get_claim(working["id"])["trustState"], "retracted")
        self.assertEqual(self.onto.get_claim(working["id"])["retractionReason"], "decayed_contradicted")
        self.assertEqual(self.onto.get_claim(confirmed["id"])["trustState"], "confirmed")

    def test_two_confirmed_contradiction_becomes_conflict_and_resolve(self) -> None:
        a = self.onto.create_claim({"content": "我坚持先看数据再拍板", "section": "principles", "layer": "self_declared"}, _ev("c1"), trust_state="confirmed", trust_origin="utterance")
        b = self.onto.create_claim({"content": "我不坚持先看数据再拍板", "section": "principles", "layer": "self_declared"}, _ev("c2"), trust_state="confirmed", trust_origin="utterance")
        report = self.client.post("/api/mindos/ontology/consolidate").json()
        self.assertEqual(report["conflicts"], 1)
        stats = self.client.get("/api/mindos/ontology/stats").json()
        self.assertEqual(stats["proposals"], 1)
        conflict = self.client.get("/api/mindos/ontology/proposals").json()["conflicts"][0]
        self.assertEqual(conflict["kind"], "contradiction")
        keep_a = conflict["claimA"]["id"] == a["id"]
        res = self.client.post(f"/api/mindos/ontology/proposals/conflicts/{conflict['id']}/resolve", json={"keep": "a" if keep_a else "b"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(self.onto.get_claim(a["id"])["trustState"], "confirmed")
        self.assertEqual(self.onto.get_claim(b["id"])["trustState"], "retracted")
        self.assertEqual(self.client.post(f"/api/mindos/ontology/proposals/conflicts/{conflict['id']}/resolve", json={"keep": "both"}).status_code, 409)

    def test_equivalent_working_is_folded_into_older(self) -> None:
        older = self.onto.create_claim({"content": "我在做远川项目", "section": "matters", "layer": "self_declared"}, _ev("c1"), trust_state="confirmed", trust_origin="utterance")
        newer = self.onto.create_claim({"content": "我在做远川项目。", "section": "matters", "layer": "hypothesis", "predicate": "committed_to"}, _ev("c2"))
        report = consolidate.run(store=self.onto, conv_store=self.convs, provider=None, now=self.now)
        self.assertEqual(report["merged"], 1)
        self.assertEqual(self.onto.get_claim(newer["id"])["trustState"], "retracted")
        self.assertEqual(len(self.onto.get_claim(older["id"])["evidence"]), 2)

    def test_principle_tension_creates_question_nudge(self) -> None:
        self.onto.create_claim({"content": "我从不在周末加班", "section": "principles", "layer": "self_declared"}, _ev("c1"), trust_state="confirmed", trust_origin="utterance")
        self.onto.create_claim({"content": "我这周末在加班赶远川项目", "section": "matters", "layer": "self_declared"}, _ev("c2"), trust_state="confirmed", trust_origin="utterance")
        report = consolidate.run(store=self.onto, conv_store=self.convs, provider=None, now=self.now)
        self.assertEqual(report["tensions"], 1)
        today = self.client.get("/api/mindos/nudges/today").json()["items"]
        self.assertEqual(today[0]["kind"], "principle_tension")
        self.assertTrue(today[0]["message"].endswith("？"))
        self.assertIn("principleId", today[0]["triggerRef"])
        conflicts = self.client.get("/api/mindos/ontology/proposals").json()["conflicts"]
        self.assertEqual(conflicts[0]["kind"], "tension")
        self.assertEqual(consolidate.run(store=self.onto, conv_store=self.convs, provider=None, now=self.now)["tensions"], 0)

    def test_promotion_ready_and_stale_defer(self) -> None:
        multi = self.onto.create_claim({"content": "我可能更信数据", "section": "ways", "layer": "hypothesis"}, _ev("c1"))
        self.onto.add_evidence(multi["id"], _ev("c2"))
        stale = self.onto.create_claim({"content": "我大概偏内向", "section": "who", "layer": "hypothesis"}, _ev("c3"))
        report = consolidate.run(store=self.onto, conv_store=self.convs, provider=None, now=self.now + timedelta(days=61))
        self.assertEqual(report["promoted"], 1)
        self.assertGreaterEqual(report["deferred"], 1)
        inbox = self.onto.inbox()
        self.assertEqual(inbox[0]["id"], multi["id"])
        self.assertTrue(inbox[0]["promotionReady"])
        self.assertNotIn(stale["id"], [c["id"] for c in inbox])

    def test_should_run_daily_or_after_twenty_claims(self) -> None:
        self.assertTrue(consolidate.should_run(self.onto, now=self.now))
        consolidate.run(store=self.onto, conv_store=self.convs, provider=None, now=self.now)
        self.assertFalse(consolidate.should_run(self.onto, now=self.now + timedelta(hours=1)))
        for i in range(20):
            self.onto.create_claim({"content": f"我第{i}条理解", "section": "who", "layer": "hypothesis"}, _ev(f"c{i}"))
        self.assertTrue(consolidate.should_run(self.onto, now=self.now + timedelta(hours=1)))
        self.assertTrue(consolidate.should_run(self.onto, now=self.now + timedelta(days=2)))

    def test_export_and_purge(self) -> None:
        self.onto.create_claim({"content": "我在做远川项目", "section": "matters", "layer": "self_declared"}, _ev("c1"), trust_state="confirmed", trust_origin="utterance")
        self.onto.create_claim({"content": "我可能偏内向", "section": "who", "layer": "hypothesis"}, _ev("c2"))
        export = self.client.get("/api/mindos/ontology/export").json()
        self.assertEqual(len(export["claims"]), 1)
        self.assertEqual(len(self.client.get("/api/mindos/ontology/export", params={"includeWorking": "true"}).json()["claims"]), 2)
        self.assertEqual(self.client.get("/api/mindos/ontology/export", params={"sections": "who"}).json()["claims"], [])
        self.assertEqual(self.client.get("/api/mindos/ontology/export", params={"sections": "nope"}).status_code, 400)
        conv = self.convs.create_conversation()
        self.convs.append_message(conv["id"], "user", "hi")
        bad = self.client.post("/api/mindos/ontology/purge", json={"confirm": "删除"})
        self.assertEqual(bad.status_code, 400)
        with patch("mindos.zhijun.projection.write_projection", return_value={}):
            ok = self.client.post("/api/mindos/ontology/purge", json={"confirm": "删除全部记忆"})
        self.assertEqual(ok.status_code, 200, ok.text)
        body = ok.json()
        self.assertEqual(body["ontology"]["claims"], 2)
        self.assertEqual(body["conversations"]["conversations"], 3)  # two evidence origins plus the explicit conversation
        self.assertEqual(self.onto.stats()["claims"]["confirmed"], 0)
        self.assertIsNotNone(self.onto.get_entity("ent_me"))
        self.assertEqual(self.convs.list_conversations(), [])


if __name__ == "__main__":
    unittest.main()
