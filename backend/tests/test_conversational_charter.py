"""Synthetic charters, exact payload privacy, partial versions and light onboarding."""
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from tests.test_task_routing import RoutingTests
from mindos.stores.growth_store import GrowthStore, GrowthConflictError
from mindos.stores.charter_draft_store import CharterDraftStore
from mindos.zhijun.charter import DraftRequest, generate, topic_progress, onboarding_context, build_router
from mindos.zhijun.routing import Router, GuardedProvider, prepare_chat, service_info
from mindos.zhijun.provider import ChatRequest, ProviderError
from mindos import growth, zhijun_onboarding


class CharterTests(unittest.TestCase):
    setUp = RoutingTests.setUp
    tearDown = RoutingTests.tearDown
    enable = RoutingTests.enable
    grant = RoutingTests.grant

    def seed(self, text="我希望今年留出更多时间陪伴家人", field="goals", external=False, assisted=False):
        meta = {"routingSources": []}
        if assisted:
            meta["replyAssistance"] = {"kind": "assisted"}
        m = self.convs.append_message(self.cid, "user", text, meta=meta)
        result = {"proposals": [{"field": field, "text": text, "messageId": m["id"], "quote": text}]}
        self.local.result = self.online.result = result
        return m

    def generate(self, ident="synthetic-request-001", external=False):
        r = Router(self.onto, self.convs, self.cid)
        req = DraftRequest(requestId=ident)
        preview = generate(r, req.model_copy(update={"previewOnly": True}))["routePreview"]
        if external:
            self.grant(preview)
            preview = generate(r, req.model_copy(update={"previewOnly": True}))["routePreview"]
        return generate(r, req.model_copy(update={"routeRevision": preview["revision"]}))["draft"]

    def accept(self, draft, field="goals", text=None, request_id="synthetic-confirm-001"):
        return CharterDraftStore().act(draft["id"], scope="global", cid=self.cid, revision=draft["revision"],
            selections={field: text or draft["fields"][field]["text"]}, skip=[], request_id=request_id)

    def test_partial_charter_has_no_required_vision_or_style(self):
        value = growth.create_charter(growth.CharterCreate(goals=["完成一次小尝试"], expectedVersion=0, requestId="manual-partial-1"))
        self.assertEqual(value["version"], 1)
        self.assertEqual(value["vision"], "")
        self.assertEqual(value["metadata"]["fields"]["boundaries"]["state"], "pending")

    def test_empty_charter_is_not_created(self):
        with self.assertRaises(HTTPException) as ctx:
            growth.create_charter(growth.CharterCreate())
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIsNone(GrowthStore.instance().current_charter())

    def test_manual_retry_is_idempotent_and_conflicting_retry_is_rejected(self):
        req = growth.CharterCreate(roles=["父亲"], expectedVersion=0, requestId="manual-retry-001")
        a = growth.create_charter(req)
        self.assertEqual(a, growth.create_charter(req))
        with self.assertRaises(HTTPException):
            growth.create_charter(req.model_copy(update={"roles": ["教师"]}))

    def test_draft_generation_does_not_create_claim_or_formal_charter(self):
        self.seed()
        before = self.convs.count_messages(self.cid)
        draft = self.generate()
        self.assertIn("goals", draft["fields"])
        self.assertEqual(before, self.convs.count_messages(self.cid))
        self.assertIsNone(GrowthStore.instance().current_charter())
        self.assertEqual(self.onto.stats()["claims"]["confirmed"], 0)

    def test_selection_edit_confirm_retains_sources_and_does_not_confirm_ontology(self):
        self.seed(assisted=True)
        draft = self.generate()
        saved = self.accept(draft, text="我希望每周抽出一段时间陪家人")["charter"]
        self.assertTrue(saved["metadata"]["fields"]["goals"]["sources"])
        self.assertTrue(saved["metadata"]["fields"]["goals"]["edited"])
        self.assertTrue(draft["fields"]["goals"]["assisted"])
        self.assertEqual(self.onto.stats()["claims"]["confirmed"], 0)

    def test_restart_and_duplicate_confirm_preserve_one_version(self):
        self.seed(); draft = self.generate()
        saved = self.accept(draft)
        reopened = CharterDraftStore()
        self.assertEqual(saved, reopened.act(draft["id"], scope="global", cid=self.cid, revision=1,
            selections={"goals": draft["fields"]["goals"]["text"]}, skip=[], request_id="synthetic-confirm-001"))
        self.assertEqual(len(GrowthStore.instance().list_charters()), 1)

    def test_old_proposal_cannot_overwrite_manual_change(self):
        self.seed(); draft = self.generate()
        growth.create_charter(growth.CharterCreate(goals=["现在更想先休息"], expectedVersion=0))
        with self.assertRaises(GrowthConflictError): self.accept(draft)
        self.assertEqual(GrowthStore.instance().current_charter()["goals"], ["现在更想先休息"])

    def test_regeneration_after_manual_change_supersedes_stale_proposal(self):
        self.seed(); old = self.generate()
        growth.create_charter(growth.CharterCreate(roles=["父亲"]))
        new = self.generate("synthetic-regenerate-002")
        self.assertIn("goals", new["fields"])
        self.assertEqual(CharterDraftStore().get(old["id"])["fields"]["goals"]["status"], "superseded")
        saved = self.accept(new)["charter"]
        self.assertEqual(saved["roles"], ["父亲"])

    def test_skipped_proposal_does_not_reappear_from_same_source(self):
        self.seed(); draft = self.generate()
        CharterDraftStore().act(draft["id"], scope="global", cid=self.cid, revision=1, selections={}, skip=["goals"], request_id="skip-one-001")
        again = self.generate("generate-again-001")
        self.assertEqual(again["fields"], {})
        self.assertIsNone(GrowthStore.instance().current_charter())

    def test_fabricated_quote_is_discarded(self):
        self.seed(); self.local.result["proposals"][0]["quote"] = "我赚了一千万"
        self.assertEqual(self.generate()["fields"], {})

    def test_single_behavior_cannot_become_a_long_term_principle(self):
        self.seed("今天我拒绝了一次加班", "principles")
        self.assertEqual(self.generate()["fields"], {})

    def test_explicit_principle_and_wish_are_separate_fields(self):
        self.seed("我一直坚持诚实，这是我的原则", "principles")
        draft = self.generate()
        self.assertIn("principles", draft["fields"])
        saved = self.accept(draft, "principles")["charter"]
        self.assertEqual(saved["vision"], "")

    def test_cross_conversation_and_scope_cannot_accept(self):
        self.seed(); draft = self.generate()
        for cid, scope in (("wrong", "global"), (self.cid, "other")):
            with self.assertRaises(GrowthConflictError):
                CharterDraftStore().act(draft["id"], scope=scope, cid=cid, revision=1, selections={"goals": "新目标"}, skip=[], request_id="cross-scope-001")

    def test_unapproved_charter_context_is_blocked_before_online_request(self):
        growth.create_charter(growth.CharterCreate(challengeStyle="先倾听，不替我做决定"))
        self.enable()
        r = Router(self.onto, self.convs, self.cid)
        plan = prepare_chat(r, "今天有件事想聊聊")
        self.assertTrue(plan.preview["missing"])
        with self.assertRaises(HTTPException):
            list(GuardedProvider(r, self.online, "chat", plan.refs, revision=plan.preview["revision"]).stream(ChatRequest(**plan.preview["request"])))
        self.assertEqual(self.online.requests, [])

    def test_approved_charter_used_and_actual_version_recorded(self):
        c = growth.create_charter(growth.CharterCreate(challengeStyle="先倾听，不替我做决定"))
        self.enable(); r = Router(self.onto, self.convs, self.cid)
        plan = prepare_chat(r, "今天有件事想聊聊"); self.grant(plan.preview)
        plan = prepare_chat(r, "今天有件事想聊聊")
        list(GuardedProvider(r, self.online, "chat", plan.refs, revision=plan.preview["revision"]).stream(ChatRequest(**plan.preview["request"])))
        self.assertIn(c["challengeStyle"], self.online.requests[-1].system)
        self.assertEqual(plan.assembled.provenance["charterVersion"], 1)

    def test_new_version_requires_new_grant(self):
        growth.create_charter(growth.CharterCreate(challengeStyle="先倾听"))
        self.enable(); r = Router(self.onto, self.convs, self.cid)
        self.grant(prepare_chat(r, "聊聊").preview)
        growth.create_charter(growth.CharterCreate(challengeStyle="直接提醒我", expectedVersion=1))
        self.assertTrue(prepare_chat(r, "聊聊").preview["missing"])

    def test_revoke_and_service_switch_block_actual_payload(self):
        growth.create_charter(growth.CharterCreate(challengeStyle="先倾听"))
        self.enable(); r = Router(self.onto, self.convs, self.cid)
        self.grant(prepare_chat(r, "聊聊").preview)
        plan = prepare_chat(r, "聊聊")
        self.store.revoke("global")
        with self.assertRaises(HTTPException):
            list(GuardedProvider(r, self.online, "chat", plan.refs, revision=plan.preview["revision"]).stream(ChatRequest(**plan.preview["request"])))
        self.assertEqual(self.online.requests, [])
        self.online._base_url = "https://another-synthetic.invalid/v1"
        with self.assertRaises(HTTPException): prepare_chat(r, "聊聊")

    def test_draft_generation_has_separate_purpose(self):
        self.enable(); self.seed()
        r = Router(self.onto, self.convs, self.cid)
        preview = generate(r, DraftRequest(requestId="purpose-check-001", previewOnly=True))["routePreview"]
        self.assertEqual(preview["purpose"], "charter_draft")
        with self.assertRaises(HTTPException): generate(r, DraftRequest(requestId="purpose-check-001", routeRevision=preview["revision"]))
        self.assertEqual(self.online.requests, [])

    def test_fresh_online_charter_context_excludes_protected_old_history(self):
        self.convs.append_message(self.cid, "user", "SECRET_OLD_DIRECTION", meta={"localOnlyDerived": True})
        self.enable(fresh=True)
        self.seed()
        draft = self.generate(external=True)
        self.assertIn("goals", draft["fields"])
        payload = json.dumps([r.messages for r in self.online.requests], ensure_ascii=False)
        self.assertNotIn("SECRET_OLD_DIRECTION", payload)
        self.assertIn("陪伴家人", payload)

    def test_fresh_online_context_without_new_messages_does_not_call_model(self):
        self.seed()
        self.enable(fresh=True)
        self.assertEqual(self.generate(external=True)["fields"], {})
        self.assertEqual(self.online.requests, [])

    def test_legacy_background_without_explicit_workspace_never_requests_authorization(self):
        self.enable(); self.seed()
        result = generate(Router(self.onto, self.convs, self.cid), DraftRequest(requestId="background-001"), background=True)
        self.assertEqual(result["state"], "skipped")
        self.assertEqual(self.store.pending(self.cid), [])
        self.assertEqual(self.online.requests, [])

    def test_model_unavailable_keeps_messages_and_creates_no_charter(self):
        self.seed(); self.local.error = ProviderError("timeout")
        with self.assertRaises(HTTPException): self.generate()
        self.assertEqual(self.convs.count_messages(self.cid), 1)
        self.assertIsNone(GrowthStore.instance().current_charter())

    def test_manual_edit_of_derived_charter_preserves_restrictions(self):
        self.seed(); saved = self.accept(self.generate())["charter"]
        edited = growth.create_charter(growth.CharterCreate(goals=["我改了说法"], expectedVersion=1))
        self.assertEqual(saved["metadata"]["fields"]["goals"]["sources"], edited["metadata"]["fields"]["goals"]["sources"])

    def test_deleted_source_blocks_charter_reading_and_acceptance(self):
        m = self.seed(); draft = self.generate(); c = self.accept(draft)["charter"]
        with self.convs._connect() as db: db.execute("DELETE FROM messages WHERE id=?", (m["id"],))
        r = Router(self.onto, self.convs, self.cid)
        closure = r.resolve(r.ref("charter", c["id"] + ":goals"))
        self.assertTrue(any(s["blocked"] for s in closure))

    def test_derived_history_cannot_bypass_charter_permission(self):
        c = growth.create_charter(growth.CharterCreate(challengeStyle="PRIVATE_STYLE"))
        r = Router(self.onto, self.convs, self.cid)
        refs = [s["ref"] for s in r.resolve(r.ref("charter", c["id"] + ":challengeStyle"))]
        m = self.convs.append_message(self.cid, "assistant", "PRIVATE_SUMMARY", meta={"routingSources": refs})
        self.enable(True); r = Router(self.onto, self.convs, self.cid)
        req = ChatRequest(system="摘要", messages=[{"role": "user", "content": m["content"]}])
        guarded = GuardedProvider(r, self.online, "summarize_conversation", [r.ref("message", m["id"])], background=True)
        with self.assertRaises(HTTPException): guarded.complete_json(req)
        self.assertEqual(self.online.requests, [])

    def test_finish_does_not_require_any_answers_or_publish_charter(self):
        self.client.app.include_router(zhijun_onboarding.router)
        self.client.post('/api/mindos/zhijun/onboarding', json={"action": "start"})
        res = self.client.post('/api/mindos/zhijun/onboarding', json={"action": "finish"})
        self.assertEqual(res.json()["state"], "ready")
        self.assertIsNone(GrowthStore.instance().current_charter())

    def test_topic_progress_is_not_message_count_and_keeps_sources(self):
        m = {"id": "one", "role": "user", "content": "我是教师，目前在做一个课程，希望今年留出更多家庭时间"}
        topics = topic_progress([m])
        self.assertEqual([t["state"] for t in topics[:3]], ["discussed"] * 3)
        self.assertEqual(topics[2]["messageIds"], ["one"])
        self.assertEqual(topics[3]["state"], "pending")

    def test_skip_and_rephrase_are_not_personal_answers(self):
        base = [{"id": "a", "role": "assistant", "content": "介绍一下自己", "meta": {"onboardingTopic": "situation"}}]
        rephrase = topic_progress(base, "请换一个说法", {"kind": "control", "control": "rephrase"})
        skip = topic_progress(base, "先放一放", {"kind": "control", "control": "pause"})
        self.assertEqual(rephrase[0]["state"], "pending")
        self.assertEqual(skip[0]["state"], "skipped")
        self.assertTrue(all(t["state"] == "pending" for t in skip[1:]))

    def test_background_task_requires_explicit_workspace_and_keeps_local_choice(self):
        from mindos.zhijun.charter import enqueue
        self.assertIsNone(enqueue(self.cid, 'before-start', '我希望今年多陪家人', ontology=self.onto))
        CharterDraftStore().start_workspace(self.cid, 'global', 'explicit-charter-start')
        enqueue(self.cid, 'synthetic-message', '我希望今年多陪家人', ontology=self.onto, local_only=True)
        with self.onto._connect() as db:
            row = db.execute("SELECT * FROM ontology_jobs WHERE kind='charter_draft'").fetchone()
        self.assertTrue(json.loads(row['payload_json'])['localOnly'])

    def test_confirmed_charter_stops_daily_background_suggestions(self):
        from mindos.zhijun.charter import enqueue
        saved = growth.create_charter(growth.CharterCreate(principles=['诚实面对不确定性']))
        enqueue(self.cid, 'new-daily-message', '我希望换一个工作，这是我的新目标', ontology=self.onto)
        with self.onto._connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM ontology_jobs WHERE kind='charter_draft'").fetchone())
        self.assertEqual(GrowthStore.instance().current_charter(), saved)

    def test_old_queued_job_stops_before_model_and_authorization(self):
        self.seed()
        growth.create_charter(growth.CharterCreate(goals=['保留这个目标']))
        r = Router(self.onto, self.convs, self.cid)
        with patch.object(r, 'provider', side_effect=AssertionError('must not select a model')):
            result = generate(r, DraftRequest(requestId='old-queued-job'), background=True)
        self.assertEqual(result['reason'], 'charter_confirmed')
        self.assertEqual(CharterDraftStore().list(self.cid, 'global'), [])
        self.assertEqual(self.store.pending(self.cid), [])

    def test_legacy_background_never_starts_before_explicit_editing(self):
        self.seed()
        r = Router(self.onto, self.convs, self.cid)
        with patch.object(self.local, 'complete_json', side_effect=AssertionError('no active workspace')) as model:
            result = generate(r, DraftRequest(requestId='running-at-confirmation'), background=True)
        self.assertEqual(result['state'], 'skipped')
        model.assert_not_called()
        self.assertEqual(CharterDraftStore().list(self.cid, 'global'), [])
        self.assertIsNone(GrowthStore.instance().current_charter())

    def test_old_background_authorization_prompt_is_hidden_and_cannot_resume(self):
        self.store.pending(self.cid, 'charter_draft', 'old-preview', '等待授权')
        self.store.pending(self.cid, 'alignment', 'other-preview', '其他任务')
        growth.create_charter(growth.CharterCreate(goals=['已确认']))
        status = self.client.get(self.url + '/routing').json()
        self.assertEqual([p['task_key'] for p in status['pending']], ['alignment'])
        from mindos.routing_routes import Resume, resume
        with patch('mindos.routing_routes.router_for', return_value=Router(self.onto, self.convs, self.cid)):
            with self.assertRaises(HTTPException):
                resume(self.cid, Resume(task='charter_draft'), None)
        self.assertEqual(self.online.requests, [])

    def test_manual_generation_after_confirmation_does_not_publish_until_reviewed(self):
        original = growth.create_charter(growth.CharterCreate(goals=['原目标']))
        self.seed('我明确想把目标改为多陪家人')
        draft = self.generate()
        self.assertIn('goals', draft['fields'])
        self.assertEqual(GrowthStore.instance().current_charter(), original)
        self.assertEqual(self.accept(draft)['charter']['version'], 2)

    def test_pending_fields_from_several_turns_form_one_batch(self):
        self.seed('我是一个教师', 'roles'); old = self.generate()
        self.seed(); newest = self.generate('synthetic-newest-001')
        self.assertEqual(set(newest['fields']), {'roles', 'goals'})
        self.assertEqual(CharterDraftStore().get(old['id'])['fields']['roles']['status'], 'superseded')
        saved = CharterDraftStore().act(newest['id'], scope='global', cid=self.cid, revision=1,
            selections={f: e['text'] for f, e in newest['fields'].items()}, skip=[], request_id='all-fields-001')
        self.assertEqual(saved['charter']['version'], 1)
        self.assertEqual(saved['charter']['roles'], ['我是一个教师'])

    def test_explicit_replace_preserves_unrelated_list_items(self):
        growth.create_charter(growth.CharterCreate(goals=['旧目标', '保留的目标']))
        self.seed(); draft = self.generate()
        result = CharterDraftStore().act(draft['id'], scope='global', cid=self.cid, revision=1,
            selections={'goals': '新目标'}, replacements={'goals': '旧目标'}, skip=[], request_id='replace-one-001')
        self.assertEqual(result['charter']['goals'], ['保留的目标', '新目标'])
        self.assertEqual(GrowthStore.instance().list_charters()[1]['goals'], ['旧目标', '保留的目标'])

    def test_changed_source_after_generation_blocks_http_confirmation(self):
        self.client.app.include_router(build_router())
        m = self.seed(); draft = self.generate()
        self.convs.update_message(m['id'], content='新的内容')
        response = self.client.post(self.url + '/charter/' + draft['id'] + '/review', json={
            'revision': 1, 'selections': {'goals': draft['fields']['goals']['text']}, 'requestId': 'source-changed-001'})
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(GrowthStore.instance().current_charter())

    def test_charter_grant_cannot_override_source_file_grant(self):
        c = GrowthStore.instance().create_charter({'goals': ['合成文件里的目标'], 'metadata': {'scope': 'global', 'fields': {
            'goals': {'sources': [{'kind': 'material', 'id': 'synthetic-file', 'materialVersion': 1}]}}}})
        self.enable(); r = Router(self.onto, self.convs, self.cid)
        with patch('mindos.zhijun.routing.read_ref', return_value=({'fileName': '合成资料.txt'}, {'snapshot_id': 'snapshot-1'}, '合成文件正文')):
            ref = r.ref('charter', c['id'] + ':goals')
            closure = r.resolve(ref)
            self.store.grant('global', [closure[0]], service_info(self.online)['id'], 'chat')
            req = ChatRequest(system='合成文件里的目标', messages=[])
            preview = r.prepare('chat', req, [ref], self.online)
            self.assertIn('material:synthetic-file', preview['missing'])
            with self.assertRaises(HTTPException):
                list(GuardedProvider(r, self.online, 'chat', [ref], revision=preview['revision']).stream(req))
            self.assertEqual(self.online.requests, [])


if __name__ == '__main__': unittest.main()
