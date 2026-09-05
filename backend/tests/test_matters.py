"""Synthetic work continuity, edit recovery, and actual provider-boundary checks."""
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests import test_task_routing as harness
from mindos import conversations, matters_routes
from mindos.stores.matters_store import MattersStore, source_version
from mindos.zhijun import context_lookup
from mindos.zhijun.context_plan import build_context_plan
from mindos.zhijun.provider import ChatRequest
from mindos.zhijun.routing import GuardedProvider, Router, prepare_chat, service_info


class MattersTests(unittest.TestCase):
    def setUp(self):
        harness.RoutingTests.setUp(self)
        self.work = MattersStore(self.onto, self.convs)
        app = FastAPI()
        app.include_router(conversations.router)
        app.include_router(matters_routes.build_router())
        self.client = TestClient(app)
        self.base = "/api/mindos/matters"

    tearDown = harness.RoutingTests.tearDown
    enable = harness.RoutingTests.enable

    def create(self, **extra):
        payload = {"requestId": "create-first", "title": "合伙人职责沟通", "goal": "明确职责和交接范围",
                   "context": "团队三人，周三见面", "nextStep": "准备谈话提纲", "conversationId": self.cid, **extra}
        response = self.client.post(self.base, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), payload

    def artifact(self, matter, *, meta=None):
        message = self.convs.append_message(self.cid, "assistant", "## 沟通提纲\n先说明职责，再核对交接范围 [p2]。", meta=meta or {"routingSources": []})
        response = self.client.post(self.base + f"/{matter['id']}/artifacts", json={"requestId": "save-first-artifact", "conversationId": self.cid, "messageId": message["id"], "kind": "communication"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), message

    def grant(self, router, refs, purpose="chat"):
        self.store.grant(router.scope, [s for ref in refs for s in router.resolve(ref)], service_info(self.online)["id"], purpose)

    def test_create_restart_list_and_same_request_do_not_duplicate(self):
        matter, payload = self.create()
        self.assertEqual(matter["conversationId"], self.cid)
        again = self.client.post(self.base, json=payload)
        self.assertEqual(again.json(), matter)
        self.assertEqual(self.client.get(self.base).json()["total"], 1)
        reopened = MattersStore(self.onto, self.convs)
        self.assertEqual(reopened.get(matter["id"], "global")["context"], payload["context"])
        self.assertEqual(len(reopened.history("matter", matter["id"], "global")), 1)
        self.assertEqual(self.onto.list_claims(), [])

    def test_edit_conflict_idempotency_and_original_audit(self):
        matter, _ = self.create()
        path = self.base + "/" + matter["id"]
        update = {"requestId": "edit-next-step", "expectedRevision": 1, "nextStep": "先准备职责清单", "outcome": "双方同意先试行两周"}
        response = self.client.patch(path, json=update)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["revision"], 2)
        self.assertEqual(self.client.patch(path, json=update).json(), response.json())
        stale = self.client.patch(path, json={**update, "requestId": "other-stale-edit", "nextStep": "旧页面文字"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.client.get(path).json()["nextStep"], "先准备职责清单")
        audit = self.client.get(path + "/history").json()["items"]
        self.assertEqual([x["revision"] for x in audit], [1, 2])
        self.assertEqual(audit[0]["record"]["nextStep"], "准备谈话提纲")
        self.assertEqual(self.onto.list_claims(), [])

    def test_whitespace_invalid_states_and_unknown_fields_do_not_create(self):
        for patch_value in ({"title": " "}, {"title": "x" * 121}, {"status": "active"}, {"routingSources": []}):
            response = self.client.post(self.base, json={"requestId": "invalid-request", "title": "任务", **patch_value})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.work.list("global"), [])

    def test_scope_prevents_reads_writes_and_cross_scope_conversation_binding(self):
        matter, _ = self.create()
        artifact, _ = self.artifact(matter)
        with patch("mindos.matters_routes._device_scope_of", return_value="device:other"):
            self.assertEqual(self.client.get(self.base).json()["items"], [])
            self.assertEqual(self.client.get(self.base + "/" + matter["id"]).status_code, 404)
            self.assertEqual(self.client.get("/api/mindos/artifacts/" + artifact["id"]).status_code, 404)
            self.assertEqual(self.client.put(self.url + "/matter", json={"requestId": "foreign-link", "expectedRevision": 0, "matterId": matter["id"]}).status_code, 404)

    def test_bind_resume_unbind_does_not_change_messages_or_mode(self):
        matter, _ = self.create()
        other = self.convs.create_conversation()["id"]
        path = f"/api/mindos/conversations/{other}/matter"
        body = {"requestId": "bind-other-conv", "expectedRevision": 0, "matterId": matter["id"]}
        response = self.client.put(path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.put(path, json=body).json(), response.json())
        self.assertEqual(self.client.get(path).json()["matter"]["id"], matter["id"])
        self.assertEqual(self.client.get(self.base + "/" + matter["id"]).json()["conversationId"], other)
        self.assertEqual(self.convs.list_messages(other), [])
        self.assertEqual(self.store.mode(other)["mode"], "legacy")
        self.assertEqual(self.client.put(path, json={**body, "requestId": "stale-unbind", "matterId": None}).status_code, 409)
        response = self.client.put(path, json={"requestId": "clear-bind-current", "expectedRevision": 1, "matterId": None})
        self.assertIsNone(response.json()["matter"])
        self.assertEqual(self.client.get(self.base + "/" + matter["id"]).json()["conversationId"], self.cid)

    def test_latest_conversation_ignores_deleted_and_foreign_devices(self):
        matter, _ = self.create()
        self.convs.delete_conversation(self.cid)
        self.assertIsNone(self.work.get(matter["id"], "global")["conversationId"])
        other = self.convs.create_conversation(device_scope="device:other")["id"]
        isolated = self.work.create("device:other", {"title": "该设备任务"}, "other-device-matter", other)
        self.assertEqual(isolated["conversationId"], other)

    def test_paused_and_completed_stay_visible_but_stop_automatic_context(self):
        matter, _ = self.create()
        router = Router(self.onto, self.convs, self.cid)
        initial = build_context_plan(router, "下一步怎么安排", [], provider=self.local)
        self.assertIn("团队三人", initial["system"])
        self.client.patch(self.base + "/" + matter["id"], json={"requestId": "pause-current", "expectedRevision": 1, "status": "paused"})
        paused = build_context_plan(router, "下一步怎么安排", [], provider=self.local)
        self.assertNotIn("团队三人", paused["system"])
        self.assertEqual(self.client.get(self.base + "?status=paused").json()["total"], 1)
        self.client.patch(self.base + "/" + matter["id"], json={"requestId": "complete-current", "expectedRevision": 2, "status": "completed", "outcome": "职责完成交接"})
        self.assertEqual(self.client.get(self.base).json()["total"], 0)
        self.assertEqual(self.client.get(self.base + "?status=all").json()["total"], 1)

    def test_artifact_server_snapshot_edit_and_audit_retain_ancestry(self):
        matter, _ = self.create()
        artifact, message = self.artifact(matter)
        self.assertNotIn("[p2]", artifact["markdown"])
        self.assertIn("## 沟通提纲", artifact["markdown"])
        self.assertFalse(artifact["userEdited"])
        original_sources = artifact["sources"]
        self.assertEqual(original_sources[0]["id"], message["id"])
        path = "/api/mindos/artifacts/" + artifact["id"]
        body = {"requestId": "edit-first-artifact", "expectedRevision": 1, "markdown": "## 我的沟通稿\n先问对方在意什么。"}
        edited = self.client.patch(path, json=body)
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertTrue(edited.json()["userEdited"])
        self.assertEqual(edited.json()["sources"], original_sources)
        self.assertEqual(self.client.patch(path, json=body).json(), edited.json())
        self.assertEqual(self.client.patch(path, json={**body, "requestId": "stale-artifact-edit"}).status_code, 409)
        self.assertEqual(len(self.client.get(path + "/history").json()["items"]), 2)
        self.assertEqual(self.convs.get_message(message["id"])["content"], message["content"])

    def test_only_complete_assistant_message_can_be_saved(self):
        matter, _ = self.create()
        user = self.convs.append_message(self.cid, "user", "我的原话")
        response = self.client.post(self.base + f"/{matter['id']}/artifacts", json={"requestId": "save-user-message", "conversationId": self.cid, "messageId": user["id"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.work.artifacts(matter["id"], "global"), [])

    def test_artifact_create_preserves_markdown_whitespace_exactly(self):
        matter, _ = self.create()
        markdown = "    第一行是缩进代码\n\n正文保留硬换行  \n下一行\n\n"
        message = self.convs.append_message(self.cid, "assistant", markdown, meta={"routingSources": []})
        payload = {"requestId": "save-whitespace-artifact", "conversationId": self.cid,
                   "messageId": message["id"], "title": "  沟通文稿  "}
        response = self.client.post(self.base + f"/{matter['id']}/artifacts", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        artifact = response.json()
        self.assertEqual(artifact["markdown"], markdown)
        self.assertEqual(artifact["title"], "沟通文稿")
        self.assertEqual(self.client.post(self.base + f"/{matter['id']}/artifacts", json=payload).json(), artifact)
        self.assertEqual(self.work.artifact(artifact["id"], "global")["markdown"], markdown)
        self.assertEqual(self.work.history("artifact", artifact["id"], "global")[0]["record"]["markdown"], markdown)
        empty = self.convs.append_message(self.cid, "assistant", " \n\t ", meta={"routingSources": []})
        invalid = self.client.post(self.base + f"/{matter['id']}/artifacts", json={**payload,
            "requestId": "reject-empty-artifact", "messageId": empty["id"]})
        self.assertEqual(invalid.status_code, 409)

    def test_artifact_edit_preserves_markdown_whitespace_exactly(self):
        matter, _ = self.create()
        artifact, _ = self.artifact(matter)
        markdown = "\n\t代码首行\n\n修改后的正文  \n保留换行\n\n  "
        path = "/api/mindos/artifacts/" + artifact["id"]
        payload = {"requestId": "edit-whitespace-artifact", "expectedRevision": 1,
                   "markdown": markdown, "title": "  我的提纲  "}
        response = self.client.patch(path, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        edited = response.json()
        self.assertEqual(edited["markdown"], markdown)
        self.assertEqual(edited["title"], "我的提纲")
        self.assertTrue(edited["userEdited"])
        self.assertEqual(edited["sources"], artifact["sources"])
        self.assertEqual(self.client.patch(path, json=payload).json(), edited)
        self.assertEqual(self.client.get(path).json()["markdown"], markdown)
        self.assertEqual(self.work.history("artifact", artifact["id"], "global")[-1]["record"]["markdown"], markdown)
        invalid = self.client.patch(path, json={"requestId": "reject-empty-artifact-edit", "expectedRevision": 2, "markdown": " \n\t "})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.client.get(path).json()["revision"], 2)

    def test_new_matter_not_implicitly_authorized_by_old_default_policy(self):
        self.enable()
        matter, _ = self.create()
        router = Router(self.onto, self.convs, self.cid)
        with self.onto._connect() as db:
            db.execute("INSERT INTO routing_auto_consent(scope,enabled,service,service_name,include_files,purposes_json,revision,updated_at) VALUES('global',1,?,'合成服务',1,'[\"chat\"]',1,'2026-01-01')", (service_info(self.online)["id"],))
        plan = build_context_plan(router, "下一步怎么安排", [], provider=self.online)
        self.assertEqual([x["kind"] for x in plan["evidence"]], ["matter"])
        request = ChatRequest(system=plan["system"], messages=[{"role": "user", "content": "下一步怎么安排"}], debug={"contextPlan": plan})
        preview = router.prepare("chat", request, plan["refs"], self.online)
        self.assertIn("matter:" + matter["id"], preview["missing"])
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", plan["refs"], revision=preview["revision"]).complete_json(request)
        self.assertEqual(self.online.requests, [])
        self.grant(router, plan["refs"])
        refreshed = router.prepare("chat", request, plan["refs"], self.online)
        GuardedProvider(router, self.online, "chat", plan["refs"], revision=refreshed["revision"]).complete_json(request)
        self.assertIn("团队三人", self.online.requests[-1].system)

    def test_bound_matter_cross_conversation_context_and_omit(self):
        matter, _ = self.create()
        other = self.convs.create_conversation()["id"]
        self.work.bind(other, "global", matter["id"], 0, "bind-work-context")
        router = Router(self.onto, self.convs, other)
        local = build_context_plan(router, "就按刚才那样继续", [], provider=self.local)
        self.assertIn("团队三人，周三见面", local["system"])
        omitted = build_context_plan(router, "就按刚才那样继续", [], provider=self.online, omit=True)
        self.assertNotIn("团队三人", omitted["system"])
        self.assertNotIn("团队三人", omitted["focus"]["query"])
        switched = build_context_plan(router, "换个话题，今天想聊家庭", [], provider=self.local)
        self.assertNotIn("团队三人", switched["system"])

    def test_matter_edit_revocation_service_and_bind_change_recheck_before_dispatch(self):
        self.enable()
        matter, _ = self.create()
        router = Router(self.onto, self.convs, self.cid)
        self.grant(router, [router.ref("matter", matter["id"])])
        plan = build_context_plan(router, "怎样准备沟通", [], provider=self.online)
        request = ChatRequest(system=plan["system"], messages=[], debug={"contextPlan": plan})
        preview = router.prepare("chat", request, plan["refs"], self.online)
        self.work.bind(self.cid, "global", None, 1, "unbind-before-send")
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", plan["refs"], revision=preview["revision"]).complete_json(request)
        self.assertEqual(self.online.requests, [])
        self.work.bind(self.cid, "global", matter["id"], 2, "rebind-current-work")
        plan = build_context_plan(router, "怎样准备沟通", [], provider=self.online)
        request = ChatRequest(system=plan["system"], messages=[], debug={"contextPlan": plan})
        preview = router.prepare("chat", request, plan["refs"], self.online)
        self.work.update(matter["id"], "global", {"context": "团队改为五人"}, 1, "update-before-send")
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", plan["refs"], revision=preview["revision"]).complete_json(request)
        changed = router.resolve(router.ref("matter", matter["id"]))[0]
        self.assertFalse(router.allowed(changed, service_info(self.online)["id"], "chat"))
        self.grant(router, [changed["ref"]])
        self.assertFalse(router.allowed(changed, "different-service", "chat"))
        self.store.revoke("global", changed["key"])
        self.assertFalse(router.allowed(changed, service_info(self.online)["id"], "chat"))
        self.assertEqual(self.online.requests, [])

    def test_artifact_source_deletion_and_version_change_block_derived_egress(self):
        matter, _ = self.create()
        artifact, message = self.artifact(matter)
        router = Router(self.onto, self.convs, self.cid)
        self.grant(router, [router.ref("artifact", artifact["id"])])
        self.convs.update_message(message["id"], content="已更正的原回复")
        closure = router.resolve(router.ref("artifact", artifact["id"]))
        self.assertTrue(any(x["blocked"] for x in closure))
        self.assertEqual(self.client.get("/api/mindos/artifacts/" + artifact["id"]).status_code, 200)
        with self.convs._connect() as db:
            db.execute("DELETE FROM messages WHERE id=?", (message["id"],))
        closure = router.resolve(router.ref("artifact", artifact["id"]))
        with self.assertRaises(HTTPException):
            router.check_lifecycle(closure)

    def test_artifact_edit_does_not_authorize_its_protected_parent(self):
        matter, _ = self.create()
        secret = self.onto.create_claim({"content": "保密人员安排", "section": "matters", "layer": "self_declared"}, [{"kind": "user_edit", "quote": "保密人员安排"}], trust_state="confirmed", trust_origin="user_created")
        router = Router(self.onto, self.convs, self.cid)
        parent = router.resolve(router.ref("claim", secret["id"]))[0]["ref"]
        artifact, _ = self.artifact(matter, meta={"routingSources": [parent]})
        edited = self.work.edit_artifact(artifact["id"], "global", {"markdown": "匿名化沟通提纲"}, 1, "anonymous-product")
        source = router.resolve(router.ref("artifact", edited["id"]))[0]
        self.store.grant("global", [source], service_info(self.online)["id"], "chat")
        preview = router.prepare("chat", ChatRequest(system=edited["markdown"], messages=[]), [source["ref"]], self.online)
        self.assertIn("claim:" + secret["id"], preview["missing"])
        self.assertEqual(self.online.requests, [])

    def test_lookup_cache_identity_changes_with_bound_matter_revision(self):
        matter, _ = self.create()
        router = Router(self.onto, self.convs, self.cid)
        kwargs = dict(depth="deep", mode="chat", material_refs=[], local=True, omit=False)
        first = context_lookup.fingerprint(router, "继续准备", **kwargs)
        self.work.update(matter["id"], "global", {"context": "场景改变"}, 1, "change-lookup-input")
        self.assertNotEqual(first, context_lookup.fingerprint(router, "继续准备", **kwargs))

    def save_turn(self, content, answer="本轮答复"):
        router = Router(self.onto, self.convs, self.cid)
        plan = prepare_chat(router, content, local=True)
        user = self.convs.append_message(self.cid, "user", content, meta={"routingSources": []})
        self.convs.append_message(self.cid, "assistant", answer, meta={"routingSources": plan.refs,
            "routingProvenance": plan.assembled.provenance, "replyTo": user["id"]})
        return plan, user

    def test_topic_switch_stays_suspended_across_short_replies_and_explicit_resume(self):
        matter, _ = self.create()
        first, original = self.save_turn("先准备沟通", "旧事项的专属内容：职责交接，仅供这次合伙人谈话使用。")
        self.assertIn("团队三人", first.preview["request"]["system"])
        changed, _ = self.save_turn("换个话题，我想聊女儿的学校", "她现在在哪个学校？")
        marker = {"matterId": matter["id"], "revision": 1}
        self.assertEqual(changed.assembled.provenance["contextPlan"]["matterSuspended"], marker)
        self.assertNotIn("旧事项的专属内容", str(changed.preview["request"]))
        short, _ = self.save_turn("就在家附近", "接送应该方便一些。")
        self.assertEqual(short.assembled.provenance["contextPlan"]["matterSuspended"], marker)
        self.assertNotIn("团队三人", short.preview["request"]["system"])
        self.assertNotIn("旧事项的专属内容", str(short.preview["request"]))
        # Even if no old prose is permitted into an online context, the local
        # explicit control persists without reopening that private prose.
        hidden = build_context_plan(Router(self.onto, self.convs, self.cid), "好的", [], provider=self.online)
        self.assertEqual(hidden["matterSuspended"], marker)
        self.assertNotIn("团队三人", hidden["system"])
        negated = build_context_plan(Router(self.onto, self.convs, self.cid), "先不要继续这件事", [], provider=self.local)
        self.assertEqual(negated["matterSuspended"], marker)
        self.assertNotIn("团队三人", negated["system"])
        resumed, _ = self.save_turn("回到这件事，继续准备合伙人沟通", "先确认职责。")
        self.assertIsNone(resumed.assembled.provenance["contextPlan"]["matterSuspended"])
        self.assertIn("团队三人", resumed.preview["request"]["system"])
        self.assertFalse(any("学校" in message["content"] for message in resumed.preview["request"]["messages"]))
        following, _ = self.save_turn("那就先这样", "好。")
        self.assertIsNone(following.assembled.provenance["contextPlan"]["matterSuspended"])
        self.assertIn("团队三人", following.preview["request"]["system"])
        router = Router(self.onto, self.convs, self.cid)
        router.context_before_seq = original["seq"]
        retried = build_context_plan(router, "先准备沟通", [], provider=self.local)
        self.assertIsNone(retried["matterSuspended"])

    def test_explicit_rebind_and_replacement_resume_without_other_conversation_state(self):
        matter, _ = self.create()
        self.save_turn("换个话题，我想聊学校", "你想从哪里开始？")
        self.save_turn("先谈交通", "好。")
        rebound = self.work.bind(self.cid, "global", matter["id"], 1, "explicit-resume-binding")
        self.assertEqual(rebound["bindingRevision"], 2)
        resumed, _ = self.save_turn("开始吧", "开始准备。")
        self.assertIsNone(resumed.assembled.provenance["contextPlan"]["matterSuspended"])
        self.assertIn("团队三人", resumed.preview["request"]["system"])
        self.save_turn("换个话题，先聊兴趣", "喜欢什么？")
        replacement = self.work.create("global", {"title": "年度会议", "context": "会议预算九千元"}, "replace-with-new-work")
        self.work.bind(self.cid, "global", replacement["id"], 2, "bind-new-work")
        new, _ = self.save_turn("开始准备", "好的。")
        self.assertIsNone(new.assembled.provenance["contextPlan"]["matterSuspended"])
        self.assertIn("会议预算九千元", new.preview["request"]["system"])
        self.assertNotIn("团队三人", new.preview["request"]["system"])
        other = self.convs.create_conversation()["id"]
        self.work.bind(other, "global", matter["id"], 0, "separate-conversation-binding")
        independent = build_context_plan(Router(self.onto, self.convs, other), "开始吧", [], provider=self.local)
        self.assertIsNone(independent["matterSuspended"])
        self.assertIn("团队三人", independent["system"])

    def test_interrupted_topic_switch_keeps_explicit_control(self):
        matter, _ = self.create()
        self.save_turn("先准备沟通")
        self.save_turn("换个话题，我想聊学校")
        assistant = self.convs.list_messages(self.cid)[-1]
        self.convs.update_message(assistant["id"], status="aborted")
        following = prepare_chat(Router(self.onto, self.convs, self.cid), "先谈交通", local=True)
        self.assertEqual(following.assembled.provenance["contextPlan"]["matterSuspended"],
                         {"matterId": matter["id"], "revision": 1})
        self.assertNotIn("团队三人", following.preview["request"]["system"])

    def test_inactive_explicit_review_uses_outcome_but_never_bypasses_authorization(self):
        self.enable()
        matter, _ = self.create()
        self.work.update(matter["id"], "global", {"status": "completed", "outcome": "职责明确，两周试行结束"}, 1, "complete-for-review")
        router = Router(self.onto, self.convs, self.cid)
        for content in ("你好", "回顾一下我的经历", "不要回顾这件事"):
            plan = build_context_plan(router, content, [], provider=self.local)
            self.assertFalse(any(item["kind"] == "matter" for item in plan["evidence"]))
            self.assertNotIn("两周试行结束", plan["system"])
        local = build_context_plan(router, "一起回顾这件事", [], provider=self.local)
        self.assertIn("两周试行结束", local["system"])
        self.assertIn("事项状态：已完成", local["system"])
        online = build_context_plan(router, "一起回顾这件事", [], provider=self.online)
        request = ChatRequest(system=online["system"], messages=[{"role": "user", "content": "一起回顾这件事"}], debug={"contextPlan": online})
        preview = router.prepare("chat", request, online["refs"], self.online)
        self.assertIn("matter:" + matter["id"], preview["missing"])
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", online["refs"], revision=preview["revision"]).complete_json(request)
        self.assertEqual(self.online.requests, [])
        self.grant(router, online["refs"])
        updated = router.prepare("chat", request, online["refs"], self.online)
        GuardedProvider(router, self.online, "chat", online["refs"], revision=updated["revision"]).complete_json(request)
        self.assertIn("两周试行结束", self.online.requests[-1].system)
        self.work.update(matter["id"], "global", {"status": "paused"}, 2, "pause-for-review")
        paused = build_context_plan(router, "复盘这件事", [], provider=self.local)
        self.assertIn("事项状态：已暂停", paused["system"])


if __name__ == "__main__":
    unittest.main()
