"""Synthetic payload assertions at the provider boundary; no external network."""

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mindos import conversations
from mindos.stores import conversation_store, ontology_store, growth_store
from mindos.stores.alignment_store import AlignmentStore
from mindos.stores.routing_store import RoutingStore
from mindos.zhijun.provider import (
    ChatRequest,
    ProviderError,
    TextDelta,
    Done,
    Usage,
    _open,
)
from mindos.zhijun.routing import Router, GuardedProvider, service_info


class Recording:
    def __init__(self, external=True, host="https://synthetic.invalid/v1"):
        self.external, self.name, self.model, self._base_url = (
            external,
            "openai" if external else "ollama",
            "synthetic",
            host,
        )
        self.requests = []
        self.error = None
        self.result = {"summary": "合成摘要", "themes": [], "open_loops": []}

    def stream(self, req):
        self.requests.append(req)
        if self.error:
            raise self.error
        yield TextDelta("合成回复：先澄清约束，再比较选择，不把愿望当事实。")
        yield Usage(12, 8)
        yield Done("stop")

    def complete_json(self, req):
        self.requests.append(req)
        if self.error:
            raise self.error
        return self.result


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        root = Path(self.tmp.name)
        self.onto = ontology_store.reset_for_tests(root / "onto.db")
        self.convs = conversation_store.reset_for_tests(root / "convs.db")
        growth_store.reset_for_tests(root / "growth.db")
        self.cid = self.convs.create_conversation()["id"]
        self.online, self.local = Recording(), Recording(False)
        for target in (
            "mindos.zhijun.routing.build_provider",
            "mindos.routing_routes.build_provider",
        ):
            self.stack.enter_context(patch(target, return_value=self.online))
        self.stack.enter_context(
            patch("mindos.zhijun.routing.local_provider", return_value=self.local)
        )
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "ZHIJUN_PROVIDER": "",
                    "ZHIJUN_EXTRACTION": "0",
                    "ZHIJUN_MATERIAL_EVIDENCE": "0",
                },
            )
        )
        app = FastAPI()
        app.include_router(conversations.router)
        self.client = TestClient(app)
        self.url = f"/api/mindos/conversations/{self.cid}"
        self.store = RoutingStore(self.onto)

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def enable(self, fresh=False):
        response = self.client.put(
            self.url + "/routing",
            json={
                "mode": "online",
                "acknowledge": True,
                "serviceId": service_info(self.online)["id"],
                "expectedRevision": self.store.mode(self.cid)["revision"],
                "freshContext": fresh,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def claim(self):
        return self.onto.create_claim(
            {
                "subject_entity_id": "ent_me",
                "section": "matters",
                "layer": "self_declared",
                "predicate": "working_on",
                "content": "合成案例：星桥项目是被安排的工作，不代表我的个人追求",
                "confidence": 0.99,
            },
            [{"kind": "user_edit", "quote": "星桥项目只是工作安排"}],
            trust_state="confirmed",
            trust_origin="user_created",
        )

    def preview(self, content="合成案例：周末想安排一次散步", **over):
        body = {"content": content, **over}
        response = self.client.post(self.url + "/routing/preview", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return body, response.json()

    def grant(self, preview):
        response = self.client.post(
            self.url + "/routing/grant",
            json={"revision": preview["revision"], "keys": preview["missing"]},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def send(self, body, preview, **over):
        return self.client.post(
            self.url + "/messages",
            json={**body, "routeRevision": preview["revision"], **over},
        )

    def test_explicit_opt_in_and_legacy_remain_local(self):
        body, p = self.preview()
        self.assertFalse(p["service"]["external"])
        self.assertEqual(self.send(body, p).status_code, 200)
        self.assertEqual(len(self.online.requests), 0)
        self.assertEqual(len(self.local.requests), 1)
        r = self.client.put(
            self.url + "/routing", json={"mode": "online", "expectedRevision": 0}
        )
        self.assertEqual(r.status_code, 409)
        self.enable(True)

    def test_clean_window_does_not_declassify_opaque_history(self):
        old = self.convs.append_message(
            self.cid, "assistant", "SECRET_OLD_PROFILE", meta={"localOnlyDerived": True}
        )
        AlignmentStore(self.onto).status(self.cid, local_only=True, status="paused")
        self.enable(True)
        body, p = self.preview()
        self.assertTrue(p["service"]["external"])
        self.assertFalse(p["missing"])
        self.assertIn(old["id"], [x["id"] for x in p["excluded"]])
        response = self.send(body, p, requestId="synthetic-clean-1")
        self.assertEqual(response.status_code, 200, response.text)
        payload = (
            json.dumps(self.online.requests[0].messages)
            + self.online.requests[0].system
        )
        self.assertNotIn("SECRET_OLD_PROFILE", payload)
        self.assertTrue(AlignmentStore(self.onto).status(self.cid)["local_only"])
        self.assertTrue(self.convs.get_message(old["id"]))

    def test_profile_requires_purpose_version_and_service_grant(self):
        c = self.claim()
        self.enable()
        body, p = self.preview("星桥项目为什么迟迟不想推进？")
        self.assertIn("claim:" + c["id"], p["missing"])
        self.assertEqual(self.send(body, p).status_code, 409)
        self.assertEqual(self.online.requests, [])
        self.grant(p)
        body, current = self.preview(body["content"])
        self.assertFalse(current["missing"])
        self.assertEqual(self.send(body, current).status_code, 200)
        self.assertIn(c["content"], self.online.requests[-1].system)
        router = Router(self.onto, self.convs, self.cid)
        req = ChatRequest(
            system="合成校准", messages=[{"role": "user", "content": c["content"]}]
        )
        p2 = router.prepare(
            "alignment", req, [router.ref("claim", c["id"])], self.online
        )
        self.assertTrue(p2["missing"], "chat permission must not grant calibration")
        self.online._base_url = "https://different.invalid/v1"
        self.assertEqual(
            self.client.post(self.url + "/routing/preview", json=body).status_code, 409
        )

    def test_revoke_between_preview_and_actual_send_blocks(self):
        self.claim()
        self.enable()
        body, p = self.preview("星桥项目工作安排")
        self.grant(p)
        body, p = self.preview(body["content"])
        self.store.revoke("global")
        self.assertEqual(self.send(body, p).status_code, 409)
        self.assertFalse(self.online.requests)

    def test_same_service_model_choice_keeps_mode_and_existing_grants(self):
        c = self.claim()
        self.enable()
        body, before = self.preview("星桥项目工作安排")
        self.grant(before)
        mode = self.store.mode(self.cid)
        self.online.model = "new-selected-model"
        body, after = self.preview(body["content"])
        self.assertEqual(after["service"]["model"], "new-selected-model")
        self.assertEqual(after["service"]["id"], before["service"]["id"])
        self.assertEqual(self.store.mode(self.cid), mode)
        self.assertFalse(after["missing"])
        self.assertEqual(self.send(body, after).status_code, 200)
        self.assertIn(c["content"], self.online.requests[-1].system)

    def test_queued_old_model_is_not_dispatched_after_default_changes(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        request = ChatRequest(system="合成测试", messages=[{"role": "user", "content": "你好"}])
        old = Recording()
        preview = router.prepare("chat", request, [], old)
        guarded = GuardedProvider(router, old, "chat", [], revision=preview["revision"])
        self.online.model = "new-selected-model"
        with self.assertRaises(HTTPException) as raised:
            list(guarded.stream(request))
        self.assertEqual(raised.exception.detail["code"], "ROUTE_CHANGED")
        self.assertFalse(old.requests)

    def test_selected_external_model_never_overrides_local_conversation(self):
        self.store.set_mode(self.cid, "local", "")
        self.online.model = "new-selected-model"
        body, preview = self.preview()
        self.assertFalse(preview["service"]["external"])
        self.assertEqual(self.send(body, preview).status_code, 200)
        self.assertFalse(self.online.requests)
        self.assertEqual(len(self.local.requests), 1)

    def test_same_endpoint_model_account_change_blocks_queued_old_credentials(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        request = ChatRequest(system="合成测试", messages=[{"role": "user", "content": "你好"}])
        old = Recording()
        old.configuration_revision = ("account-a", "old-secret-ref")
        self.online.configuration_revision = ("account-a", "new-secret-ref")
        preview = router.prepare("chat", request, [], old)
        guarded = GuardedProvider(router, old, "chat", [], revision=preview["revision"])
        with self.assertRaises(HTTPException) as raised:
            list(guarded.stream(request))
        self.assertEqual(raised.exception.detail["code"], "ROUTE_CHANGED")
        self.assertFalse(old.requests)

    def test_source_changed_between_preview_and_network_blocks(self):
        c = self.claim()
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        req = ChatRequest(
            system="test", messages=[{"role": "user", "content": c["content"]}]
        )
        refs = [router.ref("claim", c["id"])]
        p = router.prepare("chat", req, refs, self.online)
        router.authorize(p, p["missing"])
        p = router.prepare("chat", req, refs, self.online)
        guarded = GuardedProvider(
            router, self.online, "chat", refs, revision=p["revision"]
        )
        self.onto.add_evidence(
            c["id"], [{"kind": "user_edit", "quote": "合成的新情境"}]
        )
        with self.assertRaises(HTTPException):
            list(guarded.stream(req))
        self.assertFalse(self.online.requests)

    def test_omit_sources_preserves_question_not_private_profile(self):
        c = self.claim()
        self.enable()
        body, p = self.preview("星桥项目应该如何推进", omitSources=True)
        self.assertFalse(p["missing"])
        self.send(body, p)
        self.assertEqual(
            self.online.requests[-1].messages[-1]["content"], body["content"]
        )
        self.assertNotIn(c["content"], self.online.requests[-1].system)

    def test_retry_is_idempotent_and_never_falls_back(self):
        self.enable()
        self.online.error = ProviderError("合成超时", code="PROVIDER_TIMEOUT")
        body, p = self.preview()
        res = self.send(body, p, requestId="synthetic-request-1")
        self.assertIn("event: error", res.text)
        self.assertEqual(len(self.convs.list_messages(self.cid)), 2)
        self.assertEqual(self.local.requests, [])
        self.online.error = None
        # Same request id reuses the saved user message and replaces only its failed answer.
        user = self.convs.list_messages(self.cid)[0]
        body, p = self.preview(retryUserMessageId=user["id"])
        res = self.send(body, p)
        self.assertIn("event: message_done", res.text)
        self.assertEqual(len(self.convs.list_messages(self.cid)), 2)
        count = len(self.online.requests)
        self.send(body, p)
        self.assertEqual(len(self.online.requests), count)

    def test_derived_history_does_not_escape_after_revoke(self):
        self.claim()
        self.enable()
        body, p = self.preview("星桥项目工作安排")
        self.grant(p)
        body, p = self.preview(body["content"])
        self.send(body, p)
        self.store.revoke("global")
        body, p = self.preview("合成案例：独立的新问题，怎样打包行李", omitSources=True)
        self.send(body, p)
        self.assertNotIn(
            "合成回复",
            json.dumps(self.online.requests[-1].messages, ensure_ascii=False),
        )
        self.assertTrue(p["excluded"])

    def test_background_pauses_with_no_request_or_expanded_grant(self):
        c = self.claim()
        self.enable()
        r = Router(self.onto, self.convs, self.cid)
        guard = GuardedProvider(
            r,
            self.online,
            "summarize_conversation",
            [r.ref("claim", c["id"])],
            background=True,
        )
        with self.assertRaises(HTTPException):
            guard.complete_json(
                ChatRequest(
                    system="合成摘要",
                    messages=[{"role": "user", "content": c["content"]}],
                )
            )
        self.assertFalse(self.online.requests)
        self.assertEqual(
            r.store.pending(self.cid)[0]["task_key"], "summarize_conversation"
        )

    def test_actual_transport_rejects_bypass_and_forbidden_domains(self):
        with patch("mindos.zhijun.provider.llm_transport.allowed_urlopen") as http:
            for url in (
                "https://synthetic.invalid/v1/chat/completions",
                "https://api.anthropic.com/v1/messages",
                "https://claude.ai/",
            ):
                with self.assertRaises(ProviderError):
                    _open(
                        url,
                        {},
                        timeout=1,
                        headers={},
                        provider="synthetic",
                        channel="chat",
                    )
            http.assert_not_called()

    def test_scope_and_immutable_claim_authority(self):
        c = self.claim()
        other = self.convs.create_conversation(device_scope="another-device")
        secret = self.convs.append_message(other["id"], "user", "OTHER_DEVICE")
        r = Router(self.onto, self.convs, self.cid)
        self.assertTrue(r.resolve(r.ref("message", secret["id"]))[0]["blocked"])
        self.enable()
        body, p = self.preview("星桥项目")
        self.grant(p)
        body, p = self.preview(body["content"])
        self.send(body, p)
        self.assertEqual(self.onto.get_claim(c["id"])["selfAlignment"]["level"], None)
        self.assertEqual(self.onto.get_claim(c["id"])["trustState"], "confirmed")

    def test_audit_and_refresh_preserve_provider_and_source_versions(self):
        self.enable()
        body, p = self.preview()
        self.send(body, p)
        response = self.client.get(self.url).json()
        reply = response["messages"][-1]
        self.assertTrue(reply["provenance"]["routing"]["service"]["external"])
        audit = self.client.get(self.url + "/routing/audits").json()["items"][0]
        self.assertEqual(audit["model"], "synthetic")
        self.assertEqual(audit["state"], "complete")
        self.assertEqual(audit["usage"]["input_tokens"], 12)

    def test_extraction_uses_only_tracked_turn_and_never_auto_confirms(self):
        from mindos.zhijun import extract
        from mindos.zhijun.provider import fake_extract
        secret = self.claim()
        self.enable()
        text = "我是一名设计师"
        m = self.convs.append_message(self.cid, "user", text,
            meta={"routingOrigin": {"service": service_info(self.online)["id"]}, "routingSources": []})
        self.online.result = fake_extract(text)
        router = Router(self.onto, self.convs, self.cid)
        guard = GuardedProvider(router, self.online, "extract_turn", [], background=True)
        result = extract.run_extraction(provider=guard, store=self.onto, conversation_id=self.cid,
            message_id=m["id"], user_text=text, prev_assistant=None)
        self.assertTrue(result["created"])
        self.assertNotIn(secret["content"], json.dumps(self.online.requests[-1].messages, ensure_ascii=False))
        for cid in result["created"]:
            c = self.onto.get_claim(cid)
            self.assertEqual(c["trustState"], "working")
            self.assertIsNone(c["selfAlignment"]["level"])
            self.assertTrue(c["evidence"][0]["locator"]["routingSources"])

    def test_first_observation_tracks_all_actual_profile_inputs(self):
        from mindos.zhijun import extract
        self.claim()
        self.onto.create_claim({"content": "合成原则：我在意自主安排", "section": "principles", "layer": "self_declared"},
            [{"kind": "user_edit", "quote": "自主安排"}], trust_state="confirmed", trust_origin="user_created")
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        guard = GuardedProvider(router, self.online, "first_observation", [], background=True)
        with self.assertRaises(HTTPException):
            extract.first_observation(provider=guard, store=self.onto, conversation_id=self.cid, message_id=None)
        self.assertEqual(self.online.requests, [])
        self.assertEqual(len([s for s in guard.last_preview["sources"] if s["kind"] == "claim"]), 2)

    def test_locator_derivative_inherits_revoked_parent(self):
        original = self.claim()
        self.enable()
        r = Router(self.onto, self.convs, self.cid)
        parent = r.resolve(r.ref("claim", original["id"]))[0]["ref"]
        derivative = self.onto.create_claim({"content": "合成派生理解：需要自主空间", "section": "ways", "layer": "hypothesis"},
            [{"kind": "user_edit", "quote": "合成", "locator": {"routingSources": [parent], "localOnly": True}}])
        req = ChatRequest(system="test", messages=[{"role": "user", "content": derivative["content"]}])
        refs = [r.ref("claim", derivative["id"])]
        p = r.prepare("chat", req, refs, self.online)
        r.authorize(p, p["missing"])
        self.store.revoke("global", "claim:" + original["id"])
        p = r.prepare("chat", req, refs, self.online)
        self.assertIn("claim:" + original["id"], p["missing"])
        with self.assertRaises(HTTPException):
            GuardedProvider(r, self.online, "chat", refs, revision=p["revision"]).complete_json(req)

    def test_related_followup_requires_history_consent_but_new_topic_does_not(self):
        self.claim()
        self.enable()
        body, p = self.preview("星桥项目工作安排")
        self.grant(p)
        body, p = self.preview(body["content"])
        self.send(body, p)
        self.store.revoke("global")
        _, followup = self.preview("那我接着应该怎么办？")
        self.assertTrue(followup["missing"])
        self.assertIn("合成回复", json.dumps(followup["request"]["messages"], ensure_ascii=False))
        _, fresh = self.preview("煮一个鸡蛋需要几分钟？")
        self.assertFalse(fresh["missing"])
        self.assertNotIn("合成回复", json.dumps(fresh["request"]["messages"], ensure_ascii=False))

    def test_actual_http_boundary_rechecks_revocation_inside_provider(self):
        c = self.claim()
        self.enable()
        r = Router(self.onto, self.convs, self.cid)
        req = ChatRequest(system="test", messages=[{"role": "user", "content": c["content"]}])
        refs = [r.ref("claim", c["id"])]
        p = r.prepare("chat", req, refs, self.online)
        r.authorize(p, p["missing"])
        p = r.prepare("chat", req, refs, self.online)
        def revoke_at_open(_):
            self.store.revoke("global")
            return _open("https://synthetic.invalid/v1/chat/completions", {}, timeout=1,
                         headers={}, provider="synthetic", channel="chat")
        self.online.complete_json = revoke_at_open
        with patch("mindos.zhijun.provider.llm_transport.allowed_urlopen") as http:
            with self.assertRaises(HTTPException):
                GuardedProvider(r, self.online, "chat", refs, revision=p["revision"]).complete_json(req)
            http.assert_not_called()

    def test_legacy_transport_cannot_bypass_and_diagnostic_cannot_carry_data(self):
        from mindos.llm_transport import allowed_urlopen
        with patch("urllib.request.build_opener") as http:
            with self.assertRaises(HTTPException):
                allowed_urlopen("https://synthetic.invalid/chat/completions", channel="chat", data=b"{}")
            with self.assertRaises(ValueError):
                allowed_urlopen("https://synthetic.invalid/chat/completions", channel="diagnostic",
                    data=json.dumps({"messages": [{"role": "user", "content": "SECRET"}]}).encode())
            http.assert_not_called()

    def test_failed_online_retry_local_survives_refresh_without_duplicates(self):
        self.enable()
        self.online.error = ProviderError("合成超时")
        body, p = self.preview()
        self.send(body, p, requestId="synthetic-local-retry")
        user = self.convs.list_messages(self.cid)[0]
        body, p = self.preview(localOnly=True, retryUserMessageId=user["id"])
        self.assertFalse(p["service"]["external"])
        self.assertIn("event: message_done", self.send(body, p).text)
        items = self.client.get(self.url).json()["messages"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[-1]["status"], "complete")
        self.assertFalse(items[-1]["external"])
        self.send(body, p)
        self.assertEqual(len(self.local.requests), 1)

    def test_interactive_waiter_has_priority_over_queued_background(self):
        import threading
        from mindos.zhijun.gate import ProviderGate
        gate, order = ProviderGate(local_limit=1), []
        self.assertTrue(gate.acquire("local", 0))
        ready = threading.Event()
        def background():
            ready.set()
            if gate.acquire("local", 2, background=True):
                order.append("background")
                gate.release("local")
        def interactive():
            if gate.acquire("local", 2):
                order.append("interactive")
                gate.release("local")
        bg, fg = threading.Thread(target=background), threading.Thread(target=interactive)
        bg.start()
        ready.wait(1)
        fg.start()
        with gate._condition:
            self.assertTrue(gate._condition.wait_for(lambda: gate._interactive["local"] == 1, 1))
        gate.release("local")
        bg.join(3)
        fg.join(3)
        self.assertEqual(order, ["interactive", "background"])

    def test_feedback_ancestry_is_traceable_without_self_reference_cycle(self):
        from mindos import alignment_routes
        c = self.claim()
        m = self.convs.append_message(self.cid, "assistant", "合成校准问题", meta={"routingSources": []})
        response = alignment_routes.propose(c["id"], alignment_routes.Proposal(
            conversationId=self.cid, messageId=m["id"], feedback="我更认同自主决定，不是这个工作安排"), None)
        self.assertEqual(response["state"], "queued")
        r = Router(self.onto, self.convs, self.cid)
        sources = r.resolve(r.ref("claim", c["id"]))
        self.assertFalse(any(s["blocked"] for s in sources), sources)
        self.assertTrue(any(s["kind"] == "message" for s in sources))
        self.assertIsNone(self.onto.get_claim(c["id"])["selfAlignment"]["level"])

    def test_local_history_does_not_reuse_retracted_profile(self):
        c = self.claim()
        r = Router(self.onto, self.convs, self.cid)
        ref = r.resolve(r.ref("claim", c["id"]))[0]["ref"]
        old = self.convs.append_message(self.cid, "assistant", "RETRACTED_PROFILE_DERIVATIVE",
            meta={"routingSources": [ref], "localOnlyDerived": True})
        self.onto.transition(c["id"], "retract", surface="ontology_page")
        body, p = self.preview("那我接着怎么办？", localOnly=True)
        self.assertIn(old["id"], [s["id"] for s in p["excluded"]])
        self.send(body, p)
        self.assertNotIn("RETRACTED_PROFILE_DERIVATIVE", json.dumps(self.local.requests[-1].messages))

    def test_lifecycle_change_after_preview_blocks_local_too(self):
        c = self.claim()
        r = Router(self.onto, self.convs, self.cid)
        req = ChatRequest(system="test", messages=[{"role": "user", "content": c["content"]}])
        refs = [r.ref("claim", c["id"])]
        guard = GuardedProvider(r, self.local, "chat", refs)
        guard.check(req)
        self.onto.transition(c["id"], "retract", surface="ontology_page")
        with self.assertRaises(HTTPException):
            guard.complete_json(req)
        self.assertEqual(self.local.requests, [])

    def test_actual_prompt_and_receipt_include_calibrated_grade_not_just_confidence(self):
        c = self.claim()
        a = c["selfAlignment"]
        updated = AlignmentStore(self.onto).review(c["id"], {
            "requestId": "synthetic-calibration-001", "expectedRevision": a["revision"],
            "claimVersion": a["claimVersion"], "evidenceVersion": a["evidenceVersion"],
            "action": "calibrate", "level": 0, "framing": "context_only", "note": "是工作安排，不是核心追求"})
        self.enable()
        body, p = self.preview("星桥项目工作安排")
        self.grant(p)
        body, p = self.preview(body["content"])
        self.send(body, p)
        self.assertIn("不代表我", self.online.requests[-1].system)
        self.assertIn("仅适用于当时情境", self.online.requests[-1].system)
        reply = self.client.get(self.url).json()["messages"][-1]
        source = reply["provenance"]["alignmentSources"][0]
        self.assertEqual(source["level"], 0)
        self.assertEqual(source["revision"], updated["selfAlignment"]["revision"])

    def default_consent(self, enabled=True, **over):
        response = self.client.put(self.url + "/routing/default-consent", json={
            "enabled": enabled, "includeFiles": False, "acknowledge": True,
            "serviceId": service_info(self.online)["id"],
            "expectedRevision": self.store.policy("global")["revision"], **over})
        return response

    def test_default_consent_requires_explicit_service_acknowledgment(self):
        self.assertFalse(self.store.policy("global")["enabled"])
        for over in ({"acknowledge": False}, {"serviceId": "wrong-service"}):
            self.assertEqual(self.default_consent(**over).status_code, 409)
        self.assertFalse(self.store.policy("global")["enabled"])
        result = self.default_consent()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["defaultAuthorization"]["active"])
        self.assertEqual(self.store.mode(self.cid)["mode"], "legacy", "consent does not enable online mode")
        body, p = self.preview()
        self.assertFalse(p["service"]["external"])
        self.send(body, p)
        self.assertFalse(self.online.requests)

    def test_default_consent_covers_relevant_new_versions_and_known_tasks_only(self):
        c = self.claim()
        self.enable()
        self.assertEqual(self.default_consent().status_code, 200)
        body, p = self.preview("星桥项目工作安排")
        self.assertFalse(p["missing"])
        self.assertEqual(self.send(body, p).status_code, 200)
        self.assertIn(c["content"], self.online.requests[-1].system)
        self.onto.add_evidence(c["id"], [{"kind": "user_edit", "quote": "新的合成情境"}])
        r = Router(self.onto, self.convs, self.cid)
        source = r.resolve(r.ref("claim", c["id"]))[0]
        self.assertTrue(r.allowed(source, service_info(self.online)["id"], "alignment"))
        self.assertFalse(r.allowed(source, service_info(self.online)["id"], "future_unapproved_task"))
        audit = self.client.get(self.url + "/routing/audits").json()["items"][0]
        self.assertTrue(any(s["authorization"] == {"kind": "default", "revision": 1} for s in audit["sources"]))
        self.assertFalse(self.store.granted("global", source, service_info(self.online)["id"], "chat"), "default must not mint permanent per-source grants")

    def test_default_consent_disable_revokes_derived_and_background_access(self):
        c = self.claim()
        self.enable()
        self.default_consent()
        body, p = self.preview("星桥项目工作安排")
        self.send(body, p)
        self.assertEqual(self.default_consent(False).status_code, 200)
        body, p = self.preview("继续分析刚才星桥项目的工作安排")
        self.assertTrue(p["missing"])
        previous = len(self.online.requests)
        self.assertEqual(self.send(body, p).status_code, 409)
        r = Router(self.onto, self.convs, self.cid)
        req = ChatRequest(system="合成摘要", messages=[{"role": "user", "content": c["content"]}])
        guard = GuardedProvider(r, self.online, "summarize_conversation", [r.ref("claim", c["id"])], background=True)
        with self.assertRaises(HTTPException):
            guard.complete_json(req)
        self.assertEqual(len(self.online.requests), previous)

    def test_default_consent_service_scope_and_source_blocking(self):
        self.enable()
        self.default_consent(includeFiles=True)
        r = Router(self.onto, self.convs, self.cid)
        source = r.resolve(r.ref("claim", self.claim()["id"]))[0]
        self.assertFalse(r.allowed(source, "different-service", "chat"))
        other = self.convs.create_conversation(device_scope="another-device")
        other_router = Router(self.onto, self.convs, other["id"])
        self.assertFalse(other_router.allowed(source, service_info(self.online)["id"], "chat"))
        self.assertFalse(r.allowed({**source, "blocked": "unknown ancestry"}, service_info(self.online)["id"], "chat"))
        self.online._base_url = "https://changed.invalid/v1"
        state = self.client.get(self.url + "/routing").json()
        self.assertFalse(state["defaultAuthorization"]["active"])
        self.assertTrue(state["defaultAuthorization"]["serviceChanged"])

    def test_default_consent_file_text_needs_separate_switch(self):
        self.enable()
        self.default_consent()
        r = Router(self.onto, self.convs, self.cid)
        source = {"key": "material:synthetic-file", "version": "v1", "kind": "material", "blocked": "", "ordinaryService": ""}
        service = service_info(self.online)["id"]
        self.assertFalse(r.allowed(source, service, "chat"))
        self.default_consent(includeFiles=True)
        self.assertTrue(r.allowed(source, service, "chat"))
        self.default_consent(False)
        self.assertFalse(r.allowed(source, service, "chat"))

    def test_default_consent_recheck_at_actual_network_boundary(self):
        self.claim()
        self.enable()
        self.default_consent()
        body, p = self.preview("星桥项目工作安排")
        self.default_consent(False)
        self.assertEqual(self.send(body, p).status_code, 409)
        self.assertFalse(self.online.requests)

    def test_default_consent_cas_restart_and_specific_revocation(self):
        c = self.claim()
        self.enable()
        self.default_consent()
        reloaded = RoutingStore(self.onto)
        self.assertTrue(reloaded.policy("global")["enabled"])
        self.assertEqual(self.default_consent(expectedRevision=0).status_code, 409)
        r = Router(self.onto, self.convs, self.cid)
        source = r.resolve(r.ref("claim", c["id"]))[0]
        self.store.revoke("global", source["key"])
        self.assertFalse(r.allowed(source, service_info(self.online)["id"], "chat"))
        self.default_consent()
        self.assertFalse(r.allowed(source, service_info(self.online)["id"], "chat"), "re-enabling must preserve explicit exclusions")
        self.store.revoke("global")
        self.assertFalse(reloaded.policy("global")["enabled"])

    def test_pending_preview_refreshes_policy_without_model_call(self):
        self.claim()
        self.enable()
        _, p = self.preview("星桥项目工作安排")
        self.store.pending(self.cid, "chat", p["revision"], "test")
        self.default_consent()
        updated = self.client.get(self.url + "/routing/pending/" + p["revision"])
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertFalse(updated.json()["missing"])
        self.assertNotEqual(updated.json()["revision"], p["revision"])
        self.assertFalse(self.online.requests)
