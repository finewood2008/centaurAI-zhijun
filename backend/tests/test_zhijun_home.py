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

from mindos import ontology, zhijun_home
from mindos.stores import conversation_store as conversation_store_module
from mindos.stores import growth_store as growth_store_module
from mindos.stores import ontology_store as ontology_store_module


class ZhijunHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.onto = ontology_store_module.reset_for_tests(root / "ontology.db")
        self.convs = conversation_store_module.reset_for_tests(root / "conversations.db")
        self.growth = growth_store_module.reset_for_tests(root / "growth.db")
        self.now = datetime.now(timezone.utc)
        self._env = patch.dict(os.environ, {"ZHIJUN_PROVIDER": "fake"})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _claim(self, content: str, *, trust: str = "confirmed", privacy: str = "private", predicate: str = "holds_principle", valid_to: str | None = None, section: str = "principles") -> dict:
        return self.onto.create_claim(
            {"content": content, "section": section, "layer": "self_declared" if trust == "confirmed" else "hypothesis", "predicate": predicate, "privacy_level": privacy, "valid_to": valid_to},
            [{"kind": "user_edit", "quote": content}],
            trust_state=trust,
            trust_origin="user_created" if trust == "confirmed" else "model",
        )

    def _decision(self, title: str, *, review_at: datetime | None = None) -> dict:
        return self.growth.create_decision({
            "title": title,
            "context": "测试上下文",
            "options": ["做", "不做"],
            "choice": "先做小范围验证",
            "rationale": "降低风险",
            "confidence": 70,
            "expectedOutcome": "一周后看到结果",
            "reviewAt": review_at.isoformat() if review_at else None,
            "relatedEntityIds": [],
            "evidenceRefs": [],
        })

    def _home(self, *, enqueue: bool = False) -> dict:
        return zhijun_home.build_home_overview(now=self.now, enqueue=enqueue, ontology=self.onto, conversations=self.convs, growth=self.growth)

    def test_first_meet_and_building_states_do_not_queue_model(self) -> None:
        first = self._home(enqueue=True)
        self.assertEqual(first["state"], "first_meet")
        self.assertEqual(first["map"]["nodes"], [])
        self.assertEqual(first["nextAction"]["kind"], "onboarding")
        self.assertEqual(self.onto.pending_jobs(), 0)

        conv = self.convs.create_conversation(mode="onboarding")
        self.convs.append_message(conv["id"], "user", "开始认识我")
        lit = self._claim("重要决定要先小范围验证")
        building = self._home(enqueue=True)
        self.assertEqual(building["state"], "building")
        self.assertIn(f"claim:{lit['id']}", {item["id"] for item in building["map"]["nodes"]})
        self.assertEqual(building["nextAction"]["kind"], "resume_onboarding")
        self.assertEqual(building["nextAction"]["targetId"], conv["id"])
        self.assertEqual(self.onto.pending_jobs(), 0)

    def test_established_map_limits_priority_timeline_and_action(self) -> None:
        for index in range(6):
            self._claim(f"我确认过的原则 {index}")
        for index in range(5):
            self._claim(f"知君还不确定的理解 {index}", trust="working")
        due = self._decision("已经到期的判断", review_at=self.now - timedelta(days=1))
        self._decision("未来再看的判断", review_at=self.now + timedelta(days=4))
        overview = self._home()

        self.assertEqual(overview["state"], "established")
        rings = [item["ring"] for item in overview["map"]["nodes"]]
        self.assertEqual(rings.count("remembered"), 4)
        self.assertLessEqual(rings.count("tracking"), 3)
        self.assertEqual(rings.count("uncertain"), 3)
        self.assertEqual(overview["nextAction"]["kind"], "review")
        self.assertEqual(overview["nextAction"]["targetId"], due["id"])
        self.assertLessEqual(len(overview["timeline"]), 6)
        self.assertTrue(all(ref["id"] in {node["id"] for node in overview["map"]["nodes"]} for ref in overview["brief"]["sourceRefs"]))
        self.assertTrue({ref["label"] for ref in overview["brief"]["sourceRefs"]} <= {"你确认过", "我的推测", "判断簿"})

    def test_cache_is_reused_and_active_job_is_idempotent(self) -> None:
        self._claim("做重要决定前先验证真实需求")
        queued = self._home(enqueue=True)
        self.assertEqual(queued["brief"]["status"], "refreshing")
        self.assertEqual(self.onto.pending_jobs(), 1)
        self._home(enqueue=True)
        self.assertEqual(self.onto.pending_jobs(), 1)

        generated = zhijun_home.generate_home_brief(queued["sourceHash"], store=self.onto, conv_store=self.convs)
        self.assertEqual(generated["generatedBy"], "template")
        ready = self._home(enqueue=True)
        self.assertEqual(ready["brief"]["status"], "ready")
        self.assertEqual(ready["brief"]["generatedBy"], "template")

        old_hash = ready["sourceHash"]
        self.onto.transition(ready["map"]["nodes"][0]["claim"]["id"], "reaffirm", surface="today")
        changed = self._home(enqueue=False)
        self.assertNotEqual(changed["sourceHash"], old_hash)
        self.assertEqual(changed["brief"]["status"], "refreshing")

    def test_next_action_priority_order(self) -> None:
        due_decision = self._decision("到期判断", review_at=self.now - timedelta(days=1))
        outcome_decision = self._decision("已有结果", review_at=self.now + timedelta(days=1))
        self.growth.record_outcome(outcome_decision["id"], {"result": "完成", "notes": "", "evidenceRefs": []})
        commitment = self._claim("今天完成说明稿", predicate="committed_to", valid_to=(self.now - timedelta(hours=1)).isoformat(), section="matters")
        uncertain = self._claim("我可能更适合上午处理难题", trust="working")
        nudge = self.convs.create_nudge(kind="checkin", trigger_key="home-test", trigger_ref={}, why_now="今天约好看看", message="聊聊今天的变化", scheduled_for=self.now.isoformat(), now=self.now.isoformat())

        due_node = zhijun_home._decision_node(due_decision, self.now)
        outcome_node = zhijun_home._decision_node(self.growth.get_decision(outcome_decision["id"]), self.now)
        commitment_node = zhijun_home._claim_node(commitment, "tracking")
        uncertain_node = zhijun_home._claim_node(uncertain, "uncertain")
        action = lambda tracking, pending: zhijun_home._next_action("established", None, tracking, pending, self.convs, self.now)

        self.assertEqual(action([outcome_node, commitment_node, due_node], [uncertain_node])["kind"], "review")
        self.assertEqual(action([outcome_node, commitment_node], [uncertain_node])["kind"], "reflect")
        self.assertEqual(action([commitment_node], [uncertain_node])["kind"], "commitment")
        self.assertEqual(action([], [uncertain_node])["kind"], "confirm")
        self.assertEqual(action([], [])["kind"], "nudge")
        self.convs.set_nudge_status(nudge["id"], "dismissed")
        self.assertEqual(action([], [])["kind"], "chat")

    def test_external_generation_excludes_sensitive_claims(self) -> None:
        private = self._claim("我重视长期主义", privacy="private")
        sensitive = self._claim("这是一条敏感的个人信息", privacy="sensitive")
        overview = self._home()
        private_id = f"claim:{private['id']}"

        captured: dict = {}

        class Provider:
            name = "stub"
            external = True

            def complete_json(self, request):
                captured["payload"] = request.messages[0]["content"]
                return {"headline": "我还记得你看重长期的选择", "message": "这条原则仍然在我们的共同地图里。", "focusIds": [private_id]}

        with patch("mindos.zhijun.provider.build_provider", return_value=Provider()), patch("mindos.zhijun.gate.provider_gate.acquire", return_value=True), patch("mindos.zhijun.gate.provider_gate.release"):
            result = zhijun_home.generate_home_brief(overview["sourceHash"], store=self.onto, conv_store=self.convs)
        self.assertEqual(result["generatedBy"], "stub")
        self.assertIn(private["content"], captured["payload"])
        self.assertNotIn(sensitive["content"], captured["payload"])

    def test_external_decision_prompt_only_contains_safe_summary(self) -> None:
        self._claim("我会先做小范围验证")
        decision = self._decision("是否扩大小范围试点", review_at=self.now + timedelta(days=3))
        overview = self._home()
        decision_id = f"decision:{decision['id']}"
        captured: dict = {}

        class Provider:
            name = "stub"
            external = True

            def complete_json(self, request):
                captured["payload"] = request.messages[0]["content"]
                return {"headline": "我们在等一次验证", "message": "这个选择还在等待真实结果。", "focusIds": [decision_id]}

        with patch("mindos.zhijun.provider.build_provider", return_value=Provider()), patch("mindos.zhijun.gate.provider_gate.acquire", return_value=True), patch("mindos.zhijun.gate.provider_gate.release"):
            zhijun_home.generate_home_brief(overview["sourceHash"], store=self.onto, conv_store=self.convs)
        payload = json.loads(captured["payload"])
        sent = next(item for item in payload if item["id"] == decision_id)
        self.assertEqual(set(sent), {"id", "relation", "text", "choice", "status", "reviewAt"})
        self.assertNotIn("测试上下文", captured["payload"])
        self.assertNotIn("降低风险", captured["payload"])
        self.assertNotIn("一周后看到结果", captured["payload"])

    def test_invalid_model_source_ids_fall_back_to_template(self) -> None:
        self._claim("我会先做小范围验证")
        overview = self._home()

        class Provider:
            name = "stub"
            external = False

            def complete_json(self, request):
                return {"headline": "一段来信", "message": "没有合法依据。", "focusIds": ["claim:not-found"]}

        with patch("mindos.zhijun.provider.build_provider", return_value=Provider()), patch("mindos.zhijun.gate.provider_gate.acquire", return_value=True), patch("mindos.zhijun.gate.provider_gate.release"):
            result = zhijun_home.generate_home_brief(overview["sourceHash"], store=self.onto, conv_store=self.convs)
        self.assertEqual(result["generatedBy"], "template")

    def test_today_is_a_valid_review_surface(self) -> None:
        claim = self._claim("我可能更适合长期项目", trust="working")
        app = FastAPI()
        app.include_router(ontology.router)
        response = TestClient(app).post(f"/api/mindos/ontology/claims/{claim['id']}/review", json={"action": "confirm", "surface": "today"})
        self.assertEqual(response.status_code, 200, response.text)
        events = self.onto.review_events(claim["id"])
        self.assertEqual(events[0]["surface"], "today")


if __name__ == "__main__":
    unittest.main()
