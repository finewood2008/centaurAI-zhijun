"""Real scoped memory ledger/API checks with disposable stores and providers."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import test_task_routing as harness
from mindos import conversations, memory_routes
from mindos.stores.memory_store import MemoryStore
from mindos.zhijun import extract, memory
from mindos.zhijun.routing import Router


class MemoryPolicyTests(unittest.TestCase):
    tearDown = harness.RoutingTests.tearDown
    enable = harness.RoutingTests.enable
    claim = harness.RoutingTests.claim
    preview = harness.RoutingTests.preview
    send = harness.RoutingTests.send
    grant = harness.RoutingTests.grant

    def setUp(self):
        harness.RoutingTests.setUp(self)
        app = FastAPI()
        # Test-only authenticated device context; production still uses its guard.
        @app.middleware("http")
        async def device_context(request, call_next):
            request.state.mindos_device_context = SimpleNamespace(device_id=request.headers.get("x-test-device"))
            return await call_next(request)
        app.include_router(conversations.router)
        app.include_router(memory_routes.build_router())
        self.client = TestClient(app)
        self.ledger = MemoryStore(self.onto)

    def api(self, suffix, body=None, cid=None, device=None):
        return self.client.post(f"/api/mindos/conversations/{cid or self.cid}/memory/{suffix}",
                                json=body, headers={"x-test-device": device} if device else {})

    def ingest(self, text, *, cid=None, content=None, section="matters", scope="context_only",
               predicate="happened", layer="self_declared", sources=None, input_origin=None):
        cid = cid or self.cid
        message = self.convs.append_message(cid, "user", text, meta={"routingSources": sources or []})
        value = extract.ValidatedClaim(section=section, layer=layer, predicate=predicate, subject="me", object=None,
            content=content or text, quote=content or text, confidence=.95, scope=scope, privacy_level="private",
            why_it_matters="帮助安排这次行动的时间与参与者，并在后续核对具体约束")
        result = memory.process_candidates([value], [], store=self.onto, conversation_id=cid,
            message_id=message["id"], user_text=text, routing_sources=sources, input_origin=input_origin)
        return result, message

    def attention(self, cid=None):
        response = self.api("attention", cid=cid)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def pending(self, cid=None, device=None):
        return self.client.get(f"/api/mindos/conversations/{cid or self.cid}/memory/pending",
                               headers={"x-test-device": device} if device else {})

    def test_same_topic_new_evidence_can_prompt_after_three_user_turns(self):
        first, _ = self.ingest("我在一家合成制造企业任总经理", scope="long_term", section="who", predicate="role")
        state = self.attention()
        self.api("dismiss", {"topicId": state["topicId"], "kind": "claim", "id": first["created"][0]})
        second, _ = self.ingest("我长期负责产品研发的团队建设", scope="long_term", predicate="working_on")
        self.assertIsNone(self.attention()["candidate"])
        self.convs.append_message(self.cid, "user", "好的，我想先继续聊这件事")
        self.assertIsNone(self.attention()["candidate"])
        self.convs.append_message(self.cid, "user", "我希望我们继续把约束说明白")
        renewed = self.attention()
        self.assertEqual(renewed["topicId"], state["topicId"])
        self.assertEqual(renewed["candidate"]["id"], second["created"][0])
        self.assertEqual(self.attention(), renewed, "refresh cannot reserve another card")
        self.assertEqual(MemoryStore(self.onto).slot(self.cid, state["topicId"])["target_id"], second["created"][0])
        self.api("dismiss", {"topicId": renewed["topicId"], "kind": "claim", "id": second["created"][0]})
        for _ in range(4):
            self.convs.append_message(self.cid, "user", "继续聊聊这个合成案例")
        self.assertIsNone(self.attention()["candidate"], "time and repeated polls cannot resurface old evidence")
        self.assertEqual(self.pending().json()["total"], 2)

    def test_old_backlog_is_available_manually_without_immediate_reprompt(self):
        first, _ = self.ingest("我是一家合成公司的总经理", scope="long_term", section="who", predicate="role")
        self.ingest("我长期认同诚信这条原则", scope="long_term", section="principles", predicate="holds_principle")
        state = self.attention()
        self.api("dismiss", {"topicId": state["topicId"], "kind": "claim", "id": first["created"][0]})
        for _ in range(4):
            self.convs.append_message(self.cid, "user", "继续讨论合成案例的条件")
        self.assertIsNone(self.attention()["candidate"])
        self.assertEqual(self.pending().json()["total"], 2)

    def test_old_attention_row_migrates_without_replaying_historical_reminder(self):
        result, _ = self.ingest("我是一名合成企业负责人", scope="long_term", section="who", predicate="role")
        topic = memory.topic_for(self.convs, self.cid)
        with self.onto._connect() as db:
            db.execute("DROP TABLE memory_attention")
            db.execute("CREATE TABLE memory_attention (conversation_id TEXT,topic_id TEXT,kind TEXT,target_id TEXT,consumed INTEGER,PRIMARY KEY(conversation_id,topic_id))")
            db.execute("INSERT INTO memory_attention VALUES(?,?,?,?,1)", (self.cid, topic, "claim", result["created"][0]))
        restored = MemoryStore(self.onto)
        self.assertIsNone(self.attention()["candidate"])
        self.assertEqual(restored.slot(self.cid, topic)["shown_user_turn"], 1)
        self.assertEqual(self.pending().json()["total"], 1)

    def test_concurrent_polling_reserves_only_one_new_card_and_stale_dismiss_cannot_replace_it(self):
        from concurrent.futures import ThreadPoolExecutor
        first, _ = self.ingest("我是一名合成企业负责人", scope="long_term", section="who", predicate="role")
        old = self.attention()
        body = {"topicId": old["topicId"], "kind": "claim", "id": first["created"][0]}
        self.api("dismiss", body)
        second, _ = self.ingest("我主要分管合成公司的产品研发", scope="long_term", predicate="working_on")
        for _ in range(2):
            self.convs.append_message(self.cid, "user", "继续聊这件事的合成条件")
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: memory.attention(self.onto, self.convs, self.cid), range(2)))
        self.assertTrue(all(v["candidate"]["id"] == second["created"][0] for v in values))
        self.assertEqual(self.api("dismiss", body).status_code, 409)
        self.assertEqual(self.attention()["candidate"]["id"], second["created"][0])

    def test_all_topic_pending_queue_is_read_only_and_device_scoped(self):
        first, _ = self.ingest("我是一名合成企业高管", scope="long_term", section="who", predicate="role")
        first_topic = memory.topic_for(self.convs, self.cid)
        self.convs.append_message(self.cid, "user", "换个话题，聊聊家庭")
        second, _ = self.ingest("我的女儿是合成案例里的小雨", scope="long_term", section="people", predicate="relationship")
        with patch("mindos.zhijun.routing.Router.provider") as provider:
            result = self.pending()
        provider.assert_not_called()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual({item["claim"]["id"] for item in result.json()["items"]}, {first["created"][0], second["created"][0]})
        self.assertIsNone(self.ledger.slot(self.cid, first_topic), "opening the list must not consume a prompt")
        self.assertIsNone(self.ledger.slot(self.cid, memory.topic_for(self.convs, self.cid)))
        self.assertEqual(self.pending(device="other-device").status_code, 404)
        other = self.convs.create_conversation()["id"]
        self.assertEqual(self.pending(cid=other).json(), {"items": [], "total": 0})
        self.onto.transition(first["created"][0], "confirm", surface="conversation", conversation_id=self.cid)
        self.assertEqual(self.pending().json()["total"], 1)
        self.assertEqual(self.attention()["pendingCount"], 1)

    def test_pending_queue_rechecks_source_lifecycle_and_never_confirms(self):
        first, message = self.ingest("我是一名合成企业高管", scope="long_term", section="who", predicate="role")
        candidate = self.pending().json()["items"][0]["claim"]
        self.assertEqual(candidate["trustState"], "working")
        self.assertIsNone(candidate["selfAlignment"]["level"])
        self.convs.update_message(message["id"], content="原先的描述已经修改")
        self.assertEqual(self.pending().json()["total"], 0)
        self.assertEqual(self.onto.get_claim(first["created"][0])["trustState"], "working")

    def test_pending_discard_does_not_need_slot_and_is_idempotent(self):
        first, message = self.ingest("我是一名合成企业高管", scope="long_term", section="who", predicate="role")
        body = {"claimId": first["created"][0]}
        self.assertEqual(self.api("pending-dismiss", body, device="other-device").status_code, 404)
        other = self.convs.create_conversation()["id"]
        self.assertEqual(self.api("pending-dismiss", body, cid=other).status_code, 409)
        result = self.api("pending-dismiss", body)
        self.assertEqual(result.status_code, 200, result.text)
        rejected = self.onto.get_claim(body["claimId"])
        self.assertEqual(rejected["trustState"], "retracted")
        self.assertEqual(self.api("pending-dismiss", body).status_code, 200)
        self.assertEqual(self.onto.get_claim(body["claimId"]), rejected)
        self.assertEqual(self.convs.get_message(message["id"])["content"], message["content"])
        self.assertEqual(self.pending().json()["total"], 0)

    def test_pending_discard_cannot_retract_confirmed_record(self):
        first, _ = self.ingest("我是一名合成企业高管", scope="long_term", section="who", predicate="role")
        self.onto.transition(first["created"][0], "confirm", surface="conversation", conversation_id=self.cid)
        self.assertEqual(self.api("pending-dismiss", {"claimId": first["created"][0]}).status_code, 409)
        self.assertEqual(self.onto.get_claim(first["created"][0])["trustState"], "confirmed")

    def test_guided_turn_four_does_not_erase_real_boundary_candidate(self):
        text = "我的底线是不向第三方出售客户资料"
        message = self.convs.append_message(self.cid, "user", text)
        raw = {"claims": [{"section": "principles", "layer": "self_declared", "predicate": "boundary",
            "content": text, "quote": text, "confidence": .95, "scope_hint": "long_term",
            "why_it_matters": "处理客户资料和商业合作时必须考虑这条用户边界"}], "entities": []}
        with patch.object(self.local, "complete_json", return_value=raw):
            result = extract.run_extraction(provider=self.local, store=self.onto, conversation_id=self.cid,
                message_id=message["id"], user_text=text, prev_assistant="你有哪些不可退让的边界？", debug={"onboardingStep": 4})
        self.assertEqual(len(result["created"]), 1)
        candidate = self.onto.get_claim(result["created"][0])
        self.assertEqual(candidate["section"], "principles")
        self.assertEqual(candidate["trustState"], "working")
        self.assertIsNone(candidate["selfAlignment"]["level"])

    def test_policy_persists_idempotently_with_conflicts_and_device_isolation(self):
        path = "/api/mindos/memory-policy"
        self.assertEqual(self.client.get(path).json(), {"mode": "important", "revision": 0})
        body = {"mode": "manual", "expectedRevision": 0}
        first = self.client.put(path, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.client.put(path, json=body).json(), first.json())
        self.assertEqual(MemoryStore(self.onto).policy("global"), {"mode": "manual", "revision": 1})
        self.assertEqual(self.client.put(path, json={"mode": "important", "expectedRevision": 0}).status_code, 409)
        self.assertEqual(self.client.get(path, headers={"x-test-device": "synthetic-device"}).json(), {"mode": "important", "revision": 0})
        other = self.client.put(path, json=body, headers={"x-test-device": "synthetic-device"})
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(self.ledger.policy("device:synthetic-device")["mode"], "manual")
        self.assertEqual(self.client.put(path, json={"mode": "important", "expectedRevision": 1}).json()["revision"], 2)
        self.assertEqual(self.ledger.policy("device:synthetic-device")["mode"], "manual")

    def test_manual_mode_only_explicit_memory_request_produces_working_candidate(self):
        self.ledger.set_policy("global", "manual", 0)
        ignored, _ = self.ingest("我长期负责合成项目研发", scope="long_term", predicate="working_on")
        self.assertEqual(ignored["created"], [])
        self.assertIsNone(self.attention()["draft"])
        content = "我明天去合成活动看作品"
        explicit, _ = self.ingest("请记住：" + content, content=content)
        self.assertEqual(len(explicit["created"]), 1)
        claim = self.onto.get_claim(explicit["created"][0])
        self.assertEqual(claim["trustState"], "working")
        self.assertIsNone(claim["selfAlignment"]["level"])
        self.assertEqual(self.attention()["candidate"]["id"], claim["id"])

    def test_context_fragments_merge_into_one_durable_draft_without_claims(self):
        first, m1 = self.ingest("我明天去合成活动了解参与者的背景")
        second, _ = self.ingest("我这次先看看他们以前做过的作品")
        self.assertEqual(first["draftId"], second["draftId"])
        self.assertEqual(self.onto.list_claims(trust_states=("working", "confirmed")), [])
        state = self.attention()
        self.assertIsNone(state["candidate"])
        self.assertEqual(state["draft"]["revision"], 2)
        self.assertEqual(len(state["draft"]["entries"]), 2)
        restored = MemoryStore(self.onto).draft(self.cid, memory.topic_for(self.convs, self.cid))
        self.assertEqual(restored["id"], first["draftId"])
        self.assertEqual(restored["entries"][0]["messageId"], m1["id"])
        self.assertNotIn("sources", state["draft"]["entries"][0], "preview exposes original text, not hidden source internals")

    def test_reprocessing_same_context_message_does_not_duplicate_or_increment(self):
        _, message = self.ingest("我明天先去合成活动看看作品")
        draft = self.attention()["draft"]
        candidate = extract.ValidatedClaim(section="matters", layer="self_declared", predicate="happened", subject="me", object=None,
            content=message["content"], quote=message["content"], confidence=.8, scope="context_only", privacy_level="private",
            why_it_matters="安排这次活动到场顺序时需要知道要先看作品")
        memory.process_candidates([candidate], [], store=self.onto, conversation_id=self.cid,
            message_id=message["id"], user_text=message["content"])
        self.assertEqual(self.attention()["draft"], draft)

    def test_attention_is_one_slot_per_current_topic_and_conversation(self):
        a, _ = self.ingest("我是一名合成社区教师", scope="long_term", section="who", predicate="role")
        b, _ = self.ingest("我长期认同尊重当事人的选择", scope="long_term", section="principles", predicate="holds_principle")
        selected = self.attention()
        self.assertEqual(selected["candidate"]["id"], a["created"][0])
        self.assertEqual(selected["pendingCount"], 2)
        consumed = self.api("dismiss", {"topicId": selected["topicId"], "kind": "claim", "id": selected["candidate"]["id"]})
        self.assertEqual(consumed.status_code, 200, consumed.text)
        self.assertIsNone(self.attention()["candidate"], "second pending item must not immediately replace a dismissed card")
        self.assertEqual(self.onto.get_claim(b["created"][0])["trustState"], "working")
        other = self.convs.create_conversation()["id"]
        self.assertIsNone(self.attention(other)["candidate"])
        self.convs.append_message(self.cid, "user", "换个话题，我们来谈谈家庭")
        new, _ = self.ingest("我的女儿是合成案例中的家人", scope="long_term", section="people", predicate="relationship")
        next_topic = self.attention()
        self.assertNotEqual(next_topic["topicId"], selected["topicId"])
        self.assertEqual(next_topic["candidate"]["id"], new["created"][0])

    def test_attention_cannot_be_accessed_or_dismissed_by_another_device(self):
        self.ingest("我是一名合成社区教师", scope="long_term", section="who", predicate="role")
        state = self.attention()
        self.assertEqual(self.api("attention", device="other-device").status_code, 404)
        body = {"topicId": state["topicId"], "kind": "claim", "id": state["candidate"]["id"]}
        self.assertEqual(self.api("dismiss", body, device="other-device").status_code, 404)
        self.assertIsNotNone(self.attention()["candidate"])

    def test_draft_save_checks_revision_and_retry_does_not_create_another_claim(self):
        self.ingest("我明天去合成活动了解背景")
        old = self.attention()["draft"]
        self.ingest("我这次还要比较候选人的作品")
        current = self.attention()["draft"]
        body = {"draftId": old["id"], "expectedRevision": old["revision"], "action": "save"}
        self.assertEqual(self.api("draft-review", body).status_code, 409)
        self.assertEqual(self.onto.list_claims(trust_states=("working", "confirmed")), [])
        body["expectedRevision"] = current["revision"]
        saved = self.api("draft-review", body)
        self.assertEqual(saved.status_code, 200, saved.text)
        claim = saved.json()["claim"]
        self.assertEqual(claim["content"], current["savedContent"])
        self.assertEqual(claim["scope"], "context_only")
        self.assertEqual(claim["trustState"], "confirmed")
        self.assertIsNone(claim["selfAlignment"]["level"])
        repeated = self.api("draft-review", body)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["claim"]["id"], claim["id"])
        self.assertEqual(len(self.onto.list_claims(trust_states=("working", "confirmed"))), 1)

    def test_changed_source_or_other_conversation_cannot_publish_draft(self):
        _, message = self.ingest("我明天去合成活动先了解背景")
        draft = self.attention()["draft"]
        body = {"draftId": draft["id"], "expectedRevision": draft["revision"], "action": "save"}
        other = self.convs.create_conversation()["id"]
        self.assertEqual(self.api("draft-review", body, cid=other).status_code, 400)
        self.convs.update_message(message["id"], content="我已改了原先的说法")
        self.assertEqual(self.api("draft-review", body).status_code, 409)
        self.assertEqual(self.onto.list_claims(trust_states=("working", "confirmed")), [])

    def test_dismissing_draft_preserves_confirmed_records_and_does_not_resume(self):
        confirmed = self.claim()
        self.ingest("我明天先去合成活动看看作品")
        draft = self.attention()["draft"]
        body = {"draftId": draft["id"], "expectedRevision": draft["revision"], "action": "dismiss"}
        dismissed = self.api("draft-review", body)
        self.assertEqual(dismissed.status_code, 200, dismissed.text)
        self.assertEqual(self.api("draft-review", body).status_code, 200)
        self.assertEqual(self.onto.get_claim(confirmed["id"])["trustState"], "confirmed")
        self.ingest("我这次也会问问参与者的时间安排")
        self.assertEqual(self.attention()["draft"]["status"], "dismissed")
        self.assertEqual(self.attention()["draft"]["revision"], draft["revision"] + 1)

    def test_saved_event_keeps_sources_and_cannot_be_sent_online_without_grants(self):
        secret = self.claim()
        ref = Router(self.onto, self.convs, self.cid).resolve({"kind": "claim", "id": secret["id"]})[0]["ref"]
        text = "我明天去星桥活动核对合成项目的作品"
        self.ingest(text, sources=[ref])
        draft = self.attention()["draft"]
        response = self.api("draft-review", {"draftId": draft["id"], "expectedRevision": draft["revision"], "action": "save"})
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()["claim"]
        self.assertEqual(saved["evidence"][0]["locator"]["routingSources"], [ref])
        self.enable(fresh=True)
        body, preview = self.preview("星桥活动核对合成项目作品的安排是什么？")
        self.assertTrue(preview["missing"])
        self.assertTrue(any(source["kind"] == "claim" and source["id"] == saved["id"] for source in preview["sources"]))
        self.assertEqual(self.send(body, preview).status_code, 409)
        self.assertEqual(self.online.requests, [], "saving a source-linked event is not an external-model grant")

    def test_discard_retry_does_not_duplicate_rejection_or_remove_original_message(self):
        admitted, message = self.ingest("我是一名合成社区教师", scope="long_term", section="who", predicate="role")
        current = self.attention()
        body = {"topicId": current["topicId"], "kind": "claim", "id": admitted["created"][0], "discard": True}
        first = self.api("dismiss", body)
        self.assertEqual(first.status_code, 200, first.text)
        rejected = self.onto.get_claim(body["id"])
        self.assertEqual(rejected["trustState"], "retracted")
        self.assertEqual(self.api("dismiss", body).status_code, 200)
        self.assertEqual(self.onto.get_claim(body["id"]), rejected)
        self.assertEqual(self.convs.get_message(message["id"])["content"], message["content"])
        self.assertIsNone(self.attention()["candidate"])

    def test_discard_stale_card_does_not_retract_newly_confirmed_claim(self):
        admitted, _ = self.ingest("我是一名合成社区教师", scope="long_term", section="who", predicate="role")
        current = self.attention()
        claim_id = admitted["created"][0]
        self.onto.transition(claim_id, "confirm", surface="conversation", conversation_id=self.cid)
        body = {"topicId": current["topicId"], "kind": "claim", "id": claim_id, "discard": True}
        response = self.api("dismiss", body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.onto.get_claim(claim_id)["trustState"], "confirmed")

    def saved_protected_event(self):
        secret = self.claim()
        ref = Router(self.onto, self.convs, self.cid).resolve({"kind": "claim", "id": secret["id"]})[0]["ref"]
        self.ingest("我明天去星桥活动核对合成项目的作品", sources=[ref])
        draft = self.attention()["draft"]
        saved = self.api("draft-review", {"draftId": draft["id"], "expectedRevision": draft["revision"], "action": "save"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.enable(fresh=True)
        body, preview = self.preview("星桥活动核对合成项目作品的安排是什么？")
        self.grant(preview)
        body, preview = self.preview(body["content"])
        self.assertFalse(preview["missing"])
        return secret, body, preview

    def test_saved_event_revocation_before_provider_call_blocks_actual_payload(self):
        _, body, preview = self.saved_protected_event()
        self.store.revoke("global")
        self.assertEqual(self.send(body, preview).status_code, 409)
        self.assertEqual(self.online.requests, [])

    def test_saved_event_changed_parent_version_invalidates_old_preview(self):
        secret, body, preview = self.saved_protected_event()
        self.onto.add_evidence(secret["id"], [{"kind": "user_edit", "quote": "新的合成来源修订"}])
        self.assertEqual(self.send(body, preview).status_code, 409)
        self.assertEqual(self.online.requests, [])

    def test_saved_event_changed_service_cannot_reuse_previous_grants(self):
        _, body, preview = self.saved_protected_event()
        self.online._base_url = "https://other-synthetic.invalid/v1"
        self.enable()
        self.assertEqual(self.send(body, preview).status_code, 409)
        self.assertEqual(self.online.requests, [])
        self.assertTrue(self.preview(body["content"])[1]["missing"])

    def test_delete_conversation_clears_local_drafts_but_keeps_saved_event(self):
        self.ingest("我明天去合成活动看看背景")
        first = self.attention()["draft"]
        saved = self.api("draft-review", {"draftId": first["id"], "expectedRevision": first["revision"], "action": "save"})
        self.assertEqual(saved.status_code, 200, saved.text)
        claim_id = saved.json()["claim"]["id"]
        self.convs.append_message(self.cid, "user", "换个话题，我们来安排下周出行")
        self.ingest("我下周先去合成地点确认交通")
        second = self.attention()["draft"]
        self.assertNotEqual(first["id"], second["id"])
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        for draft in (first, second):
            self.assertIsNone(MemoryStore(self.onto).draft(self.cid, draft_id=draft["id"]))
        self.assertEqual(self.onto.get_claim(claim_id)["trustState"], "confirmed")
        self.assertEqual(self.api("attention").status_code, 404)

    def test_bounded_outline_keeps_event_anchor_and_does_not_reopen_saved_draft(self):
        texts = ["我明天去合成活动寻找合适的合作伙伴"] + [f"我这次第{i}步核对合成作品的细节" for i in range(1, 11)]
        for text in texts:
            self.ingest(text)
        draft = self.attention()["draft"]
        self.assertEqual(len(draft["entries"]), 8)
        self.assertEqual(draft["entries"][0]["content"], texts[0])
        self.assertEqual(draft["summary"], "；".join([texts[0], *texts[-2:]]))
        body = {"draftId": draft["id"], "expectedRevision": draft["revision"], "action": "save"}
        saved = self.api("draft-review", body)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.ingest("我这次最后再核对一下活动地点")
        unchanged = self.attention()["draft"]
        self.assertEqual(unchanged["status"], "saved")
        self.assertEqual(unchanged["revision"], draft["revision"] + 1)
        self.assertEqual(unchanged["summary"], draft["summary"])


if __name__ == "__main__":
    unittest.main()
