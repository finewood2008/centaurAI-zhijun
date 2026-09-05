"""Synthetic checks at the real routing boundary; no external network calls."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException

from tests import test_task_routing as harness
from mindos.stores.growth_store import GrowthStore
from mindos.stores.charter_draft_store import CharterDraftStore, render_document
from mindos.zhijun import charter_policy
from mindos.zhijun.provider import ChatRequest, Done, TextDelta
from mindos.zhijun.routing import Router, GuardedProvider, PURPOSES, prepare_chat, service_info, task_provider


class CharterPolicyTests(unittest.TestCase):
    setUp = harness.RoutingTests.setUp
    tearDown = harness.RoutingTests.tearDown
    enable = harness.RoutingTests.enable

    def clause(self, ident="guidance", text="先核对用户当前要求，不用旧画像替用户决定", kind="preference", control=None, **extra):
        return {"id": ident, "section": "与知君合作", "text": text, "kind": kind,
                "scope": "global", "context": "", "control": control, "sources": [], "quote": text, **extra}

    def publish(self, clauses=None, scope="global"):
        clauses = clauses or [self.clause()]
        growth = GrowthStore.instance()
        previous = growth.current_charter(scope=scope)
        return growth.create_charter({"document": render_document(clauses), "clauses": clauses,
            "expectedVersion": (previous or {}).get("version", 0), "workspaceId": "synthetic-test-workspace",
            "metadata": {"scope": scope, "origin": "workspace", "sources": [s for c in clauses for s in c["sources"]]}})

    def request(self):
        return ChatRequest(system="本体只作参考，不覆盖明确规则", messages=[{"role": "user", "content": "帮我分析当前项目的选择"}])

    def test_all_task_providers_use_actual_effective_request_and_formal_version(self):
        charter = self.publish()
        self.store.set_mode(self.cid, "local", "")
        router = Router(self.onto, self.convs, self.cid)
        for purpose in PURPOSES:
            with self.subTest(purpose=purpose):
                request = self.request()
                guarded, preview = task_provider(router, purpose, request, [])
                guarded.complete_json(request)
                actual = self.local.requests[-1]
                self.assertIn(charter_policy.POLICY_MARKER, actual.system)
                self.assertIn(charter["clauses"][0]["text"], actual.system)
                self.assertEqual(actual.system.count(charter_policy.POLICY_MARKER), 1)
                self.assertEqual(guarded.charter_basis["version"], charter["version"])
                self.assertEqual(preview["charterBasis"]["clauseIds"], ["guidance"])
                self.assertTrue(any(s["kind"] == "charter_clause" for s in guarded.last_preview["sources"]))
                self.assertEqual(request.system, "本体只作参考，不覆盖明确规则", "frozen caller request stays unchanged")
                guarded.assert_current()

    def test_raw_guard_and_replaced_refs_cannot_shed_charter(self):
        self.publish()
        router = Router(self.onto, self.convs, self.cid)
        guarded = GuardedProvider(router, self.local, "summarize_conversation", [], background=True)
        request = self.request()
        guarded.check(request)
        guarded.refs = []
        guarded.complete_json(request)
        self.assertEqual(self.local.requests[-1].system.count(charter_policy.POLICY_MARKER), 1)
        self.assertTrue(any(r["kind"] == "charter_clause" for r in guarded.refs))

    def test_omit_and_unrelated_query_do_not_remove_required_guidance(self):
        self.publish([self.clause("boundary", "不要替我决定如何处理家庭关系", kind="boundary"),
                      self.clause("principle", "理解之前先核对证据", kind="principle")])
        router = Router(self.onto, self.convs, self.cid)
        plan = prepare_chat(router, "水煮蛋需要几分钟", local=True, omit=True)
        self.assertIn("不要替我决定如何处理家庭关系", plan.preview["request"]["system"])
        self.assertIn("理解之前先核对证据", plan.assembled.system)
        self.assertEqual(set(plan.preview["charterBasis"]["clauseIds"]), {"boundary", "principle"})

    def test_new_document_inspection_does_not_report_legacy_empty_form_fields(self):
        charter = self.publish()
        router = Router(self.onto, self.convs, self.cid)
        plan = prepare_chat(router, "我的人生章程现在有哪些内容", local=True)
        self.assertIn(charter["document"], plan.assembled.system)
        self.assertIn("旧版七个表单字段为空不表示章程未填写", plan.assembled.system)
        self.assertTrue(plan.assembled.provenance["memoryContext"]["charterComplete"])
        self.assertTrue(any(s["kind"] == "charter_document" for s in plan.preview["sources"]))

    def test_version_change_blocks_queued_call_and_result_write_even_from_empty_state(self):
        router = Router(self.onto, self.convs, self.cid)
        guarded = GuardedProvider(router, self.local, "chat", [])
        request = self.request()
        guarded.check(request)
        self.publish()
        with self.assertRaises(HTTPException) as error:
            guarded.complete_json(request)
        self.assertEqual(error.exception.detail["code"], "CHARTER_CHANGED")
        self.assertEqual(self.local.requests, [])
        guarded = GuardedProvider(router, self.local, "chat", [])
        guarded.complete_json(request)
        self.publish([self.clause(text="先倾听，再提出一个可核对的问题")])
        with self.assertRaises(HTTPException):
            guarded.assert_current()

    def test_scope_isolation_does_not_use_newest_other_device_charter(self):
        global_charter = self.publish()
        device_charter = self.publish([self.clause(text="另一设备的私密协作规则")], scope="device:alpha")
        global_policy = charter_policy.scope_policy("global")
        self.assertEqual(global_policy["charterId"], global_charter["id"])
        self.assertEqual(charter_policy.scope_policy("device:alpha")["charterId"], device_charter["id"])
        self.assertEqual(charter_policy.scope_policy("device:beta")["version"], 0)
        plan = prepare_chat(Router(self.onto, self.convs, self.cid), "我想聊聊项目", local=True)
        self.assertNotIn("另一设备的私密", plan.assembled.system)
        self.assertEqual(plan.preview["charterBasis"]["charterId"], global_charter["id"])

    def test_completion_rechecks_charter_before_returning_json_or_done(self):
        self.publish()
        router = Router(self.onto, self.convs, self.cid)
        def changed_json(req):
            self.publish([self.clause(text="模型生成途中用户修改了协作要求")])
            return {"summary": "不可落库的旧版结果"}
        self.local.complete_json = changed_json
        with self.assertRaises(HTTPException) as error:
            GuardedProvider(router, self.local, "chat", []).complete_json(self.request())
        self.assertEqual(error.exception.detail["code"], "CHARTER_CHANGED")

        def changed_stream(req):
            yield TextDelta("模型生成中的文本")
            self.publish([self.clause(text="模型结束前再次更新要求")])
            yield Done("stop")
        self.local.stream = changed_stream
        stream = GuardedProvider(router, self.local, "chat", []).stream(self.request())
        self.assertIsInstance(next(stream), TextDelta)
        with self.assertRaises(HTTPException) as error:
            next(stream)
        self.assertEqual(error.exception.detail["code"], "CHARTER_CHANGED")

    def test_local_context_budget_blocks_without_truncating_confirmed_charter(self):
        self.local._num_ctx = 8192
        self.publish()
        router = Router(self.onto, self.convs, self.cid)
        GuardedProvider(router, self.local, "chat", []).complete_json(self.request())
        original = self.local.requests[-1].system
        self.assertIn(charter_policy.POLICY_MARKER, original)
        oversized = ChatRequest(system="很长的本轮背景" * 2000, messages=self.request().messages)
        with self.assertRaises(HTTPException) as error:
            GuardedProvider(router, self.local, "chat", []).complete_json(oversized)
        self.assertEqual(error.exception.detail["code"], "CHARTER_CONTEXT_TOO_LARGE")
        self.assertEqual(len(self.local.requests), 1, "oversized request never reaches local model")
        self.assertEqual(GrowthStore.instance().current_charter()["clauses"][0]["text"], self.clause()["text"])

    def test_budget_does_not_change_no_charter_legacy_calls_or_external_preview(self):
        self.local._num_ctx = 128
        router = Router(self.onto, self.convs, self.cid)
        request = ChatRequest(system="合成背景" * 2000, messages=self.request().messages)
        GuardedProvider(router, self.local, "chat", []).complete_json(request)
        self.assertEqual(len(self.local.requests), 1)
        self.publish()
        self.online._num_ctx = 128
        preview = router.prepare("chat", request, [], self.online)
        self.assertIn(charter_policy.POLICY_MARKER, preview["request"]["system"])
        self.assertTrue(preview["missing"], "online still requires separate source consent")

    def test_controls_are_explicit_and_contextual_boundary_is_not_claimed_enforced(self):
        clauses = [self.clause(c, c, control=c) for c in charter_policy.CONTROLS]
        self.publish(clauses)
        policy = charter_policy.scope_policy()
        for action in ("memory_extract", "memory_auto", "proactive", "decision_write"):
            self.assertFalse(charter_policy.check_action(policy, action)["allowed"])
            self.assertTrue(charter_policy.check_action(policy, action, explicit=True)["allowed"])
        self.assertFalse(charter_policy.check_action(policy, "chat", explicit=True, external=True)["allowed"])
        self.publish([self.clause("context", "处理家庭关系时仅本地", kind="boundary", control="local_only", scope="contextual", context="家庭关系"),
                      self.clause("natural", "不要替我决定", kind="boundary")])
        policy = charter_policy.scope_policy()
        self.assertEqual(policy["controls"], [])
        self.assertEqual(len(policy["unresolved"]), 2)
        self.assertIn("不声称已由程序完整执行", charter_policy.mandatory_context(policy)[0])

    def test_new_charter_sources_do_not_inherit_general_default_consent(self):
        self.publish()
        self.enable()
        service = service_info(self.online)["id"]
        self.store.set_policy("global", enabled=True, service=service, service_name="synthetic", include_files=True,
                              purposes=list(PURPOSES), expected_revision=0)
        router = Router(self.onto, self.convs, self.cid)
        plan = prepare_chat(router, "聊聊最近", omit=True)
        self.assertTrue(any(key.startswith("charter_clause:") for key in plan.preview["missing"]))
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", plan.refs, revision=plan.preview["revision"]).complete_json(ChatRequest(**plan.preview["request"]))
        self.assertEqual(self.online.requests, [])

    def test_background_missing_charter_consent_pauses_without_external_call(self):
        self.publish()
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "summarize_conversation", [], background=True).complete_json(self.request())
        self.assertEqual(self.online.requests, [])
        self.assertEqual(self.store.pending(self.cid)[0]["task_key"], "summarize_conversation")

    def test_exception_is_one_request_and_never_grants_source_permission(self):
        self.publish([self.clause("local", "未经本轮明确例外，只在本机处理", kind="boundary", control="local_only")])
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        make = lambda **kw: prepare_chat(router, "请帮我分析当前选择", request_id="synthetic-one-turn", **kw)
        preview = make().preview
        self.assertTrue(preview["charterConflict"]["canOverride"])
        exception = charter_policy.authorize_exception(router, preview, preview["charterConflict"]["exceptionKey"])
        plan = make(charter_exception_id=exception["exceptionId"])
        self.assertIsNone(plan.preview["charterConflict"])
        self.assertTrue(plan.preview["missing"], "rule exception is not source consent")
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", plan.refs, revision=plan.preview["revision"]).complete_json(ChatRequest(**plan.preview["request"]))
        self.assertEqual(self.online.requests, [])
        router.authorize(plan.preview, plan.preview["missing"])
        plan = make(charter_exception_id=exception["exceptionId"])
        GuardedProvider(router, self.online, "chat", plan.refs, revision=plan.preview["revision"]).complete_json(ChatRequest(**plan.preview["request"]))
        self.assertEqual(len(self.online.requests), 1)
        with self.assertRaises(HTTPException) as error:
            prepare_chat(router, "请帮我分析当前选择", request_id="another-turn", charter_exception_id=exception["exceptionId"])
        self.assertEqual(error.exception.detail["code"], "CHARTER_EXCEPTION_CHANGED")
        self.store.revoke("global")
        self.assertTrue(make(charter_exception_id=exception["exceptionId"]).preview["missing"])

    def test_clause_ancestry_revocation_and_version_change_stop_external_requests(self):
        message = self.convs.append_message(self.cid, "user", "合成原话：请先核对事实", meta={"routingSources": []})
        router = Router(self.onto, self.convs, self.cid)
        source = router.resolve(router.ref("message", message["id"]))[0]["ref"]
        self.publish([self.clause(sources=[source])])
        self.enable(fresh=True)
        router = Router(self.onto, self.convs, self.cid)
        request = self.request()
        preview = router.prepare("alignment", request, [], self.online)
        router.authorize(preview, preview["missing"])
        preview = router.prepare("alignment", request, [], self.online)
        self.store.revoke("global", "message:" + message["id"])
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "alignment", [], revision=preview["revision"]).complete_json(request)
        self.assertEqual(self.online.requests, [])
        self.convs.delete_conversation(self.cid)
        with self.assertRaises(HTTPException):
            router.check_lifecycle(router.resolve(router.ref("charter_clause", preview["charterBasis"]["charterId"] + ":guidance")))

    def test_workspace_snapshot_is_immutable_and_pending_suggestions_are_not_sources(self):
        store = CharterDraftStore()
        workspace = store.start_workspace(self.cid, "global", "start-synthetic")["workspace"]
        first = store.edit_workspace(workspace["id"], scope="global", cid=self.cid, revision=workspace["revision"],
                                     request_id="edit-first", source_text="第一份合成原文")["workspace"]
        router = Router(self.onto, self.convs, self.cid)
        ref = router.ref("charter_workspace", first["id"] + ":" + str(first["revision"]))
        before = router.resolve(ref)[0]
        store.edit_workspace(first["id"], scope="global", cid=self.cid, revision=first["revision"],
                             request_id="edit-second", source_text="第二份新原文")
        after = router.resolve(ref)[0]
        self.assertEqual(before["version"], after["version"])
        self.assertIn("第一份合成原文", after["text"])
        self.assertNotIn("第二份", after["text"])
        self.assertNotIn("suggestions", after["text"])

    def test_record_scope_uses_real_conversation_scope_and_rejects_mixing(self):
        other = self.convs.create_conversation(device_scope="device:alpha")
        self.assertEqual(charter_policy.record_scope({"evidenceRefs": [{"conversationId": other["id"]}]}, self.convs), "device:alpha")
        self.assertEqual(charter_policy.record_scope({"charterBasis": {"scope": "device:alpha"}}, self.convs), "device:alpha")
        self.assertEqual(charter_policy.record_scope({}, self.convs), "global")
        charter = self.publish(scope="device:alpha")
        self.assertEqual(charter_policy.record_scope({"charterId": charter["id"]}, self.convs), "device:alpha")
        with self.assertRaises(HTTPException):
            charter_policy.record_scope({"evidenceRefs": [{"conversationId": self.cid}, {"conversationId": other["id"]}]}, self.convs)

    def test_unrecoverable_origins_never_fall_back_to_global_or_charter_scope(self):
        origin = self.convs.create_conversation(device_scope="device:deleted-origin")
        self.convs.delete_conversation(origin["id"])
        charter = self.publish()
        unknown = {"conversationId": origin["id"]}
        records = [
            {"evidence": [unknown]},
            {"evidenceRefs": [json.dumps(unknown)]},
            {"conversationId": origin["id"]},
            {"charterBasis": {"scope": "global"}, "evidenceRefs": [unknown]},
            {"charterId": charter["id"], "evidenceRefs": [unknown]},
            {"evidenceRefs": [{"conversationId": self.cid}, unknown]},
            {"charterId": "missing-charter"},
            {"evidenceRefs": ['{"conversationId":']},
        ]
        for record in records:
            with self.subTest(record=record), self.assertRaises(HTTPException) as error:
                charter_policy.record_scope(record, self.convs)
            self.assertEqual(error.exception.detail["code"], "CHARTER_SCOPE_UNCERTAIN")
            self.assertIsNone(charter_policy.record_scope_or_none(record, self.convs))
            self.assertFalse(charter_policy.record_in_scope(record, self.convs, "global"))
        self.assertEqual(charter_policy.record_scope({"evidenceRefs": ["普通手填证据，不声称来源会话"]}, self.convs), "global")
        self.assertEqual(charter_policy.record_scope({"metadata": {"scope": "device:manual"}, "evidenceRefs": ["手填证据"]}, self.convs), "device:manual")
        self.assertEqual(charter_policy.record_scope({"charterBasis": {"scope": "device:manual"}}, self.convs), "device:manual")

    def test_unrecoverable_record_is_skipped_per_item_without_losing_valid_lists(self):
        from mindos import zhijun_home
        from mindos.zhijun import deliberate, growth_hooks, nudges
        now = datetime.now(timezone.utc)
        growth = GrowthStore.instance()
        origin = self.convs.create_conversation(device_scope="device:deleted-origin")
        payload = {"context": "合成背景", "options": ["尝试", "暂缓"], "choice": "尝试", "rationale": "获得反馈",
                   "confidence": 60, "expectedOutcome": "了解真实情况", "reviewAt": (now - timedelta(days=1)).isoformat(),
                   "relatedEntityIds": []}
        lost = growth.create_decision({**payload, "title": "失联来源的私密判断", "evidenceRefs": [json.dumps({"conversationId": origin["id"]})]})
        valid = growth.create_decision({**payload, "title": "有效的全局判断", "evidenceRefs": ["用户手填的普通证据"]})
        self.convs.delete_conversation(origin["id"])
        home = zhijun_home.build_home_overview(now=now, enqueue=False, ontology=self.onto, conversations=self.convs, growth=growth)
        rendered = json.dumps(home, ensure_ascii=False)
        self.assertNotIn(lost["title"], rendered)
        self.assertIn(valid["title"], rendered)
        nudges.scan(conv_store=self.convs, growth=growth, now=now)
        all_nudges = json.dumps(self.convs.list_nudges(), ensure_ascii=False)
        self.assertNotIn(lost["title"], all_nudges)
        self.assertIn(valid["title"], all_nudges)
        weekly = nudges.weekly_review_candidate(conv_store=self.convs, growth=growth, now=now)
        self.assertIn("1 个判断", weekly["summary"])
        result = growth_hooks.on_review({"id": "synthetic-review", "decisionId": lost["id"],
            "lessons": ["这条经验不应在来源失联后默认为全局"]}, lost, store=self.onto)
        self.assertEqual(result["reason"], "charter_scope_uncertain")
        self.assertEqual(self.onto.list_claims(), [])
        message = self.convs.append_message(self.cid, "user", "我想先尝试，用小步验证获得反馈。")
        with patch("mindos.zhijun.history.similar_decisions", return_value=[lost, valid]):
            draft, _ = deliberate.run_draft(provider=self.local, conv_store=self.convs, conversation_id=self.cid, message_id=message["id"])
        self.assertEqual(draft["fields"]["relatedDecisionIds"], [valid["id"]])
        self.assertEqual(growth.get_decision(lost["id"]), lost, "read-time exclusion never deletes or rewrites the source record")

    def test_exception_api_requires_acknowledgment_exact_preview_and_conversation(self):
        self.publish([self.clause("local", "默认仅本地处理", kind="boundary", control="local_only")])
        self.enable()
        body = {"content": "本轮合成分析", "requestId": "exception-api-turn"}
        preview = self.client.post(self.url + "/routing/preview", json=body).json()
        payload = {"revision": preview["revision"], "exceptionKey": preview["charterConflict"]["exceptionKey"]}
        response = self.client.post(self.url + "/routing/charter-exception", json=payload)
        self.assertEqual(response.status_code, 409, response.text)
        response = self.client.post(self.url + "/routing/charter-exception", json={**payload, "acknowledge": True})
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["exceptionId"]
        repeated = self.client.post(self.url + "/routing/charter-exception", json={**payload, "acknowledge": True})
        self.assertEqual(repeated.json()["exceptionId"], token)
        other = self.convs.create_conversation()["id"]
        wrong = self.client.post(f"/api/mindos/conversations/{other}/routing/charter-exception", json={**payload, "acknowledge": True})
        self.assertEqual(wrong.status_code, 409, wrong.text)
        response = self.client.post(self.url + "/routing/preview", json={**body, "charterExceptionId": token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["charterConflict"])
        self.assertTrue(response.json()["missing"])
        self.assertEqual(self.online.requests, [])

    def test_exception_rejects_changed_content_purpose_service_and_formal_version(self):
        self.publish([self.clause("local", "默认仅本地处理", kind="boundary", control="local_only")])
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        original = prepare_chat(router, "合成问题", request_id="same-turn")
        token = charter_policy.authorize_exception(router, original.preview, original.preview["charterConflict"]["exceptionKey"])["exceptionId"]
        with self.assertRaises(HTTPException):
            prepare_chat(router, "不同的合成问题", request_id="same-turn", charter_exception_id=token)
        with self.assertRaises(HTTPException):
            task_provider(router, "reply_assistance", self.request(), [], request_id="same-turn", charter_exception_id=token)
        previous_host = self.online._base_url
        self.online._base_url = "https://other-synthetic.invalid/v1"
        self.store.set_mode(self.cid, "online", service_info(self.online)["id"])
        with self.assertRaises(HTTPException):
            prepare_chat(Router(self.onto, self.convs, self.cid), "合成问题", request_id="same-turn", charter_exception_id=token)
        self.online._base_url = previous_host
        self.store.set_mode(self.cid, "online", service_info(self.online)["id"])
        self.publish([self.clause("local", "新版仍默认仅本地处理", kind="boundary", control="local_only")])
        with self.assertRaises(HTTPException):
            prepare_chat(Router(self.onto, self.convs, self.cid), "合成问题", request_id="same-turn", charter_exception_id=token)
        self.assertEqual(self.online.requests, [])

    def test_task_exception_context_reaches_actual_frozen_provider_request(self):
        self.publish([self.clause("local", "默认仅本地处理", kind="boundary", control="local_only")])
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        request = self.request()
        _, preview = task_provider(router, "reply_assistance", request, [], preview_only=True, request_id="candidate-turn")
        token = charter_policy.authorize_exception(router, preview, preview["charterConflict"]["exceptionKey"])["exceptionId"]
        _, preview = task_provider(router, "reply_assistance", request, [], preview_only=True,
                                   request_id="candidate-turn", charter_exception_id=token)
        router.authorize(preview, preview["missing"])
        _, preview = task_provider(router, "reply_assistance", request, [], preview_only=True,
                                   request_id="candidate-turn", charter_exception_id=token)
        guarded, _ = task_provider(router, "reply_assistance", request, [], revision=preview["revision"],
                                  request_id="candidate-turn", charter_exception_id=token)
        guarded.complete_json(request)
        actual = self.online.requests[-1]
        self.assertEqual(actual.debug["requestId"], "candidate-turn")
        self.assertEqual(actual.debug["charterExceptionId"], token)
        self.assertIn("默认仅本地处理", actual.system)

    def test_new_document_remains_in_historical_decision_source_closure(self):
        charter = self.publish()
        decision = GrowthStore.instance().create_decision({"title": "合成决定", "context": "合成情境", "options": ["先试", "稍后"],
            "choice": "先试", "rationale": "核对事实", "confidence": 70, "expectedOutcome": "得到反馈", "relatedEntityIds": [], "evidenceRefs": []})
        self.assertEqual(decision["charterId"], charter["id"])
        router = Router(self.onto, self.convs, self.cid)
        sources = router.resolve(router.ref("decision", decision["id"]))
        self.assertTrue(any(s["kind"] == "charter_document" and s["id"] == charter["id"] for s in sources))
        self.assertFalse(any(s["blocked"] for s in sources))

    def test_long_workspace_history_keeps_each_version_and_deduplicates_ancestors(self):
        store = CharterDraftStore()
        workspace = store.start_workspace(self.cid, "global", "chain-start")["workspace"]
        references = []
        for revision in range(2, 44):
            current = {**workspace, "revision": revision, "sourceText": f"合成第{revision}代原文", "sources": list(reversed(references))}
            with store.growth._lock, store.growth._connect() as db:
                store._save_workspace(db, current)
            references.append({"kind": "charter_workspace", "id": workspace["id"] + ":" + str(revision)})
        router = Router(self.onto, self.convs, self.cid)
        sources = router.resolve(references[-1])
        self.assertEqual(len(sources), len(references))
        self.assertFalse(any(s["blocked"] for s in sources))
        self.assertEqual({s["id"] for s in sources}, {r["id"] for r in references})
        self.assertTrue(all(s["version"] != "unavailable" for s in sources))
        router.check_lifecycle(sources)

    def test_source_cycle_and_budget_pause_without_dropping_permissions(self):
        store = CharterDraftStore()
        workspace = store.start_workspace(self.cid, "global", "cycle-start")["workspace"]
        ref = {"kind": "charter_workspace", "id": workspace["id"] + ":2"}
        with store.growth._lock, store.growth._connect() as db:
            store._save_workspace(db, {**workspace, "revision": 2, "sources": [ref]})
        router = Router(self.onto, self.convs, self.cid)
        cycle = router.resolve(ref)
        self.assertTrue(any(s.get("blockedReason") == "source_depth" for s in cycle))
        with self.assertRaises(HTTPException):
            router.check_lifecycle(cycle)
        exhausted = router.resolve({"kind": "charter_workspace", "id": workspace["id"] + ":1"}, _budget={"nodes": 1024})
        self.assertEqual(exhausted[0]["blockedReason"], "source_budget")
        with self.assertRaises(HTTPException):
            router.check_lifecycle(exhausted)
        self.assertEqual(self.local.requests, [])
        self.assertEqual(self.online.requests, [])


if __name__ == "__main__":
    unittest.main()
