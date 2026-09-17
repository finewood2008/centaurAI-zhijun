"""主动开口（V3 M5）：meta 契约、无模型、中性开场、空本体、routingSources、不引用 sensitive、引用长度、不可核实来源回退。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import conversations, ontology
from mindos.stores import conversation_store as conversation_store_module
from mindos.stores import growth_store as growth_store_module
from mindos.stores import ontology_store as ontology_store_module
from mindos.stores.charter_draft_store import render_document
from mindos.zhijun import inquiry, persona


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ChatOpeningTests(unittest.TestCase):
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
        self.client = TestClient(app)
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def claim(self, content, *, section="who", predicate="role", trust="confirmed", privacy="private", valid_to=None):
        return self.onto.create_claim({"content": content, "section": section, "layer": "self_declared" if trust == "confirmed" else "hypothesis",
                                       "predicate": predicate, "privacy_level": privacy, "valid_to": valid_to},
                                      [{"kind": "user_edit", "quote": content}], trust_state=trust,
                                      trust_origin="user_created" if trust == "confirmed" else "model")

    def summary(self, points):
        conv = self.convs.create_conversation()
        message = self.convs.append_message(conv["id"], "user", "上次说的话")
        return self.convs.save_summary(conv["id"], up_to_seq=message["seq"], summary="s", key_points=points,
                                       meta={"routingSources": [{"kind": "message", "id": message["id"]}]})

    def create(self, **body):
        with patch("mindos.zhijun.provider.build_provider", side_effect=AssertionError("opening must not call a model")):
            res = self.client.post("/api/mindos/conversations", json={"mode": "chat", **body})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def test_empty_ontology_gets_neutral_opening_with_meta_contract(self) -> None:
        conv = self.create()
        opening = conv["opening"]
        self.assertEqual(opening["role"], "assistant")
        self.assertEqual(opening["content"], persona.NEUTRAL_OPENING)
        self.assertEqual((opening["provider"], opening["model"], opening["external"]), ("template", "template", False))
        self.assertEqual(opening["meta"], {"kind": "chat_open", "routingOrigin": {"service": "", "external": False}, "routingSources": [], "inquiry": None})
        self.assertFalse(conv["reused"])
        messages = self.client.get(f"/api/mindos/conversations/{conv['id']}").json()["messages"]
        self.assertEqual([m["id"] for m in messages], [opening["id"]])
        self.assertEqual(conv["messageCount"], 1)
        self.assertEqual(conv["title"], "")
        self.assertIsNone(self.onto.meta_get("zhijun_inquiry_recent_v1"))

    def test_profile_theme_and_target_compose_opening_with_sources(self) -> None:
        self.claim("我是产品负责人")
        saved = self.summary(["远川项目定价", "招人", "待办：约投资人"])
        conv = self.create()
        opening = conv["opening"]
        self.assertTrue(opening["content"].startswith("上次我们聊到「远川项目定价」。"), opening["content"])
        self.assertIn("约投资人", opening["content"])
        self.assertTrue(opening["content"].endswith("？"))
        summary_id = f"{saved['conversationId']}:{saved['revision']}"
        refs = opening["meta"]["routingSources"]
        self.assertTrue(all(ref.get("version") for ref in refs))
        self.assertEqual({(r["kind"], r["id"]) for r in refs}, {("summary", summary_id)})
        self.assertEqual(opening["meta"]["inquiry"]["kind"], "open_loop")
        self.assertEqual(opening["meta"]["inquiry"]["key"], f"loop:{summary_id}:2")
        self.assertIn(opening["meta"]["inquiry"]["key"], inquiry.recent_keys(self.onto, "global"))
        second = self.create()["opening"]
        self.assertNotEqual(second["content"], opening["content"])
        self.assertNotIn("约投资人", second["content"])

    def test_target_only_opening_and_neutral_when_not_proactive(self) -> None:
        self.claim("我是产品负责人")
        opening = self.create()["opening"]
        self.assertIn(opening["content"], set(inquiry.GAP_QUESTIONS.values()))
        self.assertEqual(opening["meta"]["routingSources"], [])
        self.assertEqual(opening["meta"]["inquiry"]["kind"], "gap")
        self.growth.create_charter({"document": render_document([{"id": "quiet", "section": "与知君合作", "text": "不要主动提醒我", "kind": "boundary", "scope": "global",
                                                                  "context": "", "control": "no_proactive", "sources": [], "quote": "不要主动提醒我"}]),
                                    "clauses": [{"id": "quiet", "section": "与知君合作", "text": "不要主动提醒我", "kind": "boundary", "scope": "global",
                                                 "context": "", "control": "no_proactive", "sources": [], "quote": "不要主动提醒我"}],
                                    "expectedVersion": 0, "workspaceId": "synthetic-test-workspace", "metadata": {"scope": "global", "origin": "workspace", "sources": []}})
        quiet = self.create()["opening"]
        self.assertEqual(quiet["content"], persona.NEUTRAL_OPENING)
        self.assertIsNone(quiet["meta"]["inquiry"])

    def test_no_opening_for_review_onboarding_or_charter_task(self) -> None:
        self.claim("我是产品负责人")
        charter = self.create(taskContext="charter")
        self.assertNotIn("opening", charter)
        self.assertEqual(self.client.get(f"/api/mindos/conversations/{charter['id']}").json()["messages"], [])
        decision = self.growth.create_decision({"title": "要不要外包", "context": "c", "options": ["a", "b"], "choice": "a", "rationale": "r", "confidence": 60,
                                                "expectedOutcome": "e", "reviewAt": _iso(self.now - timedelta(days=1)), "relatedEntityIds": [], "evidenceRefs": []})
        review = self.client.post("/api/mindos/conversations", json={"mode": "review", "decisionId": decision["id"]}).json()
        self.assertNotIn("opening", review)
        self.assertEqual([m["meta"]["kind"] for m in self.client.get(f"/api/mindos/conversations/{review['id']}").json()["messages"]], ["review_open"])
        onboarding = self.client.post("/api/mindos/conversations", json={"mode": "onboarding"}).json()
        self.assertNotIn("opening", onboarding)

    def test_never_cites_sensitive_and_quotes_are_short(self) -> None:
        self.claim("我是产品负责人")
        self.claim("我有一段敏感的往事", predicate="background", privacy="sensitive", trust="working")
        secret = self.claim("我承诺处理一件敏感的家事", section="matters", predicate="committed_to", privacy="sensitive", valid_to=_iso(self.now - timedelta(hours=1)))
        long_content = "我承诺" + "把这个特别长的方案一直改到大家都满意为止" * 3
        stale = self.claim(long_content[:120], section="matters", predicate="committed_to", valid_to=_iso(self.now - timedelta(hours=1)))
        opening = self.create()["opening"]
        self.assertNotIn("敏感", opening["content"])
        self.assertNotIn(secret["id"], [r["id"] for r in opening["meta"]["routingSources"]])
        self.assertEqual(opening["meta"]["inquiry"]["targetId"], stale["id"])
        quoted = opening["content"].split("「", 1)[1].split("」", 1)[0]
        self.assertLessEqual(len(quoted), 40)

    def test_unverifiable_source_falls_back_to_neutral(self) -> None:
        self.claim("我是产品负责人")
        conv = self.convs.create_conversation()
        self.convs.save_summary(conv["id"], up_to_seq=1, summary="s", key_points=["远川项目定价", "待办：约投资人"], meta={})
        opening = self.create()["opening"]
        self.assertEqual(opening["content"], persona.NEUTRAL_OPENING)
        self.assertEqual(opening["meta"]["routingSources"], [])
        self.assertIsNone(opening["meta"]["inquiry"])

    def test_persona_chat_opening_templates(self) -> None:
        theme = {"section": "recent", "kind": "theme", "content": "远川项目定价；招人", "ref": {"kind": "summary", "id": "c:1"}}
        target = {"question": "上次你说要「约投资人」，后来怎么样了？", "refs": [{"kind": "summary", "id": "c:1"}]}
        text, sources = persona.chat_opening([theme], target)
        self.assertEqual(text, "上次我们聊到「远川项目定价」。上次你说要「约投资人」，后来怎么样了？")
        self.assertEqual(sources, [{"kind": "summary", "id": "c:1"}, {"kind": "summary", "id": "c:1"}])
        self.assertEqual(persona.chat_opening([theme], None), ("上次我们聊到「远川项目定价」。今天从哪里开始都可以。", [{"kind": "summary", "id": "c:1"}]))
        self.assertEqual(persona.chat_opening([{"section": "who", "kind": "claim", "content": "x", "ref": {"kind": "claim", "id": "k"}}], target), (target["question"], target["refs"]))
        self.assertEqual(persona.chat_opening([], target), (persona.NEUTRAL_OPENING, []))
        self.assertEqual(persona.chat_opening([theme], target, proactive=False), (persona.NEUTRAL_OPENING, []))
        long_theme = {"section": "recent", "kind": "theme", "content": "这是一个特别长的主题名字用来测试二十四字的截断规则到底有没有生效", "ref": {"kind": "summary", "id": "c:2"}}
        text, _ = persona.chat_opening([long_theme], target)
        self.assertLessEqual(len(text.split("「", 1)[1].split("」", 1)[0]), 24)


if __name__ == "__main__":
    unittest.main()
