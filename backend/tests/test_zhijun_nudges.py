"""知君提醒与回访：到期判断 → review_due 提醒、每日上限、静默领域、去重、永久静默、回访会话记结果。"""
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
from mindos.zhijun import nudges as nudge_service


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _reply(sse_text: str) -> str:
    """把 SSE 里的 token 片段拼回完整回复（关键词可能被分片切开）。"""
    parts = []
    for frame in sse_text.split("\n\n"):
        lines = frame.strip().split("\n")
        if lines and lines[0] == "event: token":
            data = "\n".join(l[5:].strip() for l in lines[1:] if l.startswith("data:"))
            parts.append(json.loads(data)["t"])
    return "".join(parts)


def _decision(title: str, review_at: datetime | None) -> dict:
    return {
        "title": title,
        "context": "背景",
        "options": ["A", "B"],
        "choice": "A",
        "rationale": "因为 A 更稳",
        "confidence": 60,
        "expectedOutcome": "两周内见效",
        "reviewAt": _iso(review_at) if review_at else None,
        "relatedEntityIds": [],
        "evidenceRefs": [],
    }


class NudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.onto = ontology_store_module.reset_for_tests(root / "ontology.db")
        self.convs = conversation_store_module.reset_for_tests(root / "conversations.db")
        self.growth = growth_store_module.reset_for_tests(root / "growth.db")
        self._env = patch.dict(os.environ, {"ZHIJUN_PROVIDER": "fake", "ZHIJUN_MATERIAL_EVIDENCE": "0"})
        self._env.start()
        app = FastAPI()
        app.include_router(conversations.router)
        app.include_router(ontology.router)
        app.include_router(nudges.router)
        app.include_router(growth.router)
        self.client = TestClient(app)
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_scan_creates_review_due_with_why_now_and_dedupes(self) -> None:
        overdue = self.growth.create_decision(_decision("要不要涨价", self.now - timedelta(days=2)))
        self.growth.create_decision(_decision("下季度招人", self.now + timedelta(days=10)))
        result = nudge_service.scan(conv_store=self.convs, growth=self.growth, now=self.now)
        self.assertEqual(result["created"], 1)
        today = self.client.get("/api/mindos/nudges/today").json()
        self.assertEqual(len(today["items"]), 1)
        item = today["items"][0]
        self.assertEqual(item["kind"], "review_due")
        self.assertEqual(item["triggerRef"]["decisionId"], overdue["id"])
        self.assertIn("已经过了 2 天", item["whyNow"])
        self.assertEqual(item["status"], "shown")
        self.assertEqual(nudge_service.scan(conv_store=self.convs, growth=self.growth, now=self.now)["created"], 0)

    def test_quiet_domains_and_daily_cap(self) -> None:
        self.growth.create_charter(
            {"vision": "v", "roles": [], "principles": [], "boundaries": [], "goals": [], "challengeStyle": "温和", "quietDomains": ["家庭"]}
        )
        for i in range(5):
            self.growth.create_decision(_decision(f"工作判断{i}", self.now - timedelta(days=1)))
        self.growth.create_decision(_decision("家庭矛盾怎么处理", self.now - timedelta(days=1)))
        result = nudge_service.scan(conv_store=self.convs, growth=self.growth, now=self.now)
        self.assertEqual(result["created"], 5)
        today = self.client.get("/api/mindos/nudges/today").json()
        self.assertEqual(len(today["items"]), 3)
        self.assertTrue(all("家庭" not in i["message"] for i in today["items"]))
        self.client.put("/api/mindos/nudges/policy", json={"maxPerDay": 5})
        self.assertEqual(len(self.client.get("/api/mindos/nudges/today").json()["items"]), 5)
        self.client.put("/api/mindos/nudges/policy", json={"enabled": False})
        self.assertEqual(self.client.get("/api/mindos/nudges/today").json()["items"], [])

    def test_dismiss_and_silence(self) -> None:
        decision = self.growth.create_decision(_decision("要不要涨价", self.now - timedelta(days=1)))
        self.client.post("/api/mindos/nudges/scan")
        item = self.client.get("/api/mindos/nudges/today").json()["items"][0]
        self.assertEqual(self.client.post(f"/api/mindos/nudges/{item['id']}/dismiss").json()["status"], "dismissed")
        self.assertEqual(self.client.get("/api/mindos/nudges/today").json()["items"], [])
        # 去重窗口内不会重建；窗口外静默则永久生效
        self.assertEqual(nudge_service.scan(conv_store=self.convs, growth=self.growth, now=self.now)["created"], 0)
        later = self.now + timedelta(days=5)
        self.assertEqual(nudge_service.scan(conv_store=self.convs, growth=self.growth, now=later)["created"], 1)
        item = self.client.get("/api/mindos/nudges/today").json()["items"][0]
        silenced = self.client.post(f"/api/mindos/nudges/{item['id']}/silence").json()
        self.assertIn(f"review_due:{decision['id']}", silenced["policy"]["silencedRefs"])
        self.assertEqual(silenced["nudge"]["status"], "silenced")
        self.assertEqual(nudge_service.scan(conv_store=self.convs, growth=self.growth, now=later + timedelta(days=10))["created"], 0)
        self.assertEqual(self.client.post("/api/mindos/nudges/ndg_missing/dismiss").status_code, 404)

    def test_quiet_domain_from_onboarding_claims(self) -> None:
        """建档里说过「不希望主动提」的话题，也算静默领域：逾期判断标题命中就不提醒。"""
        onto = ontology_store_module.OntologyStore.instance()
        origin = self.convs.create_conversation(mode="onboarding")
        message = self.convs.append_message(origin["id"], "user", "健康和家里的矛盾这些话题不用主动提")
        onto.create_claim(
            {"content": "我不希望AI主动提起健康和家里的矛盾这些话题", "section": "principles", "layer": "self_declared"},
            [{"kind": "conversation_turn", "conversation_id": origin["id"], "message_id": message["id"], "quote": message["content"]}],
            trust_state="confirmed",
            trust_origin="utterance",
        )
        self.assertEqual(nudge_service.quiet_words_from_text("我不希望AI主动提起健康和家里的矛盾这些话题"), ["健康", "家里的矛盾"])
        self.assertEqual(nudge_service.quiet_words_from_text("健康话题不用主动提"), ["健康"])
        self.assertEqual(nudge_service._quiet_words(None, onto), ["健康", "家里的矛盾"])
        quiet = self.growth.create_decision(_decision("家里的矛盾要不要摊开说", self.now - timedelta(days=2)))
        loud = self.growth.create_decision(_decision("要不要涨价", self.now - timedelta(days=2)))
        result = nudge_service.scan(conv_store=self.convs, growth=self.growth, now=self.now)
        self.assertEqual(result["created"], 1)
        items = self.convs.list_nudges()
        self.assertEqual([n["triggerRef"]["decisionId"] for n in items], [loud["id"]])
        self.assertNotIn(quiet["id"], [n["triggerRef"].get("decisionId") for n in items])

    def test_commitment_due_and_weekly_review(self) -> None:
        onto = ontology_store_module.OntologyStore.instance()
        due = _iso(self.now - timedelta(hours=2))
        origin = self.convs.create_conversation()
        message = self.convs.append_message(origin["id"], "user", "三个月内把团队招齐")
        commit = onto.create_claim(
            {"content": "我承诺三个月内把团队招齐", "section": "matters", "layer": "self_declared", "predicate": "committed_to", "valid_to": due},
            [{"kind": "conversation_turn", "conversation_id": origin["id"], "message_id": message["id"], "quote": message["content"]}],
            trust_state="confirmed",
            trust_origin="utterance",
        )
        result = nudge_service.scan(conv_store=self.convs, growth=self.growth, now=self.now)
        kinds = [n["kind"] for n in self.convs.list_nudges()]
        self.assertIn("commitment_due", kinds)
        self.assertNotIn("weekly_review", kinds)  # 非周日不触发
        item = next(n for n in self.convs.list_nudges() if n["kind"] == "commitment_due")
        self.assertEqual(item["triggerRef"]["claimId"], commit["id"])
        self.assertIn("期限", item["whyNow"])
        # 周日 + 本周有判断 → 每周回顾，且一周内只有一条
        sunday = self.now + timedelta(days=(6 - self.now.weekday()) % 7)
        self.growth.create_decision(_decision("要不要涨价", sunday + timedelta(days=3)))
        with patch.dict(os.environ, {"ZHIJUN_WEEKLY_ANYDAY": "1"}):
            first = nudge_service.scan(conv_store=self.convs, growth=self.growth, now=self.now)
            again = nudge_service.scan(conv_store=self.convs, growth=self.growth, now=self.now)
        weekly = [n for n in self.convs.list_nudges() if n["kind"] == "weekly_review"]
        self.assertEqual(len(weekly), 1)
        self.assertIn("要不要花五分钟一起看看", weekly[0]["message"])
        self.assertNotIn("连续", weekly[0]["message"])

    def test_review_conversation_records_outcome_and_acts_nudge(self) -> None:
        decision = self.growth.create_decision(_decision("要不要涨价", self.now - timedelta(days=1)))
        self.client.post("/api/mindos/nudges/scan")
        self.assertEqual(self.client.post("/api/mindos/conversations", json={"mode": "review"}).status_code, 400)
        self.assertEqual(self.client.post("/api/mindos/conversations", json={"mode": "review", "decisionId": "dec_missing"}).status_code, 404)
        conv = self.client.post("/api/mindos/conversations", json={"mode": "review", "decisionId": decision["id"]}).json()
        self.assertEqual(conv["decisionId"], decision["id"])
        self.assertTrue(conv["title"].startswith("回访："))
        detail = self.client.get(f"/api/mindos/conversations/{conv['id']}").json()
        self.assertEqual(detail["messages"][0]["meta"]["kind"], "review_open")
        self.assertEqual(detail["decision"]["id"], decision["id"])

        res = self.client.post(f"/api/mindos/conversations/{conv['id']}/messages", json={"content": "我们把价格提了一成，客户基本没流失"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("回访", _reply(res.text))
        self.assertIn("记下结果", _reply(res.text))

        outcome = self.client.post(f"/api/mindos/conversations/{conv['id']}/outcome", json={"result": "提价一成，客户没流失", "notes": "比预期好"})
        self.assertEqual(outcome.status_code, 200, outcome.text)
        body = outcome.json()
        self.assertEqual(body["decision"]["status"], "outcome_recorded")
        self.assertEqual(body["nudgesActed"], 1)
        self.assertEqual(self.client.get("/api/mindos/nudges/today").json()["items"], [])
        detail = self.client.get(f"/api/mindos/conversations/{conv['id']}").json()
        self.assertEqual(detail["messages"][-1]["meta"]["kind"], "outcome_recorded")
        again = self.client.post(f"/api/mindos/conversations/{conv['id']}/outcome", json={"result": "再记一次"})
        self.assertEqual(again.status_code, 409)

        res = self.client.post(f"/api/mindos/conversations/{conv['id']}/messages", json={"content": "感觉当时低估了客户的接受度"})
        self.assertIn("复盘", _reply(res.text))

        plain = self.client.post("/api/mindos/conversations", json={}).json()
        self.assertEqual(self.client.post(f"/api/mindos/conversations/{plain['id']}/outcome", json={"result": "x"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
