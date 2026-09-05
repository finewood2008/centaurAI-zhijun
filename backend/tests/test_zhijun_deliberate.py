"""知君商量模式：草稿字段只能来自用户原话、把握必须是用户说的、确认写入判断簿并绑定章程版本。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import conversations, growth, nudges, ontology
from mindos.stores import conversation_store as conversation_store_module
from mindos.stores import growth_store as growth_store_module
from mindos.stores import ontology_store as ontology_store_module
from mindos.zhijun import context, deliberate, jobs
from mindos.zhijun.provider import fake_draft

USER_TEXTS = [
    "我在纠结要不要把远川项目外包出去还是自己招人做。",
    "我倾向自己招人，因为控制力更强，七成把握，预期三个月内团队到位。",
]


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        name, data = None, []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        events.append((name or "message", json.loads("\n".join(data) or "{}")))
    return events


class ParseAndValidateTests(unittest.TestCase):
    def test_parse_confidence(self) -> None:
        self.assertEqual(deliberate.parse_confidence("七成"), 70)
        self.assertEqual(deliberate.parse_confidence("70%"), 70)
        self.assertEqual(deliberate.parse_confidence(55), 55)
        self.assertIsNone(deliberate.parse_confidence(120))
        self.assertIsNone(deliberate.parse_confidence(True))
        self.assertIsNone(deliberate.parse_confidence("很有把握"))

    def test_user_only_fields_must_come_from_user_text(self) -> None:
        raw = {
            "title": "远川项目怎么做",
            "context": "外包 vs 自己招人",
            "options": ["外包出去", "自己招人做"],
            "leaning": "我倾向自己招人",
            "choice": "自己招人（这是模型编的）",
            "rationale": "因为控制力更强",
            "confidence": 70,
            "expectedOutcome": "预期三个月内团队到位",
            "reviewAt": "2026-10-01T00:00:00Z",
            "keyQuestion": "预算撑得住吗？",
            "zhijunView": "我会先招一个核心",
            "userQuotes": ["我倾向自己招人", "编造的引用"],
        }
        fields, changed = deliberate.validate_draft(raw, user_texts=USER_TEXTS, prev_fields=None)
        self.assertEqual(fields["leaning"], "我倾向自己招人")
        self.assertIsNone(fields["choice"])  # 不在用户原话里 → 置空
        self.assertEqual(fields["rationale"], "因为控制力更强")
        self.assertEqual(fields["confidence"], 70)
        self.assertEqual(fields["expectedOutcome"], "预期三个月内团队到位")
        self.assertEqual(fields["reviewAt"], "2026-10-01T00:00:00Z")
        self.assertEqual(fields["userQuotes"], ["我倾向自己招人"])
        self.assertIn("options", changed)

    def test_model_confidence_without_user_number_is_dropped(self) -> None:
        raw = {"title": "t", "context": "c", "options": [], "leaning": None, "choice": None, "rationale": None, "confidence": 80, "expectedOutcome": None, "reviewAt": None, "keyQuestion": None, "zhijunView": None, "userQuotes": []}
        fields, _ = deliberate.validate_draft(raw, user_texts=["我在纠结要不要外包"], prev_fields=None)
        self.assertIsNone(fields["confidence"])

    def test_previous_fields_are_carried(self) -> None:
        prev = {**deliberate.default_fields(), "leaning": "我倾向自己招人", "options": ["外包", "招人"]}
        fields, changed = deliberate.validate_draft({"title": "", "context": "", "options": []}, user_texts=USER_TEXTS, prev_fields=prev)
        self.assertEqual(fields["leaning"], "我倾向自己招人")
        self.assertEqual(fields["options"], ["外包", "招人"])
        self.assertNotIn("leaning", changed)

    def test_fake_draft_splits_options_and_reads_confidence(self) -> None:
        raw = fake_draft(USER_TEXTS, "…【知君的看法】先招一个核心再说。")
        self.assertEqual(len(raw["options"]), 2)
        self.assertEqual(raw["confidence"], 70)
        self.assertIn("倾向", raw["leaning"])
        self.assertIn("因为", raw["rationale"])
        self.assertIn("预期", raw["expectedOutcome"])
        self.assertEqual(raw["zhijunView"], "先招一个核心再说。")

    def test_derived_prompts_strip_assistant_markers_but_preserve_user_literals(self) -> None:
        request = deliberate.build_draft_request(
            ["我把 [p0] 当作自己的原始代号。"],
            ["知君引用了旧依据 [p01][m0]，继续讨论。"],
            None,
        )
        prompt = request.messages[0]["content"]
        self.assertIn("用户1：我把 [p0] 当作自己的原始代号。", prompt)
        self.assertIn("知君最近一句：知君引用了旧依据，继续讨论。", prompt)
        self.assertNotIn("[p01]", request.debug["assistantText"])
        self.assertNotIn("[m0]", request.debug["assistantText"])

        rendered = context._render_history([
            {"role": "user", "content": "这是用户输入的 [p0]。"},
            {"role": "assistant", "content": "这是旧回复 [p01][m0]。"},
        ], 1000)
        self.assertEqual(rendered[0]["content"], "这是用户输入的 [p0]。")
        self.assertEqual(rendered[1]["content"], "这是旧回复。")

    def test_summary_prompt_only_strips_markers_from_assistant_messages(self) -> None:
        class SummaryProvider:
            name = "local-test"
            external = False

            def complete_json(self, request):
                self.request = request
                return {"summary": "测试摘要", "themes": [], "open_loops": []}

        provider = SummaryProvider()
        messages = [
            {"id": "user-1", "role": "user", "content": "我把 [p0] 当作自己的原始代号。", "meta": {}},
            {"id": "assistant-1", "role": "assistant", "content": "旧回答依据 [p01][m0]，继续讨论。", "meta": {}},
        ]
        with patch.object(jobs.provider_gate, "acquire", return_value=True), \
                patch.object(jobs.provider_gate, "release"):
            jobs._model_summary(messages, provider)
        prompt = provider.request.messages[0]["content"]
        self.assertIn("用户：我把 [p0] 当作自己的原始代号。", prompt)
        self.assertIn("知君：旧回答依据，继续讨论。", prompt)
        self.assertNotIn("[p01]", prompt)
        self.assertNotIn("[m0]", prompt)


class DeliberateApiTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _send(self, conversation_id: str, content: str, mode: str = "chat") -> list[tuple[str, dict]]:
        res = self.client.post(f"/api/mindos/conversations/{conversation_id}/messages", json={"content": content, "mode": mode})
        self.assertEqual(res.status_code, 200, res.text)
        return _parse_sse(res.text)

    def test_deliberate_turn_emits_draft_and_confirm_writes_decision(self) -> None:
        self.growth.create_charter(
            {"vision": "清醒", "roles": ["创业者"], "principles": ["先看数据"], "boundaries": [], "goals": [], "challengeStyle": "直接", "quietDomains": []}
        )
        conv = self.client.post("/api/mindos/conversations", json={}).json()
        events = self._send(conv["id"], USER_TEXTS[0], mode="deliberate")
        names = [n for n, _ in events]
        self.assertEqual(events[0][1]["turnMode"], "deliberate")
        self.assertIn("decision_draft", names)
        self.assertLess(names.index("decision_draft"), names.index("extraction"))
        draft = next(d for n, d in events if n == "decision_draft")
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["state"], "queued")
        jobs.drain(store=self.onto, conv_store=self.convs, max_jobs=30)
        draft = self.client.get(f"/api/mindos/conversations/{conv['id']}/decision-draft").json()
        self.assertEqual(len(draft["fields"]["options"]), 2)
        reply = "".join(d["t"] for n, d in events if n == "token")
        self.assertIn("你面前的选项", reply)
        self.assertIn("【知君的看法】", reply)

        events = self._send(conv["id"], USER_TEXTS[1], mode="deliberate")
        draft2 = next(d for n, d in events if n == "decision_draft")
        self.assertEqual(draft2["state"], "queued")
        jobs.drain(store=self.onto, conv_store=self.convs, max_jobs=30)
        draft2 = self.client.get(f"/api/mindos/conversations/{conv['id']}/decision-draft").json()
        self.assertEqual(draft2["id"], draft["id"])
        self.assertEqual(draft2["revision"], 2)
        self.assertEqual(draft2["fields"]["confidence"], 70)
        self.assertNotEqual(draft2["fields"]["confidence"], draft["fields"]["confidence"])
        got = self.client.get(f"/api/mindos/conversations/{conv['id']}/decision-draft").json()
        self.assertEqual(got["revision"], 2)

        incomplete = self.client.post(f"/api/mindos/conversations/{conv['id']}/decision-draft/confirm", json={})
        self.assertEqual(incomplete.status_code, 400)
        self.assertEqual(incomplete.json()["detail"]["code"], "DRAFT_INCOMPLETE")

        confirmed = self.client.post(
            f"/api/mindos/conversations/{conv['id']}/decision-draft/confirm",
            json={"choice": "自己招人", "expectedOutcome": "三个月内团队到位"},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        body = confirmed.json()
        self.assertEqual(body["draft"]["status"], "confirmed")
        decision = body["decision"]
        self.assertEqual(decision["choice"], "自己招人")
        self.assertEqual(decision["confidence"], 70)
        self.assertEqual(decision["charterVersion"], 1)
        self.assertEqual(decision["status"], "open")
        self.assertIn("自己招人", decision["options"])
        self.assertTrue(any("messageId" in ref for ref in decision["evidenceRefs"]))
        listed = self.client.get("/api/mindos/growth/decisions").json()["items"]
        self.assertEqual(listed[0]["id"], decision["id"])
        detail = self.client.get(f"/api/mindos/conversations/{conv['id']}").json()
        self.assertEqual(detail["decisionDraft"]["status"], "confirmed")
        self.assertEqual(detail["messages"][-1]["meta"]["kind"], "decision_confirmed")

        again = self.client.post(f"/api/mindos/conversations/{conv['id']}/decision-draft/confirm", json={"choice": "x"})
        self.assertEqual(again.status_code, 400)
        events = self._send(conv["id"], "还有一件事我在纠结要不要涨价", mode="deliberate")
        draft3 = next(d for n, d in events if n == "decision_draft")
        self.assertEqual(draft3["state"], "queued")
        jobs.drain(store=self.onto, conv_store=self.convs, max_jobs=30)
        draft3 = self.client.get(f"/api/mindos/conversations/{conv['id']}/decision-draft").json()
        self.assertNotEqual(draft3["id"], draft["id"])
        self.assertEqual(draft3["revision"], 1)

    def test_discard_and_missing_draft(self) -> None:
        conv = self.client.post("/api/mindos/conversations", json={}).json()
        self.assertEqual(self.client.get(f"/api/mindos/conversations/{conv['id']}/decision-draft").status_code, 404)
        self._send(conv["id"], USER_TEXTS[0], mode="deliberate")
        res = self.client.post(f"/api/mindos/conversations/{conv['id']}/decision-draft/discard")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "discarded")
        self.assertEqual(self.client.post(f"/api/mindos/conversations/{conv['id']}/decision-draft/discard").status_code, 409)
        bad = self.client.post(f"/api/mindos/conversations/{conv['id']}/messages", json={"content": "x" * 10, "mode": "weird"})
        self.assertEqual(bad.status_code, 422)


if __name__ == "__main__":
    unittest.main()
