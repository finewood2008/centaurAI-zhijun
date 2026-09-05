"""Bounded lookup planning, cache lineage and citation audit; synthetic providers."""
from copy import deepcopy
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from tests import test_task_routing as harness
from mindos.zhijun import context_lookup as lookup
from mindos.zhijun.provider import ChatRequest, OpenAICompatibleProvider, ProviderError
from mindos.zhijun.routing import Router, GuardedProvider, service_info


class LookupValidationTests(unittest.TestCase):
    def test_queries_are_short_deduplicated_local_search_conditions(self):
        self.assertEqual(lookup.normalize_queries({"queries": [" 合作边界 ", "合作边界", "过去的选择"]}), ["合作边界", "过去的选择"])
        self.assertEqual(lookup.normalize_queries({"queries": []}), [])
        self.assertEqual(lookup.normalize_queries({"queries": [{"query": "合作", "entities": ["老王"], "time": "2024年"}]}), ["合作 老王 2024年"])

    def test_invalid_shapes_urls_and_unbounded_text_are_rejected(self):
        bad = [None, [], {"queries": "abc"}, {"queries": ["a"]*4}, {"queries": [None]},
               {"queries": [""]}, {"queries": ["字"*241]}, {"queries": ["https://example.invalid"]},
               {"queries": ["file:///private"]}, {"queries": ["hello\0world"]},
               {"queries": [{"query": "a", "execute": "command"}]},
               {"queries": [{"query": "a", "entities": [1]}]}]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                lookup.normalize_queries(raw)

    def test_schema_does_not_stringify_arbitrary_objects_or_ignore_commands(self):
        bad = [{"queries": [], "execute": "do something"},
               {"queries": [{"query": {"private": "object"}}]},
               {"queries": [{"query": ["not", "text"]}]},
               {"queries": [{"query": "合作", "time": {"start": "2020"}}]},
               {"queries": [{"query": "合作", "entities": ["甲", "乙", "丙", "丁"]}]},
               {"queries": [{"query": "合作", "types": ["execute"]}]}]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                lookup.normalize_queries(raw)

    def test_eligibility_is_explicit_and_never_adds_default_local_call(self):
        plan = SimpleNamespace(provider=SimpleNamespace(external=True),
                               assembled=SimpleNamespace(provenance={"contextPlan": {}}))
        self.assertFalse(lookup.eligible(plan, "你好", "brief", "chat", request_id="r"))
        self.assertTrue(lookup.eligible(plan, "对比这两次选择", "brief", "chat", request_id="r"))
        self.assertTrue(lookup.eligible(plan, "请展开", "deep", "chat", request_id="r"))
        for kwargs in ({"request_id": None}, {"request_id": "r", "omit": True},
                       {"request_id": "r", "charter_exception_id": "one-turn-only"}):
            self.assertFalse(lookup.eligible(plan, "对比这两次选择", "deep", "deliberate", **kwargs))
        plan.provider.external = False
        self.assertFalse(lookup.eligible(plan, "对比这两次选择", "deep", "deliberate", request_id="r"))

    def test_completed_lookup_and_short_slot_answer_do_not_loop(self):
        plan = SimpleNamespace(provider=SimpleNamespace(external=True),
                               assembled=SimpleNamespace(provenance={"contextPlan": {"stage": "supplemented", "needsLookup": True}}))
        self.assertFalse(lookup.eligible(plan, "对比这两次选择", "deep", "deliberate", request_id="r"))
        plan.assembled.provenance["contextPlan"] = {"focus": {"continuation": True}, "needsLookup": True}
        self.assertFalse(lookup.eligible(plan, "辅助角色", "deep", "deliberate", request_id="r"))

    def test_unavailable_lookup_is_terminal_for_the_same_turn(self):
        plan = SimpleNamespace(provider=SimpleNamespace(external=True), assembled=SimpleNamespace(
            provenance={"contextPlan": {"stage": "lookup_unavailable", "needsLookup": True}}))
        self.assertFalse(lookup.eligible(plan, "对比这两次选择", "deep", "deliberate", request_id="r"))

    def test_citation_receipt_only_accepts_provided_ids_without_mutating_packet(self):
        context = {"revision": "v1", "background": [{"citationId": "p2", "id": "b"}],
                   "evidence": [{"citationId": "p1", "id": "e"}], "refs": [{"id": "ancestor-not-a-passage"}]}
        before = deepcopy(context)
        receipt = lookup.citation_receipt(context, "依据 [p2] 和 [p1]，再次 [p2]，伪引用 [p999]。")
        self.assertEqual(receipt["providedRefs"], ["p1", "p2"])
        self.assertEqual(receipt["citedRefs"], ["p2", "p1"])
        self.assertEqual(receipt["citationAudit"], {"invalidRefs": ["p999"]})
        self.assertEqual(context, before)
        self.assertEqual(lookup.citation_receipt(context, "没有声称引用") ["citedRefs"], [])
        self.assertEqual(lookup.citation_receipt(None, "[p1]")["citationAudit"]["invalidRefs"], ["p1"])

    def test_citation_markers_are_removed_only_from_derived_reading_text(self):
        raw = "产品方向 [p2][p10][p0][p01]。材料依据 [m1][m0][m007]；普通 [x] 保留。"
        self.assertEqual(lookup.strip_citation_markers(raw), "产品方向。材料依据；普通 [x] 保留。")
        self.assertEqual(lookup.strip_citation_markers("Use [p1] because it matters."), "Use because it matters.")
        receipt = lookup.citation_receipt({"background": [{"citationId": "p2"}], "evidence": []}, raw)
        self.assertEqual(receipt["citedRefs"], ["p2"])
        self.assertEqual(receipt["citationAudit"]["invalidRefs"], ["p10", "p0", "p01", "m1", "m0", "m007"])
        plain = "用户写下的普通文本 ，以及尾部空格  "
        self.assertEqual(lookup.strip_citation_markers(plain), plain)


class LookupGuardedTests(unittest.TestCase):
    setUp = harness.RoutingTests.setUp
    tearDown = harness.RoutingTests.tearDown
    enable = harness.RoutingTests.enable
    claim = harness.RoutingTests.claim

    def plan(self, *, grant=True):
        self.enable()
        self.online.result = {"queries": ["星桥项目的合作边界"]}
        router = Router(self.onto, self.convs, self.cid)
        claim = self.claim()
        sources = router.resolve(router.ref("claim", claim["id"]))
        refs = [sources[0]["ref"]]
        if grant:
            self.store.grant(router.scope, sources, service_info(self.online)["id"], "chat")
        req = ChatRequest(system="已授权的原始上下文：" + sources[0]["text"],
                          messages=[{"role": "user", "content": "对比我的两次合作选择"}])
        preview = router.prepare("chat", req, refs, self.online)
        plan = SimpleNamespace(router=router, provider=self.online, refs=refs, preview=preview,
            assembled=SimpleNamespace(provenance={"contextPlan": {}}))
        return plan, claim

    def fingerprint(self, plan):
        return lookup.fingerprint(plan.router, "对比我的两次合作选择", depth="brief", mode="chat",
                                  material_refs=[], local=False, omit=False)

    def final_request(self, plan, saved):
        # Cached hints are derived content: final assembly MUST carry these refs,
        # not just new search hits. This tests that contract at the actual guard.
        req = ChatRequest(system="补查线索：" + "；".join(saved["queries"]), messages=[])
        preview = plan.router.prepare("chat", req, saved["sources"], self.online)
        return GuardedProvider(plan.router, self.online, "chat", saved["sources"], revision=preview["revision"]).complete_json(req)

    def test_successful_phase_is_cached_and_not_a_user_message_or_profile_edit(self):
        plan, claim = self.plan()
        before = self.onto.get_claim(claim["id"])
        messages = self.convs.list_messages(self.cid)
        fp = self.fingerprint(plan)
        first = lookup.run(plan, request_id="one-send", fingerprint_value=fp)
        second = lookup.run(plan, request_id="one-send", fingerprint_value=fp)
        self.assertEqual(first, second)
        self.assertEqual(len(self.online.requests), 1)
        self.assertEqual(first["stage"], "supplemented")
        self.assertTrue(first["sources"])
        self.assertTrue(all(source.get("version") for source in first["sources"]))
        self.assertEqual(self.convs.list_messages(self.cid), messages)
        self.assertEqual(self.onto.get_claim(claim["id"]), before)
        self.assertEqual(self.online.requests[0].max_tokens, 500)
        self.assertEqual(self.online.requests[0].debug["task"], "context_lookup")
        self.assertIsNone(lookup.cached(plan.router, "one-send", "different-input"))
        self.assertIsNone(lookup.cached(plan.router, None, fp))

    def test_no_permission_means_no_model_call_and_no_completed_cache(self):
        plan, _ = self.plan(grant=False)
        fp = self.fingerprint(plan)
        with self.assertRaises(HTTPException):
            lookup.run(plan, request_id="unauthorized", fingerprint_value=fp)
        self.assertEqual(self.online.requests, [])
        self.assertIsNone(lookup.cached(plan.router, "unauthorized", fp))

    def test_failed_or_interrupted_phase_can_retry_but_is_not_complete_cache(self):
        plan, _ = self.plan()
        fp = self.fingerprint(plan)
        self.online.error = ProviderError("synthetic timeout")
        with self.assertRaises(ProviderError):
            lookup.run(plan, request_id="retry", fingerprint_value=fp)
        self.assertIsNone(lookup.cached(plan.router, "retry", fp))
        self.online.error = None
        completed = lookup.run(plan, request_id="retry", fingerprint_value=fp)
        self.assertEqual(completed["state"], "complete")
        self.assertEqual(len(self.online.requests), 2)
        lookup._save(plan.router, "interrupted", fp, {"state": "running"})
        self.assertIsNone(lookup.cached(plan.router, "interrupted", fp))

    def test_malformed_model_output_caches_unavailable_not_completed_hints(self):
        plan, _ = self.plan()
        fp = self.fingerprint(plan)
        self.online.result = {"queries": ["https://example.invalid"]}
        saved = lookup.run(plan, request_id="invalid-output", fingerprint_value=fp)
        self.assertEqual(saved["state"], "unavailable")
        self.assertEqual(saved["stage"], "lookup_unavailable")
        self.assertEqual(saved["queries"], [])
        self.assertEqual(saved["attempts"], 2)
        self.assertEqual(saved["failureCode"], "INVALID_JSON_REPLY")
        self.assertEqual(len(self.online.requests), 2)
        self.assertEqual(lookup.cached(plan.router, "invalid-output", fp), saved)

    def test_empty_then_valid_retries_once_same_provider_and_keeps_sources(self):
        plan, _ = self.plan()
        fp = self.fingerprint(plan)
        def complete(req):
            self.online.requests.append(req)
            if len(self.online.requests) == 1:
                raise ProviderError("synthetic empty", code="EMPTY_REPLY")
            return {"queries": ["星桥项目的合作边界"]}
        self.online.complete_json = complete
        saved = lookup.run(plan, request_id="empty-then-valid", fingerprint_value=fp)
        self.assertEqual(saved["state"], "complete")
        self.assertEqual(saved["attempts"], 2)
        self.assertEqual(saved["queries"], ["星桥项目的合作边界"])
        self.assertTrue(saved["sources"])
        self.assertEqual(len(self.online.requests), 2)
        self.assertEqual(self.local.requests, [])
        self.assertTrue(all(r.debug["task"] == "context_lookup" for r in self.online.requests))
        self.assertEqual(lookup.run(plan, request_id="empty-then-valid", fingerprint_value=fp), saved)
        self.assertEqual(len(self.online.requests), 2)

    def test_empty_or_invalid_json_twice_has_bounded_terminal_fallback(self):
        plan, claim = self.plan()
        fp = self.fingerprint(plan)
        for code in ("EMPTY_REPLY", "INVALID_JSON_REPLY"):
            with self.subTest(code=code):
                self.online.requests.clear()
                self.online.error = ProviderError("synthetic unusable output", code=code)
                saved = lookup.run(plan, request_id=code, fingerprint_value=fp)
                self.assertEqual(saved["state"], "unavailable")
                self.assertEqual(saved["stage"], "lookup_unavailable")
                self.assertEqual(saved["queries"], [])
                self.assertEqual(saved["attempts"], 2)
                self.assertEqual(saved["failureCode"], code)
                self.assertIn("额外补查暂未完成", saved["notice"])
                self.assertTrue(saved["revision"])
                self.assertIn(claim["id"], [s["id"] for s in saved["sources"]])
                self.assertEqual(lookup.run(plan, request_id=code, fingerprint_value=fp), saved)
                self.assertEqual(len(self.online.requests), 2)
                self.assertEqual(self.local.requests, [])

    def test_other_provider_errors_do_not_retry_or_fallback(self):
        plan, _ = self.plan()
        fp = self.fingerprint(plan)
        for code in ("PROVIDER_TIMEOUT", "PROVIDER_BUSY", "PROVIDER_REJECTED", "PROVIDER_MISCONFIGURED"):
            with self.subTest(code=code):
                self.online.requests.clear()
                self.online.error = ProviderError("synthetic blocked request", code=code)
                with self.assertRaises(ProviderError) as raised:
                    lookup.run(plan, request_id=code, fingerprint_value=fp)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(len(self.online.requests), 1)
                self.assertIsNone(lookup.cached(plan.router, code, fp))

    def test_permission_revoked_after_empty_reply_blocks_second_call(self):
        plan, claim = self.plan()
        fp = self.fingerprint(plan)
        def complete(req):
            self.online.requests.append(req)
            self.store.revoke("global", "claim:" + claim["id"])
            raise ProviderError("synthetic empty", code="EMPTY_REPLY")
        self.online.complete_json = complete
        with self.assertRaises(HTTPException):
            lookup.run(plan, request_id="empty-then-revoked", fingerprint_value=fp)
        self.assertEqual(len(self.online.requests), 1)
        self.assertIsNone(lookup.cached(plan.router, "empty-then-revoked", fp))

    def test_source_changed_after_empty_reply_blocks_second_call(self):
        plan, claim = self.plan()
        fp = self.fingerprint(plan)
        def complete(req):
            self.online.requests.append(req)
            self.onto.add_evidence(claim["id"], [{"kind": "user_edit", "quote": "版本改变"}])
            raise ProviderError("synthetic empty", code="EMPTY_REPLY")
        self.online.complete_json = complete
        with self.assertRaises(HTTPException):
            lookup.run(plan, request_id="empty-then-changed", fingerprint_value=fp)
        self.assertEqual(len(self.online.requests), 1)
        self.assertIsNone(lookup.cached(plan.router, "empty-then-changed", fp))

    def test_model_changed_after_empty_reply_cannot_retry_on_either_model(self):
        plan, _ = self.plan()
        fp = self.fingerprint(plan)
        replacement = harness.Recording()
        replacement.model = "different-model"
        def complete(req):
            self.online.requests.append(req)
            self.stack.enter_context(patch("mindos.zhijun.routing.build_provider", return_value=replacement))
            raise ProviderError("synthetic empty", code="EMPTY_REPLY")
        self.online.complete_json = complete
        with self.assertRaises(HTTPException):
            lookup.run(plan, request_id="empty-then-model-change", fingerprint_value=fp)
        self.assertEqual(len(self.online.requests), 1)
        self.assertEqual(replacement.requests, [])
        self.assertIsNone(lookup.cached(plan.router, "empty-then-model-change", fp))

    def test_revocation_on_last_empty_reply_prevents_terminal_cache(self):
        plan, claim = self.plan()
        fp = self.fingerprint(plan)
        def complete(req):
            self.online.requests.append(req)
            if len(self.online.requests) == 2:
                self.store.revoke("global", "claim:" + claim["id"])
            raise ProviderError("synthetic empty", code="EMPTY_REPLY")
        self.online.complete_json = complete
        with self.assertRaises(HTTPException):
            lookup.run(plan, request_id="last-empty-revoked", fingerprint_value=fp)
        self.assertEqual(len(self.online.requests), 2)
        self.assertIsNone(lookup.cached(plan.router, "last-empty-revoked", fp))

    def test_unavailable_cache_retains_original_permissions_for_final_request(self):
        plan, claim = self.plan()
        fp = self.fingerprint(plan)
        self.online.error = ProviderError("synthetic empty", code="EMPTY_REPLY")
        saved = lookup.run(plan, request_id="fallback-lineage", fingerprint_value=fp)
        self.online.error = None
        self.store.revoke("global", "claim:" + claim["id"])
        cached = lookup.cached(plan.router, "fallback-lineage", fp)
        self.assertEqual(cached["sources"], saved["sources"])
        with self.assertRaises(HTTPException):
            self.final_request(plan, cached)
        self.assertEqual(len(self.online.requests), 2)

    def test_revoked_during_lookup_response_is_rejected_before_saving(self):
        plan, claim = self.plan()
        fp = self.fingerprint(plan)
        def changed(req):
            self.online.requests.append(req)
            self.store.revoke("global", "claim:" + claim["id"])
            return {"queries": ["星桥项目的私密合作情况"]}
        self.online.complete_json = changed
        with self.assertRaises(HTTPException):
            lookup.run(plan, request_id="revoke-in-flight", fingerprint_value=fp)
        self.assertIsNone(lookup.cached(plan.router, "revoke-in-flight", fp))

    def test_cached_hints_keep_lineage_when_permission_is_later_revoked(self):
        plan, claim = self.plan()
        fp = self.fingerprint(plan)
        saved = lookup.run(plan, request_id="cached", fingerprint_value=fp)
        self.store.revoke("global", "claim:" + claim["id"])
        cached = lookup.cached(plan.router, "cached", fp)
        self.assertEqual(cached["sources"], saved["sources"])
        with self.assertRaises(HTTPException):
            self.final_request(plan, cached)
        self.assertEqual(len(self.online.requests), 1)

    def test_cached_hints_cannot_bypass_changed_version_or_service(self):
        plan, claim = self.plan()
        fp = self.fingerprint(plan)
        saved = lookup.run(plan, request_id="cached", fingerprint_value=fp)
        self.onto.add_evidence(claim["id"], [{"kind": "user_edit", "quote": "补充新版本证据"}])
        with self.assertRaises(HTTPException):
            self.final_request(plan, saved)
        self.online._base_url = "https://second-synthetic.invalid/v1"
        with self.assertRaises(HTTPException):
            self.final_request(plan, saved)
        self.assertEqual(len(self.online.requests), 1)

    def test_cache_is_conversation_scoped(self):
        plan, _ = self.plan()
        fp = self.fingerprint(plan)
        lookup.run(plan, request_id="same-client-id", fingerprint_value=fp)
        other = self.convs.create_conversation(device_scope="another-device")
        router = Router(self.onto, self.convs, other["id"])
        self.assertIsNone(lookup.cached(router, "same-client-id", fp))

    def test_wire_payload_lookup_uses_primary_model_and_bounded_budget(self):
        plan, claim = self.plan()
        plan.provider = OpenAICompatibleProvider(self.online._base_url, self.online.model, "synthetic-key",
            timeout=1, task_model="unrelated-background-model", thinking="deepseek")
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"queries": ["合作边界"]}'}}]}).encode())
        with patch("mindos.zhijun.provider.llm_transport.allowed_urlopen", return_value=response) as opened:
            saved = lookup.run(plan, request_id="wire-payload", fingerprint_value=self.fingerprint(plan))
        opened.assert_called_once()
        payload = json.loads(opened.call_args.kwargs["data"])
        self.assertEqual(payload["model"], self.online.model)
        self.assertEqual(payload["max_tokens"], 500)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertFalse(payload["stream"])
        self.assertIn(claim["content"], payload["messages"][0]["content"])
        self.assertTrue(saved["sources"])

    def test_wire_invalid_json_is_a_retryable_lookup_result_not_raw_parse_error(self):
        plan, _ = self.plan()
        plan.provider = OpenAICompatibleProvider(self.online._base_url, self.online.model, "synthetic-key",
            timeout=1, thinking="deepseek")
        replies = [io.BytesIO(json.dumps({"choices": [{"message": {"content": "not JSON"},
            "finish_reason": "stop"}]}).encode()) for _ in range(2)]
        with patch("mindos.zhijun.provider.llm_transport.allowed_urlopen", side_effect=replies) as opened:
            saved = lookup.run(plan, request_id="wire-invalid-json", fingerprint_value=self.fingerprint(plan))
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(saved["state"], "unavailable")
        self.assertEqual(saved["failureCode"], "INVALID_JSON_REPLY")
        self.assertEqual(saved["queries"], [])
        for call in opened.call_args_list:
            payload = json.loads(call.kwargs["data"])
            self.assertEqual(payload["model"], self.online.model)
            self.assertEqual(payload["max_tokens"], 500)
            self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_wire_empty_content_with_usage_retries_without_changing_model_or_budget(self):
        plan, _ = self.plan()
        plan.provider = OpenAICompatibleProvider(self.online._base_url, self.online.model, "synthetic-key",
            timeout=1, thinking="deepseek")
        replies = [io.BytesIO(json.dumps(payload).encode()) for payload in (
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 2499, "completion_tokens": 94}},
            {"choices": [{"message": {"content": '{"queries": []}'}, "finish_reason": "stop"}]},
        )]
        with patch("mindos.zhijun.provider.llm_transport.allowed_urlopen", side_effect=replies) as opened:
            saved = lookup.run(plan, request_id="wire-empty-with-usage", fingerprint_value=self.fingerprint(plan))
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(saved["state"], "complete")
        self.assertEqual(saved["attempts"], 2)
        self.assertEqual(saved["queries"], [])
        for call in opened.call_args_list:
            payload = json.loads(call.kwargs["data"])
            self.assertEqual(payload["model"], self.online.model)
            self.assertEqual(payload["max_tokens"], 500)
            self.assertEqual(payload["thinking"], {"type": "disabled"})
