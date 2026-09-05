"""HTTP/stream continuation through one lookup phase; all models are synthetic."""
import unittest
from unittest.mock import patch

from tests import test_task_routing as harness
from tests.test_zhijun_turn_sse import _parse_sse
from mindos.zhijun import context_lookup
from mindos.zhijun.provider import ChatRequest, Done, ProviderError, TextDelta
from mindos.zhijun.turn import run_turn
from mindos.zhijun.routing import GuardedProvider, Router, service_info


class LookupTurnTests(unittest.TestCase):
    def setUp(self):
        harness.RoutingTests.setUp(self)
        self.stack.enter_context(patch("mindos.zhijun.memory_index._local_encoder", return_value=(None, None)))

    tearDown = harness.RoutingTests.tearDown
    enable = harness.RoutingTests.enable
    preview = harness.RoutingTests.preview
    grant = harness.RoutingTests.grant
    send = harness.RoutingTests.send

    def hidden_claim(self):
        return self.onto.create_claim({"content": "星桥合作先签保密协议", "section": "matters",
            "layer": "self_declared", "predicate": "working_on"},
            [{"kind": "user_edit", "quote": "星桥合作先签保密协议"}],
            trust_state="confirmed", trust_origin="user_created")

    def start_pending_lookup(self, request_id="lookup-pending-001"):
        self.enable()
        claim = self.hidden_claim()
        self.online.result = {"queries": ["星桥保密协议"]}
        body, preview = self.preview("对比我过去两次选择", requestId=request_id)
        self.assertEqual(preview["missing"], [])
        response = self.send(body, preview)
        self.assertEqual(response.status_code, 200, response.text)
        events = _parse_sse(response.text)
        self.assertIn("context_phase", [name for name, _ in events])
        errors = [data for name, data in events if name == "error"]
        self.assertTrue(errors, response.text)
        error = errors[0]
        self.assertIn("preview", error)
        self.assertIn("claim:" + claim["id"], error["preview"]["missing"])
        self.assertEqual(len(self.online.requests), 1)
        self.assertEqual(self.online.requests[0].debug["task"], "context_lookup")
        self.assertNotIn(claim["content"], self.online.requests[0].system)
        self.assertNotIn("token", [name for name, _ in events])
        return claim, body, error

    def test_http_lookup_pause_preview_authorize_resume_same_nonce_once(self):
        claim, body, error = self.start_pending_lookup()
        messages = self.convs.list_messages(self.cid)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["status"], "error")
        context = messages[1]["meta"]["routingProvenance"]["contextPlan"]
        self.assertEqual(context["providedRefs"], [])
        self.assertEqual(context["citedRefs"], [])
        self.assertEqual(context["delivery"], "awaiting_authorization")
        resume = {**body, "retryUserMessageId": error["userMessageId"]}
        preview = self.client.post(self.url + "/routing/preview", json=resume)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("claim:" + claim["id"], preview.json()["missing"])
        self.grant(preview.json())
        confirmed = self.client.post(self.url + "/routing/preview", json=resume)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["missing"], [])
        self.assertEqual(len(self.online.requests), 1, "preview/grant must not repeat planner")
        def stream(req):
            self.online.requests.append(req)
            yield TextDelta("可以参考这次保密协议的经历 [p1]。伪引用测试 [p999]。")
            yield Done("stop")
        self.online.stream = stream
        response = self.send(resume, confirmed.json())
        self.assertEqual(response.status_code, 200, response.text)
        events = _parse_sse(response.text)
        self.assertNotIn("context_phase", [name for name, _ in events])
        self.assertIn("message_done", [name for name, _ in events])
        self.assertEqual(len(self.online.requests), 2)
        self.assertIn(claim["content"], self.online.requests[-1].system)
        messages = self.convs.list_messages(self.cid)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["id"], error["messageId"])
        self.assertEqual(messages[1]["status"], "complete")
        context = messages[1]["meta"]["routingProvenance"]["contextPlan"]
        self.assertIn("p1", context["providedRefs"])
        self.assertEqual(context["citedRefs"], ["p1"])
        self.assertEqual(context["citationAudit"]["invalidRefs"], ["p999"])
        replay = self.send(resume, confirmed.json())
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(len(self.online.requests), 2)
        self.assertEqual(len(self.convs.list_messages(self.cid)), 2)

    def test_direct_simple_turn_uses_only_answer_no_planner(self):
        self.enable()
        _, preview = self.preview("你好", requestId="lookup-simple-001")
        events = list(run_turn(conversation_id=self.cid, content="你好", request_id="lookup-simple-001",
                               route_revision=preview["revision"], conv_store=self.convs, ontology=self.onto))
        self.assertNotIn("context_phase", [name for name, _ in events])
        self.assertIn("message_done", [name for name, _ in events])
        self.assertEqual(len(self.online.requests), 1)
        self.assertNotEqual(self.online.requests[0].debug.get("task"), "context_lookup")

    def test_lookup_error_preserves_one_user_message_and_explicit_retry(self):
        self.enable()
        self.online.error = ProviderError("synthetic lookup timeout")
        body, preview = self.preview("对比我过去两次选择", requestId="lookup-error-001")
        failed = self.send(body, preview)
        self.assertEqual(failed.status_code, 200, failed.text)
        events = _parse_sse(failed.text)
        error = next(data for name, data in events if name == "error")
        self.assertEqual(len(self.convs.list_messages(self.cid)), 2)
        self.assertEqual(self.convs.list_messages(self.cid)[1]["status"], "error")
        self.online.error = None
        self.online.result = {"queries": []}
        resume = {**body, "retryUserMessageId": error["userMessageId"]}
        retry_preview = self.client.post(self.url + "/routing/preview", json=resume)
        self.assertEqual(retry_preview.status_code, 200, retry_preview.text)
        complete = self.send(resume, retry_preview.json())
        self.assertEqual(complete.status_code, 200, complete.text)
        self.assertIn("message_done", [name for name, _ in _parse_sse(complete.text)])
        self.assertEqual(len(self.convs.list_messages(self.cid)), 2)
        self.assertEqual(len(self.online.requests), 3)  # failed planner, retry planner, one answer

    def install_empty_lookup(self, *, first_only=False, malformed=False):
        calls = []
        def complete(req):
            self.online.requests.append(req)
            calls.append(req)
            if first_only and len(calls) > 1:
                return {"queries": []}
            if malformed:
                return {"queries": ["https://example.invalid/not-a-local-query"]}
            raise ProviderError("synthetic empty lookup", code="EMPTY_REPLY")
        self.online.complete_json = complete
        return calls

    def test_empty_lookup_then_valid_runs_two_plans_and_one_answer(self):
        self.enable()
        calls = self.install_empty_lookup(first_only=True)
        body, preview = self.preview("对比我过去两次选择", requestId="lookup-retry-success-001")
        response = self.send(body, preview)
        self.assertEqual(response.status_code, 200, response.text)
        events = _parse_sse(response.text)
        self.assertNotIn("error", [name for name, _ in events])
        self.assertIn("message_done", [name for name, _ in events])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.online.requests), 3)
        self.assertEqual(self.local.requests, [])
        messages = self.convs.list_messages(self.cid)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["status"], "complete")
        self.assertEqual(messages[1]["meta"]["contextStage"], "supplemented")

    def test_twice_empty_lookup_answers_with_notice_and_cached_replay(self):
        self.enable()
        calls = self.install_empty_lookup()
        body, preview = self.preview("对比我过去两次选择", requestId="lookup-fallback-001", depth="deep", mode="deliberate")
        response = self.send(body, preview)
        self.assertEqual(response.status_code, 200, response.text)
        events = _parse_sse(response.text)
        self.assertNotIn("error", [name for name, _ in events])
        self.assertIn("message_done", [name for name, _ in events])
        phases = [data for name, data in events if name == "context_phase" and data["stage"] == "lookup_unavailable"]
        self.assertEqual(len(phases), 1)
        self.assertIn("额外补查暂未完成", phases[0]["message"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.online.requests), 3)
        self.assertIn("额外补查暂未完成", self.online.requests[-1].system)
        self.assertEqual(self.local.requests, [])
        messages = self.convs.list_messages(self.cid)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["status"], "complete")
        self.assertEqual(messages[1]["meta"]["contextStage"], "lookup_unavailable")
        context = messages[1]["meta"]["routingProvenance"]["contextPlan"]
        self.assertEqual(context["stage"], "lookup_unavailable")
        self.assertEqual(context["lookupAttempts"], 2)
        self.assertIn("额外补查暂未完成", context["lookupNotice"])
        replay = self.send(body, preview)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(len(self.online.requests), 3)
        self.assertEqual(len(self.convs.list_messages(self.cid)), 2)

    def test_invalid_lookup_schema_twice_still_allows_answer(self):
        self.enable()
        calls = self.install_empty_lookup(malformed=True)
        body, preview = self.preview("对比我过去两次选择", requestId="lookup-schema-fallback-001")
        response = self.send(body, preview)
        self.assertEqual(response.status_code, 200, response.text)
        events = _parse_sse(response.text)
        self.assertNotIn("error", [name for name, _ in events])
        self.assertIn("message_done", [name for name, _ in events])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.online.requests), 3)
        self.assertNotIn("example.invalid/not-a-local-query", self.online.requests[-1].system)
        self.assertEqual(self.convs.list_messages(self.cid)[1]["meta"]["contextStage"], "lookup_unavailable")

    def test_final_answer_retry_reuses_unavailable_lookup_without_duplicate_messages(self):
        self.enable()
        calls = self.install_empty_lookup()
        answer_calls = []
        def stream(req):
            self.online.requests.append(req)
            answer_calls.append(req)
            if len(answer_calls) == 1:
                raise ProviderError("synthetic final timeout", code="PROVIDER_TIMEOUT")
            yield TextDelta("仅根据本轮已允许的信息回答。")
            yield Done("stop")
        self.online.stream = stream
        body, preview = self.preview("对比我过去两次选择", requestId="lookup-cached-fallback-001")
        response = self.send(body, preview)
        self.assertEqual(response.status_code, 200, response.text)
        error = next(data for name, data in _parse_sse(response.text) if name == "error")
        self.assertEqual(error["code"], "PROVIDER_TIMEOUT")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(answer_calls), 1)
        resume = {**body, "retryUserMessageId": error["userMessageId"]}
        retry_preview = self.client.post(self.url + "/routing/preview", json=resume)
        self.assertEqual(retry_preview.status_code, 200, retry_preview.text)
        complete = self.send(resume, retry_preview.json())
        self.assertEqual(complete.status_code, 200, complete.text)
        events = _parse_sse(complete.text)
        self.assertIn("message_done", [name for name, _ in events])
        self.assertNotIn("lookup", [data.get("stage") for name, data in events if name == "context_phase"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(answer_calls), 2)
        self.assertEqual(len(self.convs.list_messages(self.cid)), 2)
        self.assertEqual(self.convs.list_messages(self.cid)[1]["id"], error["messageId"])
        self.assertEqual(self.convs.list_messages(self.cid)[1]["status"], "complete")
        self.assertIn("额外补查暂未完成", answer_calls[1].system)

    def test_revocation_after_first_empty_lookup_blocks_retry_and_final_answer(self):
        self.enable()
        claim = self.onto.create_claim({"content": "我是产品负责人", "section": "who", "layer": "self_declared", "predicate": "role"},
            [{"kind": "user_edit", "quote": "我是产品负责人"}], trust_state="confirmed", trust_origin="user_created")
        router = Router(self.onto, self.convs, self.cid)
        self.store.grant("global", router.resolve(router.ref("claim", claim["id"])), service_info(self.online)["id"], "chat")
        def complete(req):
            self.online.requests.append(req)
            self.store.revoke("global", "claim:" + claim["id"])
            raise ProviderError("synthetic empty", code="EMPTY_REPLY")
        self.online.complete_json = complete
        body, preview = self.preview("对比我过去两次选择", requestId="lookup-empty-revoked-001")
        self.assertEqual(preview["missing"], [])
        response = self.send(body, preview)
        self.assertEqual(response.status_code, 200, response.text)
        events = _parse_sse(response.text)
        self.assertNotIn("token", [name for name, _ in events])
        self.assertNotIn("message_done", [name for name, _ in events])
        self.assertIn("error", [name for name, _ in events])
        self.assertEqual(len(self.online.requests), 1)
        self.assertEqual(self.online.requests[0].debug["task"], "context_lookup")
        assistant = self.convs.list_messages(self.cid)[1]
        self.assertEqual(assistant["status"], "error")
        self.assertEqual(assistant["meta"]["routingProvenance"]["contextPlan"]["providedRefs"], [])

    def assert_provider_change_after_lookup_blocks_answer(self, *, configuration_only=False):
        self.enable()
        calls = self.install_empty_lookup()
        self.online.configuration_revision = ("synthetic-profile", "old-secret-version")
        replacement = harness.Recording(host=self.online._base_url)
        replacement.model = self.online.model if configuration_only else "changed-model"
        replacement.configuration_revision = (
            "synthetic-profile", "new-secret-version" if configuration_only else "old-secret-version")
        original_run = context_lookup.run
        def lookup_then_switch(*args, **kwargs):
            value = original_run(*args, **kwargs)
            self.assertEqual(value["state"], "unavailable")
            self.assertEqual(value["attempts"], 2)
            self.stack.enter_context(patch("mindos.zhijun.routing.build_provider", return_value=replacement))
            return value
        body, preview = self.preview("对比我过去两次选择", requestId="lookup-reassembly-switch-001")
        with patch("mindos.zhijun.context_lookup.run", side_effect=lookup_then_switch):
            response = self.send(body, preview)
        self.assertEqual(response.status_code, 200, response.text)
        events = _parse_sse(response.text)
        error = next((data for name, data in events if name == "error"), None)
        self.assertIsNotNone(error, "reassembly must not silently switch the answer provider")
        self.assertEqual(error["code"], "ROUTE_CHANGED")
        self.assertNotIn("token", [name for name, _ in events])
        self.assertNotIn("message_done", [name for name, _ in events])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.online.requests), 2)
        self.assertEqual(replacement.requests, [])
        self.assertEqual(self.local.requests, [])
        messages = self.convs.list_messages(self.cid)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["status"], "error")
        self.assertEqual(messages[1]["model"], self.online.model)
        self.assertEqual(messages[1]["meta"]["routingProvenance"]["contextPlan"]["providedRefs"], [])

    def test_model_switch_between_lookup_and_reassembly_blocks_final_answer(self):
        self.assert_provider_change_after_lookup_blocks_answer()

    def test_config_revision_switch_between_lookup_and_reassembly_blocks_final_answer(self):
        self.assert_provider_change_after_lookup_blocks_answer(configuration_only=True)

    def test_repeat_pending_send_reuses_cached_phase_without_second_model_call(self):
        _, body, error = self.start_pending_lookup("lookup-repeat-001")
        resume = {**body, "retryUserMessageId": error["userMessageId"]}
        preview = self.client.post(self.url + "/routing/preview", json=resume)
        self.assertEqual(preview.status_code, 200, preview.text)
        denied = self.send(resume, preview.json())
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual(len(self.online.requests), 1)
        self.assertEqual(len(self.convs.list_messages(self.cid)), 2)

    def test_cached_phase_source_cannot_be_deleted_before_final_answer(self):
        claim, body, error = self.start_pending_lookup("lookup-delete-001")
        self.onto.transition(claim["id"], "retract", surface="ontology_page")
        resume = {**body, "retryUserMessageId": error["userMessageId"]}
        preview = self.client.post(self.url + "/routing/preview", json=resume)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertFalse(any(s["id"] == claim["id"] for s in preview.json()["sources"]))
        self.assertEqual(len(self.online.requests), 1)
        # A newly retrieved optional source may disappear; it must not remain in
        # actual text or be invented as an already read source on resume.
        response = self.send(resume, preview.json())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(claim["content"], self.online.requests[-1].system)

    def test_lookup_input_lineage_is_required_after_cache_resume(self):
        self.enable()
        seed = self.onto.create_claim({"content": "我是产品负责人", "section": "who", "layer": "self_declared", "predicate": "role"},
            [{"kind": "user_edit", "quote": "我是产品负责人"}], trust_state="confirmed", trust_origin="user_created")
        router = Router(self.onto, self.convs, self.cid)
        self.store.grant("global", router.resolve(router.ref("claim", seed["id"])), service_info(self.online)["id"], "chat")
        claim = self.hidden_claim()
        self.online.result = {"queries": ["星桥保密协议"]}
        body, preview = self.preview("对比我过去两次选择", requestId="lookup-lineage-001")
        response = self.send(body, preview)
        self.assertEqual(response.status_code, 200, response.text)
        error = next(data for name, data in _parse_sse(response.text) if name == "error")
        self.assertIn(seed["content"], self.online.requests[0].system)
        self.store.revoke("global", "claim:" + seed["id"])
        resume = {**body, "retryUserMessageId": error["userMessageId"]}
        pending = self.client.post(self.url + "/routing/preview", json=resume)
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertIn("claim:" + seed["id"], pending.json()["missing"])
        self.assertIn("claim:" + claim["id"], pending.json()["missing"])
        self.assertEqual(self.send(resume, pending.json()).status_code, 409)
        self.assertEqual(len(self.online.requests), 1)

    def test_revoked_after_preview_before_dispatch_is_not_reported_as_provided(self):
        self.enable()
        claim = self.onto.create_claim({"content": "我是产品负责人", "section": "who", "layer": "self_declared", "predicate": "role"},
            [{"kind": "user_edit", "quote": "我是产品负责人"}], trust_state="confirmed", trust_origin="user_created")
        router = Router(self.onto, self.convs, self.cid)
        self.store.grant("global", router.resolve(router.ref("claim", claim["id"])), service_info(self.online)["id"], "chat")
        _, preview = self.preview("你好", requestId="lookup-before-dispatch-001")
        stream = run_turn(conversation_id=self.cid, content="你好", request_id="lookup-before-dispatch-001",
                          route_revision=preview["revision"], conv_store=self.convs, ontology=self.onto)
        self.assertEqual(next(stream)[0], "meta")
        self.assertEqual(next(stream)[0], "provenance")
        self.store.revoke("global", "claim:" + claim["id"])
        events = list(stream)
        self.assertIn("error", [name for name, _ in events])
        self.assertEqual(self.online.requests, [], "the final egress guard blocked before any provider invocation")
        assistant = self.convs.list_messages(self.cid)[-1]
        context = assistant["meta"]["routingProvenance"]["contextPlan"]
        self.assertEqual(context["providedRefs"], [])
        self.assertEqual(context["citedRefs"], [])
        self.assertEqual(context["delivery"], "awaiting_authorization")

    def test_cancel_immediately_after_meta_keeps_prepared_not_provided_receipt(self):
        self.enable()
        claim = self.onto.create_claim({"content": "我是产品负责人", "section": "who", "layer": "self_declared", "predicate": "role"},
            [{"kind": "user_edit", "quote": "我是产品负责人"}], trust_state="confirmed", trust_origin="user_created")
        router = Router(self.onto, self.convs, self.cid)
        self.store.grant("global", router.resolve(router.ref("claim", claim["id"])), service_info(self.online)["id"], "chat")
        _, preview = self.preview("你好", requestId="lookup-cancel-before-dispatch-001")
        stream = run_turn(conversation_id=self.cid, content="你好", request_id="lookup-cancel-before-dispatch-001",
                          route_revision=preview["revision"], conv_store=self.convs, ontology=self.onto)
        self.assertEqual(next(stream)[0], "meta")
        stream.close()
        self.assertEqual(self.online.requests, [])
        assistant = self.convs.list_messages(self.cid)[-1]
        self.assertEqual(assistant["status"], "aborted")
        context = assistant["meta"]["routingProvenance"]["contextPlan"]
        self.assertEqual(context["providedRefs"], [])
        self.assertEqual(context["delivery"], "prepared")

    def test_candidate_text_cannot_borrow_newer_message_version_authorization(self):
        from mindos.zhijun.context_plan import build_context_plan
        from mindos.zhijun.context_sources import history_candidates
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        other = self.convs.create_conversation()
        old = self.convs.append_message(other["id"], "user", "星桥方案包含未授权的旧私密条件", meta={"routingSources": []})
        stale_candidates = history_candidates(router, ["星桥方案"])
        self.assertEqual([c["ref"]["id"] for c in stale_candidates], [old["id"]])
        self.convs.update_message(old["id"], content="星桥方案现在只保留公开条件")
        current = router.resolve(router.ref("message", old["id"]))
        self.store.grant("global", current, service_info(self.online)["id"], "chat")
        with patch("mindos.zhijun.context_sources.history_candidates", return_value=stale_candidates):
            plan = build_context_plan(router, "星桥方案", [], provider=self.online)
        request = ChatRequest(system=plan["system"], messages=[{"role": "user", "content": "星桥方案"}])
        preview = router.prepare("chat", request, plan["refs"], self.online)
        self.assertEqual(preview["missing"], [])
        GuardedProvider(router, self.online, "chat", plan["refs"], revision=preview["revision"]).complete_json(request)
        self.assertNotIn(old["content"], self.online.requests[-1].system,
                         "a stale retrieval snapshot must not borrow permission for a newer source version")
