"""Explicit unbound matter retrieval at the real, isolated provider boundary."""
import unittest

from fastapi import HTTPException

from tests import test_matters as harness
from mindos.zhijun.context_plan import build_context_plan, fit_context_plan
from mindos.zhijun.provider import ChatRequest
from mindos.zhijun.routing import GuardedProvider, Router, prepare_chat, service_info


class MatterRetrievalTests(unittest.TestCase):
    setUp = harness.MattersTests.setUp
    tearDown = harness.MattersTests.tearDown
    enable = harness.MattersTests.enable
    grant = harness.MattersTests.grant
    save_turn = harness.MattersTests.save_turn

    def create(self, title="我准备做一个模型微调项目", *, scope="global", **fields):
        return self.work.create(scope, {"title": title, "goal": "合成目标：验证小样本领域适应", **fields}, title)

    def plan(self, content, *, online=False, **kwargs):
        return build_context_plan(Router(self.onto, self.convs, self.cid), content, [],
                                  provider=self.online if online else self.local, **kwargs)

    def test_named_project_reaches_formal_chat_without_binding_or_claims(self):
        matter = self.create()
        other = self.convs.create_conversation()["id"]
        self.work.bind(other, "global", matter["id"], 0, "bind-other-fixture")
        plan = prepare_chat(Router(self.onto, self.convs, self.cid), "请对我的模型微调项目提出建议", local=True)
        self.assertIn("验证小样本领域适应", plan.preview["request"]["system"])
        self.assertIn(matter["id"], [ref["id"] for ref in plan.refs])
        self.assertIsNone(self.work.binding(self.cid, "global")["matter"])
        self.assertEqual(self.onto.list_claims(), [])

    def test_generic_request_offers_bounded_candidates_and_clarification(self):
        for index in range(5):
            self.create("合成事项" + str(index))
        for content in ("你对我的事件有什么建议", "你看得到我的事件嘛", "列出我的事情", "查看事情与成果"):
            plan = self.plan(content)
            self.assertEqual(len([i for i in plan["evidence"] if i["kind"] == "matter"]), 3)
            self.assertIn("并非完整清单", plan["system"])
            self.assertIn("先核对具体指哪一件", plan["system"])
            self.assertIsNone(plan["matterBinding"]["matterId"])

    def test_named_request_does_not_mix_in_unrelated_generic_results(self):
        matter = self.create()
        self.create("合成旅行计划")
        plan = self.plan("分析我的项目：模型微调")
        self.assertEqual([i["id"] for i in plan["evidence"]], [matter["id"]])

    def test_scope_status_ordinary_chat_and_lookup_hints_do_not_broaden_discovery(self):
        self.create(scope="device:other")
        matter = self.create("合成已完成事项")
        self.work.update(matter["id"], "global", {"status": "completed"}, 1, "complete-fixture")
        self.assertEqual(self.plan("你看得到我的事件嘛")["evidence"], [])
        self.create()
        for content in ("你好", "今天天气如何", "不要读取我的事情", "不要对我的事件提出建议"):
            self.assertEqual(self.plan(content, queries=["模型微调项目"])["evidence"], [])

    def test_online_requires_exact_grant_and_checks_revision_before_dispatch(self):
        self.enable()
        matter = self.create()
        router = Router(self.onto, self.convs, self.cid)
        plan = self.plan("请对模型微调项目提出建议", online=True)
        request = ChatRequest(system=plan["system"], messages=[], debug={"contextPlan": plan})
        preview = router.prepare("chat", request, plan["refs"], self.online)
        self.assertIn("matter:" + matter["id"], preview["missing"])
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", plan["refs"], revision=preview["revision"]).complete_json(request)
        self.assertEqual(self.online.requests, [])
        self.grant(router, plan["refs"])
        preview = router.prepare("chat", request, plan["refs"], self.online)
        GuardedProvider(router, self.online, "chat", plan["refs"], revision=preview["revision"]).complete_json(request)
        self.assertIn("验证小样本领域适应", self.online.requests[-1].system)
        self.work.update(matter["id"], "global", {"goal": "已变更目标"}, 1, "edit-fixture")
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", plan["refs"], revision=preview["revision"]).complete_json(request)
        self.assertEqual(len(self.online.requests), 1)

    def test_default_omit_preserved_for_bound_and_unbound_matters(self):
        self.enable()
        matter = self.create()
        self.store.set_handling("global", enabled=True, action="omit", service=service_info(self.online)["id"], expected_revision=0)
        for bound in (False, True):
            if bound:
                self.work.bind(self.cid, "global", matter["id"], 0, "bind-fixture")
            plan = self.plan("请你对我的事件提出一些实质性建议", online=True)
            self.assertEqual(plan["evidence"], [])
            self.assertNotIn("验证小样本领域适应", plan["system"])
            self.assertTrue(any(i["id"] == matter["id"] and i["restricted"] for i in plan["excluded"]))

    def test_private_recent_candidates_do_not_crowd_out_granted_matter(self):
        self.enable()
        granted = self.create("合成已授权事项")
        router = Router(self.onto, self.convs, self.cid)
        self.grant(router, [router.ref("matter", granted["id"])])
        for index in range(5):
            self.create("合成未授权事项" + str(index))
        self.store.set_handling("global", enabled=True, action="omit", service=service_info(self.online)["id"], expected_revision=0)
        plan = self.plan("你看得到我的事件嘛", online=True)
        self.assertEqual([i["id"] for i in plan["evidence"]], [granted["id"]])

    def test_suspended_binding_generic_request_does_not_resume_or_retrieve_others(self):
        matter = self.create()
        self.create("合成其他事项")
        self.work.bind(self.cid, "global", matter["id"], 0, "bind-fixture")
        self.save_turn("换个话题，聊点其他事情")
        plan = self.plan("请你对我的事件提出一些实质性建议")
        self.assertIsNotNone(plan["matterSuspended"])
        self.assertFalse(any(i["kind"] == "matter" for i in plan["evidence"]))

    def test_omit_and_budget_remove_text_and_refs_together(self):
        self.create()
        self.assertEqual(self.plan("请对模型微调项目提出建议", omit=True)["evidence"], [])
        plan = fit_context_plan(self.plan("请对模型微调项目提出建议"), 1)
        self.assertEqual(plan["system"], "")
        self.assertEqual(plan["refs"], [])
        self.assertEqual(plan["providedRefs"], [])


if __name__ == "__main__":
    unittest.main()
