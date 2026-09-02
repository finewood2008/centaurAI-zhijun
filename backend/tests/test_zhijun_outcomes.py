"""对话产出与回访会话：GET /conversations/{id}/outcomes、列表项 outcomes 计数、同判断回访会话复用与模板开场。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import conversations, growth, nudges, ontology
from mindos.stores import conversation_store as conversation_store_module
from mindos.stores import growth_store as growth_store_module
from mindos.stores import ontology_store as ontology_store_module


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _reply(sse_text: str) -> str:
    parts = []
    for frame in sse_text.split("\n\n"):
        lines = frame.strip().split("\n")
        if lines and lines[0] == "event: token":
            parts.append(json.loads("\n".join(l[5:].strip() for l in lines[1:] if l.startswith("data:")))["t"])
    return "".join(parts)


class OutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.onto = ontology_store_module.reset_for_tests(root / "ontology.db")
        self.convs = conversation_store_module.reset_for_tests(root / "conversations.db")
        self.growth = growth_store_module.reset_for_tests(root / "growth.db")
        self._env = patch.dict(os.environ, {"ZHIJUN_PROVIDER": "fake", "ZHIJUN_MATERIAL_EVIDENCE": "0"})
        self._env.start()
        app = FastAPI()
        for module in (conversations, ontology, nudges, growth):
            app.include_router(module.router)
        self.client = TestClient(app)
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _evidence(self, conversation_id: str, message_id: str = "m1") -> list[dict]:
        return [{"kind": "conversation_turn", "conversation_id": conversation_id, "message_id": message_id, "quote": "原话"}]

    def _outcomes(self, conversation_id: str) -> dict:
        res = self.client.get(f"/api/mindos/conversations/{conversation_id}/outcomes")
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def _list_item(self, conversation_id: str) -> dict:
        items = self.client.get("/api/mindos/conversations").json()["items"]
        return next(i for i in items if i["id"] == conversation_id)

    def test_outcomes_after_two_claims_and_one_confirm(self) -> None:
        conv = self.client.post("/api/mindos/conversations", json={"mode": "chat"}).json()
        other = self.client.post("/api/mindos/conversations", json={"mode": "chat"}).json()
        first = self.onto.create_claim({"content": "我在做远川项目", "section": "matters", "layer": "self_declared"}, self._evidence(conv["id"]))
        second = self.onto.create_claim({"content": "我想明年把公司做到盈利", "section": "direction", "layer": "aspirational"}, self._evidence(conv["id"]))
        self.onto.create_claim({"content": "我周末想多陪家人", "section": "who", "layer": "self_declared"}, self._evidence(other["id"]))
        review = self.client.post(f"/api/mindos/ontology/claims/{first['id']}/review", json={"action": "confirm", "surface": "conversation", "conversationId": conv["id"]})
        self.assertEqual(review.status_code, 200, review.text)

        out = self._outcomes(conv["id"])
        self.assertEqual(out["conversationId"], conv["id"])
        self.assertEqual([c["id"] for c in out["confirmedClaims"]], [first["id"]])
        self.assertEqual([c["id"] for c in out["workingClaims"]], [second["id"]])
        self.assertEqual(set(out["confirmedClaims"][0]), {"id", "content", "section", "layer"})
        self.assertIsNone(out["decision"])
        self.assertEqual(out["commitments"], [])
        self.assertEqual(out["pendingJobs"], 0)
        self.assertEqual(out["retracted"], 0)

        item = self._list_item(conv["id"])
        self.assertEqual(item["mode"], "chat")
        self.assertEqual(item["outcomes"], {"confirmed": 1, "working": 1, "decision": False, "commitments": 0})
        self.assertEqual(self._list_item(other["id"])["outcomes"], {"confirmed": 0, "working": 1, "decision": False, "commitments": 0})

        # 承诺（committed_to 且带 validTo）与本会话确认入簿的判断
        due = _iso(self.now + timedelta(days=30))
        commit = self.onto.create_claim(
            {"content": "我承诺三个月内把团队招齐", "section": "matters", "layer": "self_declared", "predicate": "committed_to", "valid_to": due},
            self._evidence(conv["id"], "m2"),
            trust_state="confirmed",
            trust_origin="utterance",
        )
        self.convs.upsert_draft(conv["id"], {"title": "测试要不要外包", "choice": "自己做", "rationale": "控制力更强", "confidence": 60, "expectedOutcome": "两周内见效"})
        confirmed = self.client.post(f"/api/mindos/conversations/{conv['id']}/decision-draft/confirm", json={})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        decision_id = confirmed.json()["decision"]["id"]

        out = self._outcomes(conv["id"])
        self.assertEqual(out["decision"]["id"], decision_id)
        self.assertEqual(set(out["decision"]), {"id", "title", "choice", "reviewAt", "status"})
        self.assertEqual(out["decision"]["title"], "测试要不要外包")
        self.assertEqual(out["commitments"], [{"claimId": commit["id"], "content": "我承诺三个月内把团队招齐", "validTo": commit["validTo"]}])
        self.assertEqual(len(out["confirmedClaims"]), 2)
        self.assertEqual(self._list_item(conv["id"])["outcomes"], {"confirmed": 2, "working": 1, "decision": True, "commitments": 1})

        # 撤回后不再计入已确认，retracted 计数加一
        self.client.post(f"/api/mindos/ontology/claims/{first['id']}/review", json={"action": "retract", "surface": "ontology_page"})
        out = self._outcomes(conv["id"])
        self.assertEqual([c["id"] for c in out["confirmedClaims"]], [commit["id"]])
        self.assertEqual(out["retracted"], 1)
        self.assertEqual(self.client.get("/api/mindos/conversations/conv_missing/outcomes").status_code, 404)

    def test_review_conversation_reused_with_template_opening(self) -> None:
        decision = self.growth.create_decision({
            "title": "要不要涨价", "context": "背景", "options": ["A", "B"], "choice": "A", "rationale": "因为 A 更稳", "confidence": 60,
            "expectedOutcome": "两周内见效", "reviewAt": _iso(self.now - timedelta(days=1)), "relatedEntityIds": [], "evidenceRefs": [],
        })
        first = self.client.post("/api/mindos/conversations", json={"mode": "review", "decisionId": decision["id"]}).json()
        self.assertFalse(first["reused"])
        self.assertEqual(first["mode"], "review")
        detail = self.client.get(f"/api/mindos/conversations/{first['id']}").json()
        opening = detail["messages"][0]
        self.assertEqual(opening["role"], "assistant")
        self.assertEqual(opening["meta"]["kind"], "review_open")
        self.assertEqual(opening["provider"], "template")
        self.assertEqual(opening["model"], "template")
        self.assertEqual(opening["content"], "「要不要涨价」到了回访的时候。当时你选了「A」，预期是「两周内见效」。先别急着说结果，这段时间你感觉怎么样？")

        second = self.client.post("/api/mindos/conversations", json={"mode": "review", "decisionId": decision["id"]}).json()
        self.assertTrue(second["reused"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.client.get(f"/api/mindos/conversations/{first['id']}").json()["messages"]), 1)

        res = self.client.post(f"/api/mindos/conversations/{first['id']}/messages", json={"content": "我们把价格提了一成，客户基本没流失"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("回访", _reply(res.text))
        out = self._outcomes(first["id"])
        self.assertEqual(out["decision"]["id"], decision["id"])
        self.assertTrue(self._list_item(first["id"])["outcomes"]["decision"])


if __name__ == "__main__":
    unittest.main()
