"""Isolated guided-charter workspaces, CAS and immutable publication checks."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mindos.stores.growth_store import GrowthStore, GrowthConflictError
from mindos.stores.charter_draft_store import CharterDraftStore, render_document
from mindos.zhijun.charter import DraftRequest, generate_workspace, enqueue, run_job, build_router, workspace_context, workspace_topic_progress
from mindos.zhijun.routing import Router
from tests.test_task_routing import RoutingTests


def clause(ident="care", text="我希望为家人留出时间", **extra):
    return {"id": ident, "section": "生活与关系", "text": text, "kind": "aspiration",
            "scope": "global", "sources": [], "quote": text, **extra}


class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.growth = GrowthStore(Path(self.tmp.name) / "growth.db")
        self.store = CharterDraftStore(self.growth)
        self.cid, self.scope = "synthetic-conversation", "global"

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, request="start-001"):
        return self.store.start_workspace(self.cid, self.scope, request)["workspace"]

    def edit(self, ws, **kwargs):
        return self.store.edit_workspace(ws["id"], scope=self.scope, cid=self.cid,
            revision=ws["revision"], request_id="edit-" + ws["id"] + str(ws["revision"]), **kwargs)["workspace"]

    def publish(self, ws, selected=None, request="publish-001"):
        return self.store.workspace_action(ws["id"], scope=self.scope, cid=self.cid,
            revision=ws["revision"], request_id=request, action="publish", selected_ids=selected or ["care"])

    def generated(self, ws, request="generated-001", **kwargs):
        params = dict(scope=self.scope, cid=self.cid, generation=ws["generation"],
            source_revision=ws["revision"], manual_revision=ws["manualRevision"], base_version=ws["baseVersion"],
            clauses=[clause()], sources=[{"kind": "message", "id": "synthetic-user"}],
            context_revision="context-1", request_id=request)
        params.update(kwargs)
        return self.store.apply_generated(ws["id"], **params)["workspace"]

    def test_start_explicit_idempotent_single_active_per_device(self):
        self.assertIsNone(self.store.latest_workspace("global"))
        first = self.start()
        again = self.store.start_workspace(self.cid, "global", "start-001", start_seq=99)["workspace"]
        self.assertEqual(first, again)
        other = self.store.start_workspace("other-cid", "global", "start-other")
        self.assertEqual(other["conversationId"], self.cid)
        self.assertEqual(other["workspace"]["id"], first["id"])
        self.assertIsNone(self.growth.current_charter())

    def test_manual_free_document_custom_sections_publish_only_selected(self):
        ws = self.edit(self.start(), source_text="原文里还有没决定的内容", clauses=[clause(), clause("work", "我希望慢一点创业", section="自己的节奏")])
        result = self.publish(ws)
        saved = result["charter"]
        self.assertIn("生活与关系", saved["document"])
        self.assertNotIn("创业", saved["document"])
        self.assertNotIn("原文", saved["document"])
        self.assertEqual(saved["document"], render_document(saved["clauses"]))
        self.assertEqual(saved["vision"], "")
        self.assertEqual(result["workspace"]["status"], "published")
        self.assertIsNone(self.store.active_workspace(self.cid, "global"))
        self.assertEqual(result, self.publish(ws))
        self.assertEqual(len(self.growth.list_charters()), 1)

    def test_empty_formal_not_published(self):
        ws = self.start()
        with self.assertRaises(ValueError): self.publish(ws)
        self.assertIsNone(self.growth.current_charter())

    def test_revision_cas_preserves_first_manual_edit(self):
        ws = self.start()
        edited = self.edit(ws, source_text="我的原文")
        with self.assertRaises(GrowthConflictError):
            self.store.edit_workspace(ws["id"], scope="global", cid=self.cid, revision=ws["revision"],
                request_id="different-edit", source_text="过期内容")
        self.assertEqual(self.store.get_workspace(ws["id"])["sourceText"], "我的原文")
        self.assertEqual(edited, self.edit(ws, source_text="我的原文"))

    def test_two_store_instances_cannot_both_save_same_revision(self):
        ws = self.start()
        other = CharterDraftStore(GrowthStore(self.growth._db_path))
        gate = threading.Barrier(2)
        results = []
        def save(store, text):
            gate.wait(timeout=3)
            try:
                store.edit_workspace(ws["id"], scope="global", cid=self.cid, revision=1,
                    request_id="concurrent-" + text, source_text=text)
                results.append("saved")
            except GrowthConflictError:
                results.append("conflict")
        threads = [threading.Thread(target=save, args=(store, text)) for store, text in ((self.store, "one"), (other, "two"))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertEqual(sorted(results), ["conflict", "saved"])
        self.assertEqual(self.store.get_workspace(ws["id"])["revision"], 2)

    def test_concurrent_manual_edit_moves_ai_result_to_suggestion(self):
        before = self.start()
        edited = self.edit(before, clauses=[clause(text="手动写的愿望")])
        latest = self.generated(before)
        self.assertEqual(latest["clauses"][0]["text"], "手动写的愿望")
        self.assertEqual(len(latest["suggestions"]), 1)
        self.assertEqual(latest["manualRevision"], edited["manualRevision"])
        suggestion = latest["suggestions"][0]
        merged = self.store.workspace_action(latest["id"], scope="global", cid=self.cid,
            revision=latest["revision"], request_id="merge-001", action="merge", suggestion_id=suggestion["id"])["workspace"]
        self.assertEqual(merged["clauses"][0]["text"], clause()["text"])
        self.assertIsNone(self.growth.current_charter())

    def test_generated_draft_does_not_publish_and_retries_are_one_revision(self):
        ws = self.start()
        output = self.generated(ws)
        self.assertEqual(output["clauses"][0]["id"], "care")
        self.assertEqual(output, self.generated(ws))
        self.assertIsNone(self.growth.current_charter())

    def test_incremental_draft_does_not_drop_omitted_old_clauses(self):
        ws = self.generated(self.start())
        next_ws = self.generated(ws, request="generated-002", clauses=[clause("new", "我想慢慢探索")])
        self.assertEqual([c["id"] for c in next_ws["clauses"]], ["care", "new"])

    def test_closed_and_superseded_generation_cannot_overwrite(self):
        ws = self.start()
        ready = self.generated(ws)
        self.publish(ready)
        with self.assertRaises(GrowthConflictError): self.generated(ws, request="late-generation")
        self.assertEqual(self.growth.current_charter()["version"], 1)

    def test_pause_resume_keeps_text_and_invalidates_inflight_generation(self):
        ws = self.edit(self.start(), source_text="保留这段原文")
        self.store.workspace_action(ws["id"], scope="global", cid=self.cid,
            revision=ws["revision"], request_id="pause-001", action="pause")
        resumed = self.start("resume-001")
        self.assertEqual(resumed["id"], ws["id"])
        self.assertEqual(resumed["sourceText"], "保留这段原文")
        self.assertGreater(resumed["generation"], ws["generation"])
        with self.assertRaises(GrowthConflictError): self.generated(ws)

    def test_restart_keeps_immutable_input_snapshots(self):
        ws = self.start()
        self.edit(ws, source_text="后来写的")
        reopened = CharterDraftStore(GrowthStore(self.growth._db_path))
        self.assertEqual(reopened.get_workspace_revision(ws["id"], 1)["sourceText"], "")
        self.assertEqual(reopened.get_workspace(ws["id"])["sourceText"], "后来写的")

    def test_manual_edit_retains_actual_ancestry_ignores_client_refs(self):
        ws = self.generated(self.start())
        changed = self.edit(ws, source_text="改写原文", clauses=[clause("replacement", sources=[{"kind": "message", "id": "forged"}])])
        self.assertEqual(changed["clauses"][0]["sources"], [{"kind": "message", "id": "synthetic-user"}])

    def test_scope_specific_current_and_cas_no_global_fallback(self):
        a = self.growth.create_charter({"goals": ["甲的目标"], "expectedVersion": 0, "metadata": {"scope": "a"}})
        b = self.growth.create_charter({"goals": ["乙的目标"], "expectedVersion": 0, "metadata": {"scope": "b"}})
        self.assertIsNone(self.growth.current_charter("global"))
        self.assertEqual(self.growth.current_charter("a")["id"], a["id"])
        self.assertEqual(self.growth.current_charter("b")["id"], b["id"])
        self.assertEqual(len(self.growth.charter_history("a")["versions"]), 1)
        with self.assertRaises(GrowthConflictError):
            self.store.edit_workspace(self.start()["id"], scope="a", cid=self.cid,
                revision=1, request_id="cross-device", source_text="不能写入")

    def test_legacy_api_cannot_overwrite_full_document(self):
        ws = self.edit(self.start(), clauses=[clause()])
        self.publish(ws)
        with self.assertRaises(GrowthConflictError):
            self.growth.create_charter({"goals": ["从旧表单覆盖"], "expectedVersion": 1})
        self.assertEqual(self.growth.current_charter()["document"], render_document([clause()]))

    def test_partial_new_version_keeps_unselected_standing_clauses(self):
        first = self.edit(self.start(), clauses=[clause(), clause("work", "原本的工作方向")])
        self.publish(first, selected=["care", "work"])
        second = self.edit(self.start("start-second"), clauses=[clause(text="尚未确认的改变"), clause("work", "原本的工作方向"), clause("new", "新的愿望")])
        saved = self.publish(second, selected=["new"], request="publish-second")["charter"]
        self.assertEqual([c["id"] for c in saved["clauses"]], ["care", "work", "new"])
        self.assertIn("我希望为家人留出时间", saved["document"])
        self.assertNotIn("尚未确认的改变", saved["document"])

    def test_only_explicit_manual_deletion_removes_standing_clause(self):
        first = self.edit(self.start(), clauses=[clause(), clause("work", "原工作方向")])
        self.publish(first, selected=["care", "work"])
        second = self.edit(self.start("start-second"), clauses=[clause("work", "更新后的工作方向")])
        self.assertEqual(second["deletedClauseIds"], ["care"])
        saved = self.publish(second, selected=["work"], request="publish-second")["charter"]
        self.assertEqual([c["id"] for c in saved["clauses"]], ["work"])

    def test_unknown_control_and_unscoped_context_rejected(self):
        ws = self.start()
        for value in (clause(control="delete_everything"), clause(scope="contextual")):
            with self.assertRaises(ValueError): self.edit(ws, clauses=[value])

    def test_ambiguity_must_be_explicitly_resolved_before_publish(self):
        ws = self.edit(self.start(), clauses=[clause(clarification="这里指哪些情境？")])
        with self.assertRaises(ValueError): self.publish(ws)
        resolved = self.edit(ws, clauses=[clause(clarification=None)])
        self.assertEqual(len(self.publish(resolved)["charter"]["clauses"]), 1)

    def test_delete_only_publish_keeps_other_existing_clause(self):
        first = self.edit(self.start(), clauses=[clause(), clause("work", "工作愿望")])
        self.publish(first, selected=["care", "work"])
        second = self.edit(self.start("new-session"), clauses=[clause("work", "工作愿望")])
        result = self.store.workspace_action(second["id"], scope="global", cid=self.cid,
            revision=second["revision"], request_id="delete-only", action="publish", selected_ids=[])
        self.assertEqual([c["id"] for c in result["charter"]["clauses"]], ["work"])

    def test_new_formal_version_blocks_old_workspace_without_erasing_it(self):
        ws = self.edit(self.start(), clauses=[clause()])
        self.growth.create_charter({"goals": ["旧界面新确认"], "expectedVersion": 0})
        with self.assertRaises(GrowthConflictError): self.publish(ws)
        self.assertEqual(self.store.get_workspace(ws["id"])["status"], "active")

    def test_decision_binds_actual_historical_or_no_charter_not_new_current(self):
        first = self.growth.create_charter({"goals": ["旧目标"]})
        second = self.growth.create_charter({"goals": ["新目标"]})
        payload = {"title": "合成决定", "context": "合成情境", "options": ["慢一点"], "choice": "慢一点",
            "rationale": "合成依据", "confidence": 50, "expectedOutcome": "观察变化", "reviewAt": None,
            "relatedEntityIds": [], "evidenceRefs": [], "scope": "global"}
        old = self.growth.create_decision({**payload, "charterBasis": {"charterId": first["id"], "version": first["version"], "scope": "global"}})
        self.assertEqual(old["charterVersion"], first["version"])
        none = self.growth.create_decision({**payload, "charterBasis": {"charterId": None, "version": 0, "scope": "global"}})
        self.assertIsNone(none["charterId"])
        current = self.growth.create_decision(payload)
        self.assertEqual(current["charterId"], second["id"])
        with self.assertRaises(GrowthConflictError):
            self.growth.create_decision({**payload, "scope": "another-device", "charterBasis": {"charterId": first["id"], "version": first["version"]}})


class WorkspaceGenerationTests(unittest.TestCase):
    setUp = RoutingTests.setUp
    tearDown = RoutingTests.tearDown
    enable = RoutingTests.enable
    grant = RoutingTests.grant

    def start(self):
        return CharterDraftStore().start_workspace(self.cid, "global", "workspace-start")["workspace"]

    def seed(self):
        m = self.convs.append_message(self.cid, "user", "我希望为家人留出时间", meta={"routingSources": []})
        self.local.result = self.online.result = {"clauses": [{**clause(), "sourceId": m["id"]}]}
        return m

    def test_no_background_generation_without_explicit_workspace(self):
        m = self.seed()
        self.assertIsNone(enqueue(self.cid, m["id"], m["content"], ontology=self.onto))
        self.assertEqual(run_job({"conversationId": self.cid, "messageId": m["id"]}, self.onto, self.convs)["state"], "skipped")
        self.assertEqual(self.local.requests, [])

    def test_guidance_start_template_is_not_an_answer(self):
        self.start()
        r = Router(self.onto, self.convs, self.cid)
        template = "我想通过对话建立人生章程。请从眼下重要的方向和合作方式开始，一次只聊一个主题，不要求我先想清楚全部。"
        instruction, topic = workspace_context(r, template)
        self.assertEqual(topic, "life")
        self.assertIn("怎样的生活", instruction)
        self.assertTrue(all(value == "pending" for value in workspace_topic_progress([], template).values()))

    def test_guidance_remembers_skipped_topic_after_later_turn(self):
        self.start()
        self.convs.append_message(self.cid, "assistant", "你希望过怎样的生活？", meta={"charterTopic": "life"})
        self.convs.append_message(self.cid, "user", "我还没想清楚，先跳过")
        self.convs.append_message(self.cid, "assistant", "眼下最想照顾什么？", meta={"charterTopic": "care"})
        self.convs.append_message(self.cid, "user", "先照顾好身体")
        instruction, topic = workspace_context(Router(self.onto, self.convs, self.cid), "然后再看看")
        self.assertEqual(topic, "principles")
        self.assertNotIn("尚未涉及时可轻问：你希望自己过怎样的生活", instruction)
        states = workspace_topic_progress(self.convs.list_messages(self.cid))
        self.assertEqual(states["life"], "skipped")
        self.assertEqual(states["care"], "discussed")

    def test_guidance_rephrase_does_not_advance_current_topic(self):
        messages = [{"role": "assistant", "content": "你希望怎样生活", "meta": {"charterTopic": "life"}}]
        states = workspace_topic_progress(messages, "请换个说法", {"kind": "control", "control": "rephrase"})
        self.assertEqual(states["life"], "pending")

    def test_queued_messages_coalesce_preserving_local_choice(self):
        ws = self.start(); m = self.seed()
        first = enqueue(self.cid, m["id"], m["content"], ontology=self.onto, local_only=True)
        m2 = self.convs.append_message(self.cid, "user", "也想照顾好健康")
        second = enqueue(self.cid, m2["id"], m2["content"], ontology=self.onto, local_only=True)
        self.assertEqual(first, second)
        with self.onto._connect() as db:
            rows = db.execute("SELECT * FROM ontology_jobs WHERE kind='charter_draft'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn(ws["id"], rows[0]["payload_json"])

    def test_local_generation_quotes_only_and_no_formal_or_ontology_write(self):
        ws = self.start(); self.seed()
        r = Router(self.onto, self.convs, self.cid)
        output = generate_workspace(r, ws["id"], DraftRequest(requestId="generate-local", localOnly=True))
        self.assertEqual(len(output["workspace"]["clauses"]), 1)
        self.assertIsNone(GrowthStore.instance().current_charter())
        self.assertEqual(self.onto.stats()["claims"]["confirmed"], 0)
        self.assertEqual(len(self.local.requests), 1)
        again = generate_workspace(r, ws["id"], DraftRequest(requestId="generate-local", localOnly=True))
        self.assertEqual(again, output)
        self.assertEqual(len(self.local.requests), 1)

    def test_unapproved_online_background_pauses_before_payload(self):
        self.enable(); ws = self.start(); self.seed()
        with self.assertRaises(HTTPException):
            generate_workspace(Router(self.onto, self.convs, self.cid), ws["id"], DraftRequest(requestId="background-new"), background=True)
        self.assertEqual(self.online.requests, [])
        self.assertTrue(self.store.pending(self.cid))

    def test_authorized_online_draft_and_immutable_snapshot_publish(self):
        self.enable(); ws = self.start(); self.seed()
        r = Router(self.onto, self.convs, self.cid)
        req = DraftRequest(requestId="authorized-generation")
        preview = generate_workspace(r, ws["id"], req.model_copy(update={"previewOnly": True}))["routePreview"]
        self.assertEqual(preview["purpose"], "charter_draft")
        self.grant(preview)
        preview = generate_workspace(r, ws["id"], req.model_copy(update={"previewOnly": True}))["routePreview"]
        result = generate_workspace(r, ws["id"], req.model_copy(update={"routeRevision": preview["revision"]}))
        self.assertEqual(len(self.online.requests), 1)
        self.assertIn("我希望为家人留出时间", self.online.requests[0].messages[0]["content"])
        app = FastAPI(); app.include_router(build_router()); client = TestClient(app)
        ready = result["workspace"]
        response = client.post(f"/api/mindos/conversations/{self.cid}/charter/workspace/{ws['id']}/publish",
            json={"revision": ready["revision"], "requestId": "publish-authorized", "selectedClauseIds": ["care"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["charter"]["metadata"]["sources"])

    def test_retained_legacy_sources_are_checked_even_when_only_new_clause_selected(self):
        other = self.convs.create_conversation()["id"]
        m = self.convs.append_message(other, "user", "我希望保留原有方向", meta={"routingSources": []})
        old = GrowthStore.instance().create_charter({"goals": ["原有方向"], "metadata": {"scope": "global",
            "fields": {"goals": {"state": "confirmed", "sources": [{"kind": "message", "id": m["id"]}]}}}})
        ws = self.start()
        ready = CharterDraftStore().apply_generated(ws["id"], scope="global", cid=self.cid, generation=ws["generation"],
            source_revision=ws["revision"], manual_revision=ws["manualRevision"], base_version=ws["baseVersion"],
            clauses=[clause("new", "新方向")], sources=[], context_revision="synthetic-new", request_id="generated-independent")["workspace"]
        self.convs.delete_conversation(other)
        app = FastAPI(); app.include_router(build_router()); client = TestClient(app)
        response = client.post(f"/api/mindos/conversations/{self.cid}/charter/workspace/{ws['id']}/publish",
            json={"revision": ready["revision"], "requestId": "publish-retained", "selectedClauseIds": ["new"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(GrowthStore.instance().current_charter()["id"], old["id"])
        self.assertEqual(CharterDraftStore().get_workspace(ws["id"])["status"], "active")

    def test_model_error_retains_raw_text_no_fallback(self):
        from mindos.zhijun.provider import ProviderError
        ws = self.start(); self.seed()
        ws = CharterDraftStore().edit_workspace(ws["id"], scope="global", cid=self.cid, revision=ws["revision"],
            request_id="before-error", source_text="必须保留的原文")["workspace"]
        self.local.error = ProviderError("合成超时")
        with self.assertRaises(HTTPException) as error:
            generate_workspace(Router(self.onto, self.convs, self.cid), ws["id"], DraftRequest(requestId="model-error", localOnly=True))
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(CharterDraftStore().get_workspace(ws["id"])["sourceText"], "必须保留的原文")
        self.assertEqual(self.online.requests, [])

    def test_model_finishing_after_publish_cannot_reopen_working_copy(self):
        ws = self.start(); self.seed()
        store = CharterDraftStore()
        def publish_during_request(req):
            edited = store.edit_workspace(ws["id"], scope="global", cid=self.cid, revision=ws["revision"], request_id="during-edit", clauses=[clause()])["workspace"]
            store.workspace_action(ws["id"], scope="global", cid=self.cid, revision=edited["revision"], request_id="during-publish", action="publish", selected_ids=["care"])
            return self.local.result
        with patch.object(self.local, "complete_json", side_effect=publish_during_request):
            with self.assertRaises(HTTPException):
                generate_workspace(Router(self.onto, self.convs, self.cid), ws["id"], DraftRequest(requestId="late-result", localOnly=True))
        self.assertEqual(store.get_workspace(ws["id"])["status"], "published")
        self.assertEqual(len(GrowthStore.instance().list_charters()), 1)

    def test_get_and_start_endpoints_do_not_publish_and_edit_uses_cas(self):
        app = FastAPI(); app.include_router(build_router())
        client = TestClient(app)
        base = f"/api/mindos/conversations/{self.cid}/charter"
        self.assertIsNone(client.get(base).json()["workspace"])
        response = client.post(base + "/workspace/start", json={"requestId": "endpoint-start"})
        self.assertEqual(response.status_code, 200)
        ws = response.json()["workspace"]
        body = {"revision": ws["revision"], "requestId": "endpoint-edit", "sourceText": "我的全文", "clauses": [clause()]}
        response = client.put(base + "/workspace/" + ws["id"], json=body)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(GrowthStore.instance().current_charter())
        response = client.put(base + "/workspace/" + ws["id"], json={**body, "requestId": "endpoint-stale", "sourceText": "旧修改"})
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
