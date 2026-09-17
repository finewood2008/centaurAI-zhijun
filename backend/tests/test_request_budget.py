"""DeepSeek budgets are canonical before consent, not rewritten at dispatch."""
from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from tests import test_task_routing as harness
from mindos.zhijun import context_lookup
from mindos.zhijun.provider import ChatRequest
from mindos.zhijun.request_budget import normalize_request_budget
from mindos.zhijun.routing import GuardedProvider, Router
from mindos.zhijun.turn import run_turn


class RequestBudgetTests(unittest.TestCase):
    def provider(self, **overrides):
        return SimpleNamespace(**{"external": True, "name": "openai", "model": "deepseek-v4-flash", **overrides})

    def test_short_chat_lookup_and_structured_budgets_share_the_floor(self):
        for budget in (1024, 500, 800):
            with self.subTest(budget=budget):
                original = ChatRequest("合成系统", [{"role": "user", "content": "合成输入"}],
                                       max_tokens=budget, effort="low", temperature=0,
                                       json_schema={"type": "object"}, debug={"task": "synthetic"})
                result = normalize_request_budget(original, self.provider())
                self.assertEqual(asdict(result), {**asdict(original), "max_tokens": 4096})
                self.assertEqual(original.max_tokens, budget)
                self.assertIs(normalize_request_budget(result, self.provider()), result)

    def test_existing_higher_limits_and_medium_effort_are_preserved(self):
        for budget in (4096, 6000, 8192):
            request = ChatRequest("", [], max_tokens=budget, effort="medium")
            self.assertIs(normalize_request_budget(request, self.provider()), request)

    def test_malformed_budgets_are_not_coerced_into_valid_requests(self):
        for budget in (0, -1, True, False, "500", 500.0, None):
            with self.subTest(budget=budget):
                request = ChatRequest("", [], max_tokens=budget)
                self.assertIs(normalize_request_budget(request, self.provider()), request)

    def test_only_the_external_openai_deepseek_model_family_is_changed(self):
        request = ChatRequest("", [], max_tokens=500)
        for model in ("DeepSeek-V4-Flash", "vendor/deepseek-r1", "org/team/DEEPSEEK-chat"):
            with self.subTest(model=model):
                self.assertEqual(normalize_request_budget(request, self.provider(model=model)).max_tokens, 4096)
        for overrides in ({"external": False}, {"name": "ollama"}, {"name": "named_other"},
                          {"name": "OpenAI"}, {"model": "synthetic"}, {"model": "my-deepseek"},
                          {"model": "deepseek/other"}, {"model": ""}, {"model": None}):
            with self.subTest(overrides=overrides):
                self.assertIs(normalize_request_budget(request, self.provider(**overrides)), request)


class BudgetAuthorizationTests(unittest.TestCase):
    setUp = harness.RoutingTests.setUp
    tearDown = harness.RoutingTests.tearDown
    enable = harness.RoutingTests.enable
    preview = harness.RoutingTests.preview
    grant = harness.RoutingTests.grant
    send = harness.RoutingTests.send

    def online_router(self):
        self.online.model = "deepseek-v4-flash"
        self.enable()
        return Router(self.onto, self.convs, self.cid)

    def test_preview_fingerprint_and_actual_stream_request_are_identical(self):
        router = self.online_router()
        request = ChatRequest("合成系统", [{"role": "user", "content": "你好"}], max_tokens=1024)
        preview = router.prepare("chat", request, [], self.online)
        guarded = GuardedProvider(router, self.online, "chat", [], revision=preview["revision"])
        list(guarded.stream(request))
        self.assertEqual(len(self.online.requests), 1)
        self.assertEqual(self.online.requests[0].max_tokens, 4096)
        self.assertEqual(asdict(self.online.requests[0]), preview["request"])
        self.assertEqual(guarded.last_preview["revision"], preview["revision"])
        self.assertTrue(preview["request"]["debug"]["charterRequestFingerprint"])
        self.assertEqual(request.max_tokens, 1024)

    def test_structured_request_normalizes_before_both_bind_entry_points(self):
        router = self.online_router()
        request = ChatRequest("合成整理", [], max_tokens=800, json_schema={"type": "object"})
        preview = router.prepare("extract_turn", request, [], self.online)
        guarded = GuardedProvider(router, self.online, "extract_turn", [], revision=preview["revision"])
        guarded.complete_json(request)
        self.assertEqual(asdict(self.online.requests[0]), preview["request"])
        self.assertEqual(self.online.requests[0].max_tokens, 4096)
        self.assertEqual(guarded.last_preview["revision"], preview["revision"])

    def test_old_low_budget_authorization_is_not_reused(self):
        router = self.online_router()
        request = ChatRequest("合成系统", [], max_tokens=1024)
        with patch("mindos.zhijun.routing.normalize_request_budget", side_effect=lambda req, provider: req):
            previous = router.prepare("chat", request, [], self.online)
        guarded = GuardedProvider(router, self.online, "chat", [], revision=previous["revision"])
        with self.assertRaises(HTTPException) as raised:
            guarded.complete_json(request)
        self.assertEqual(raised.exception.detail["code"], "ROUTE_CHANGED")
        self.assertEqual(self.online.requests, [])
        self.assertNotEqual(guarded.last_preview["request"]["debug"]["charterRequestFingerprint"],
                            previous["request"]["debug"]["charterRequestFingerprint"])

    def test_real_lookup_500_budget_is_raised_once_without_extra_calls(self):
        router = self.online_router()
        request = ChatRequest("合成上下文", [{"role": "user", "content": "对比两个选择"}])
        preview = router.prepare("chat", request, [], self.online)
        plan = SimpleNamespace(router=router, provider=self.online, refs=[], preview=preview)
        self.online.result = {"queries": []}
        result = context_lookup.run(plan, request_id="budget-lookup", fingerprint_value="synthetic")
        self.assertEqual(result["state"], "complete")
        self.assertEqual(len(self.online.requests), 1)
        self.assertEqual(self.online.requests[0].max_tokens, 4096)
        self.assertEqual(self.online.requests[0].effort, "low")
        self.assertEqual(self.online.requests[0].debug["task"], "context_lookup")

    def test_regular_chat_preview_and_send_use_same_effective_budget(self):
        self.online_router()
        body, preview = self.preview(content="你好")
        self.assertEqual(preview["request"]["max_tokens"], 4096)
        self.grant(preview)
        response = self.send(body, preview)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("event: error", response.text)
        self.assertIn("event: message_done", response.text)
        self.assertEqual(len(self.online.requests), 1)
        self.assertEqual(self.online.requests[0].max_tokens, 4096)
        self.assertEqual(self.online.requests[0].debug["charterRequestFingerprint"],
                         preview["request"]["debug"]["charterRequestFingerprint"])

    def test_legacy_turn_keeps_local_budget_and_normalizes_external_deepseek(self):
        self.online.model = "deepseek-v4-flash"
        for provider, expected in ((self.online, 4096), (self.local, 1024)):
            with self.subTest(external=provider.external):
                cid = self.convs.create_conversation()["id"]
                events = list(run_turn(cid, "你好", provider=provider, conv_store=self.convs, ontology=self.onto))
                self.assertTrue(any(event == "message_done" for event, _ in events))
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual(provider.requests[0].max_tokens, expected)


if __name__ == "__main__":
    unittest.main()
