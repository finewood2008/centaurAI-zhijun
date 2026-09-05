"""知君对话竖切（演示模型）：SSE 事件顺序、并发 409、通道不可用、抽取 → inbox → 确认 → 下一轮引用 → 撤回不回流。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import conversations, ontology, zhijun_status
from mindos.stores import conversation_store as conversation_store_module
from mindos.stores import ontology_store as ontology_store_module
from mindos.zhijun import jobs
from mindos.zhijun.gate import conversation_locks
from mindos.zhijun.provider import ProviderError
from mindos.zhijun.turn import TurnError, run_turn


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        name = None
        data_lines: list[str] = []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        events.append((name or "message", json.loads("\n".join(data_lines) or "{}")))
    return events


class TurnSseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.onto = ontology_store_module.reset_for_tests(root / "ontology.db")
        self.convs = conversation_store_module.reset_for_tests(root / "conversations.db")
        self._env = patch.dict(os.environ, {"ZHIJUN_PROVIDER": "fake", "ZHIJUN_MATERIAL_EVIDENCE": "0"})
        self._env.start()
        app = FastAPI()
        app.include_router(conversations.router)
        app.include_router(ontology.router)
        app.include_router(zhijun_status.router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _send(self, conversation_id: str, content: str, depth: str = "brief") -> list[tuple[str, dict]]:
        res = self.client.post(f"/api/mindos/conversations/{conversation_id}/messages", json={"content": content, "depth": depth})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.headers["content-type"].startswith("text/event-stream"))
        return _parse_sse(res.text)

    def test_status_reports_fake_provider(self) -> None:
        res = self.client.get("/api/mindos/zhijun/status")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["provider"], "fake")
        self.assertTrue(body["configured"])
        self.assertEqual(body["extraction"], "enabled")

    def test_full_loop_talk_remember_confirm_use_retract(self) -> None:
        conv = self.client.post("/api/mindos/conversations", json={"mode": "chat"}).json()
        events = self._send(conv["id"], "我在做远川项目，压力很大。我想明年把公司做到盈利。")
        names = [name for name, _ in events]
        self.assertEqual(names[:2], ["meta", "provenance"])
        self.assertIn("token", names)
        self.assertEqual(names[-2:], ["extraction", "message_done"])
        meta = events[0][1]
        self.assertEqual(meta["provider"], "fake")
        self.assertEqual(events[-2][1]["state"], "queued")
        done = events[-1][1]
        self.assertEqual(done["status"], "complete")

        detail = self.client.get(f"/api/mindos/conversations/{conv['id']}").json()
        roles = [m["role"] for m in detail["messages"]]
        self.assertEqual(roles, ["user", "assistant"])
        assistant = detail["messages"][1]
        self.assertEqual(assistant["id"], meta["messageId"])
        self.assertEqual(assistant["status"], "complete")
        self.assertEqual("".join(d["t"] for n, d in events if n == "token"), assistant["content"])
        receipt = self.client.get(f"/api/mindos/conversations/{conv['id']}/messages/{meta['messageId']}/receipt").json()
        self.assertEqual(receipt["provider"], "fake")
        self.assertEqual(receipt["confirmedClaimIds"], [])

        processed = jobs.drain(store=self.onto, conv_store=self.convs)
        self.assertGreaterEqual(processed, 1)
        stats = self.client.get("/api/mindos/ontology/stats").json()
        self.assertFalse(stats["hasOntology"])
        self.assertEqual(stats["claims"]["confirmed"], 0)
        self.assertEqual(stats["inbox"], 1)
        inbox = self.client.get("/api/mindos/ontology/inbox").json()["items"]
        told_id = next(c["id"] for c in inbox if c["layer"] == "self_declared")
        # One durable candidate per turn; the extra aspiration must not silently
        # become a second candidate or a confirmed profile entry.
        self.assertEqual(inbox[0]["trustState"], "working")
        accepted = self.client.post(f"/api/mindos/ontology/claims/{told_id}/review",
            json={"action": "confirm", "surface": "conversation", "conversationId": conv["id"], "messageId": meta["messageId"]})
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["claim"]["trustState"], "confirmed")
        confirmed = self.client.get("/api/mindos/ontology/claims", params={"trust": "confirmed"}).json()["items"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["trustState"], "confirmed")
        detail = self.client.get(f"/api/mindos/conversations/{conv['id']}").json()
        note = detail["messages"][-1]
        self.assertEqual(note["role"], "system")
        self.assertEqual(note["meta"]["kind"], "review")
        self.assertTrue(note["content"].startswith("你确认了："))

        events = self._send(conv["id"], "远川项目最近推进得怎么样")
        provenance = next(d for n, d in events if n == "provenance")
        self.assertIn(told_id, [c["id"] for c in provenance["confirmedClaims"]])
        reply = "".join(d["t"] for n, d in events if n == "token")
        self.assertIn("我记得你说过", reply)
        self.assertIn("【你告诉我的】我在做远川项目", reply)
        receipt = self.client.get(f"/api/mindos/conversations/{conv['id']}/messages/{events[0][1]['messageId']}/receipt").json()
        self.assertIn(told_id, receipt["confirmedClaimIds"])
        # 刷新后历史回复也带出处（由回执还原）
        history = self.client.get(f"/api/mindos/conversations/{conv['id']}").json()["messages"]
        last_reply = [m for m in history if m["role"] == "assistant"][-1]
        self.assertTrue(last_reply["provenance"]["fromReceipt"])
        self.assertIn(told_id, [c["id"] for c in last_reply["provenance"]["confirmedClaims"]])
        self.assertEqual(last_reply["provenance"]["confirmedClaims"][0]["content"], "我在做远川项目，压力很大")

        retract = self.client.post(f"/api/mindos/ontology/claims/{told_id}/review", json={"action": "retract", "surface": "ontology_page"})
        self.assertEqual(retract.status_code, 200)
        conflict = self.client.post(f"/api/mindos/ontology/claims/{told_id}/review", json={"action": "confirm", "surface": "ontology_page"})
        self.assertEqual(conflict.status_code, 409)

        jobs.drain(store=self.onto, conv_store=self.convs)
        events = self._send(conv["id"], "远川项目")
        provenance = next(d for n, d in events if n == "provenance")
        self.assertNotIn(told_id, [c["id"] for c in provenance["confirmedClaims"]])
        self.assertGreaterEqual(provenance["retractedNotices"], 1)
        reply = "".join(d["t"] for n, d in events if n == "token")
        self.assertNotIn("我在做远川项目", reply)

        projection = self.client.get("/api/mindos/ontology/projection").json()
        self.assertNotIn("我想明年把公司做到盈利", projection["markdown"])
        self.assertNotIn("我在做远川项目", projection["markdown"])

    def test_partial_review_returns_replacement(self) -> None:
        conversation = self.convs.create_conversation()
        message = self.convs.append_message(conversation["id"], "user", "x；y（合成的两段待核对原话）")
        claim = self.onto.create_claim(
            {"content": "我可能偏内向", "section": "who", "layer": "hypothesis", "confidence": 0.5},
            [{"kind": "conversation_turn", "conversation_id": conversation["id"], "message_id": message["id"], "quote": "x"}],
        )
        res = self.client.post(
            f"/api/mindos/ontology/claims/{claim['id']}/review",
            json={"action": "partial", "surface": "ontology_page", "editedContent": "我在陌生场合偏安静，熟了就不会"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["claim"]["trustState"], "superseded")
        self.assertEqual(body["replacedBy"]["trustState"], "confirmed")
        self.assertEqual(body["replacedBy"]["layer"], "self_declared")
        # 已被替代的理解不再接受任何转移 → 409；未替代但缺少修改内容 → 400。
        stale = self.client.post(f"/api/mindos/ontology/claims/{claim['id']}/review", json={"action": "partial", "surface": "ontology_page"})
        self.assertEqual(stale.status_code, 409)
        fresh = self.onto.create_claim(
            {"content": "我大概更信数据", "section": "ways", "layer": "hypothesis", "confidence": 0.5},
            [{"kind": "conversation_turn", "conversation_id": conversation["id"], "message_id": message["id"], "quote": "y"}],
        )
        bad = self.client.post(f"/api/mindos/ontology/claims/{fresh['id']}/review", json={"action": "partial", "surface": "ontology_page"})
        self.assertEqual(bad.status_code, 400)

    def test_user_created_claim_is_confirmed(self) -> None:
        res = self.client.post(
            "/api/mindos/ontology/claims",
            json={"content": "我坚持先看数据再拍板", "section": "principles", "layer": "self_declared", "exportAllowed": True},
        )
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["trustState"], "confirmed")
        self.assertEqual(body["trustOrigin"], "user_created")
        self.assertTrue(body["exportAllowed"])
        projection = self.client.get("/api/mindos/ontology/projection").json()
        self.assertIn("我坚持先看数据再拍板", projection["exportableMarkdown"])
        dup = self.client.post("/api/mindos/ontology/claims", json={"content": "我坚持先看数据再拍板", "section": "principles"})
        self.assertEqual(dup.status_code, 409)

    def test_onboarding_mode_asks_one_question_at_a_time(self) -> None:
        conv = self.client.post("/api/mindos/conversations", json={"mode": "onboarding"}).json()
        first = "".join(d["t"] for n, d in self._send(conv["id"], "你好，我们开始吧") if n == "token")
        self.assertIn("我该怎么称呼你", first)
        second = "".join(d["t"] for n, d in self._send(conv["id"], "叫我阿远就行，我是创业者也是父亲") if n == "token")
        self.assertIn("记下了", second)
        self.assertIn("最占心思的一件事", second)

    def test_turn_in_flight_returns_409(self) -> None:
        conv = self.client.post("/api/mindos/conversations", json={}).json()
        self.assertTrue(conversation_locks.acquire(conv["id"]))
        try:
            res = self.client.post(f"/api/mindos/conversations/{conv['id']}/messages", json={"content": "我在忙远川项目"})
        finally:
            conversation_locks.release(conv["id"])
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["detail"]["code"], "TURN_IN_FLIGHT")
        ok = self.client.post(f"/api/mindos/conversations/{conv['id']}/messages", json={"content": "我在忙远川项目"})
        self.assertEqual(ok.status_code, 200)

    def test_provider_unavailable_maps_to_http_status(self) -> None:
        conv = self.client.post("/api/mindos/conversations", json={}).json()
        with patch("mindos.zhijun.turn.build_provider", side_effect=ProviderError("坏了", status_code=503, code="PROVIDER_UNAVAILABLE")):
            res = self.client.post(f"/api/mindos/conversations/{conv['id']}/messages", json={"content": "我在忙远川项目"})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["detail"]["code"], "PROVIDER_UNAVAILABLE")
        missing = self.client.post("/api/mindos/conversations/conv_missing/messages", json={"content": "我在忙远川项目"})
        self.assertEqual(missing.status_code, 404)
        empty = self.client.post(f"/api/mindos/conversations/{conv['id']}/messages", json={"content": "   "})
        self.assertEqual(empty.status_code, 400)

    def test_client_abort_persists_partial_text(self) -> None:
        conv = self.convs.create_conversation()
        gen = run_turn(conv["id"], "我在做远川项目，我想把它做成", provider=None)
        name, meta = next(gen)
        self.assertEqual(name, "meta")
        next(gen)  # provenance
        name, token = next(gen)
        self.assertEqual(name, "token")
        gen.close()
        message = self.convs.get_message(meta["messageId"])
        self.assertEqual(message["status"], "aborted")
        self.assertTrue(message["content"].startswith(token["t"]))
        self.assertFalse(conversation_locks.in_flight(conv["id"]))
        with self.assertRaises(TurnError):
            next(run_turn(conv["id"], ""))

    def test_delete_conversation(self) -> None:
        conv = self.client.post("/api/mindos/conversations", json={}).json()
        self.assertEqual(self.client.delete(f"/api/mindos/conversations/{conv['id']}").json()["deleted"], True)
        self.assertEqual(self.client.get(f"/api/mindos/conversations/{conv['id']}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
