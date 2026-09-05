"""Exact Markdown charter editing/publication; synthetic sources and providers."""
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests import test_charter_workspace as fixture
from tests import test_task_routing as routing_fixture
from mindos.stores.charter_draft_store import CharterDraftStore, derive_document_clauses
from mindos.stores.growth_store import GrowthStore, GrowthConflictError
from mindos.zhijun.charter import build_router, DraftRequest, generate_workspace
from mindos.zhijun.routing import Router, GuardedProvider, prepare_chat
from mindos.zhijun.provider import ChatRequest


DOCUMENT = "\n# 我的生活章程\n\n  我希望留出安静的时间。  \n\n## 合作方式\n\n- 先听完我的问题\n- 不确定时直接说明\n\n```text\n保留代码块的空行\n\n与缩进\n```\n\n| 范围 | 约定 |\n| --- | --- |\n| 周末 | 慢一点 |\n\n"


class MarkdownStoreTests(unittest.TestCase):
    setUp = fixture.WorkspaceStoreTests.setUp
    tearDown = fixture.WorkspaceStoreTests.tearDown
    start = fixture.WorkspaceStoreTests.start
    edit = fixture.WorkspaceStoreTests.edit
    generated = fixture.WorkspaceStoreTests.generated

    def publish(self, ws, request="markdown-publish-001", confirm=False):
        return self.store.workspace_action(ws["id"], scope=self.scope, cid=self.cid,
            revision=ws["revision"], request_id=request, action="publish", publish_document=True,
            confirm_control_changes=confirm)

    def test_edit_publish_restart_keep_exact_body_and_separate_source_text(self):
        ws = self.edit(self.start(), source_text="未决定是否纳入的旧原始想法")
        before_revision = ws["revision"]
        ws = self.edit(ws, document=DOCUMENT)
        self.assertEqual(ws["document"], DOCUMENT)
        self.assertEqual(ws["documentFormat"], "markdown")
        self.assertEqual(ws["sourceText"], "未决定是否纳入的旧原始想法")
        self.assertEqual(self.store.get_workspace_revision(ws["id"], before_revision)["document"], "")
        published = self.publish(ws)
        self.assertEqual(published, self.publish(ws))
        reopened = GrowthStore(self.growth._db_path)
        self.assertEqual(reopened.current_charter()["document"], DOCUMENT)
        self.assertEqual(reopened.get_charter(published["charter"]["id"])["document"], DOCUMENT)
        self.assertEqual(len(reopened.list_charters()), 1)
        self.assertNotIn("旧原始想法", published["charter"]["document"])
        next_ws = self.start("markdown-new-session")
        self.assertEqual(next_ws["document"], DOCUMENT)
        self.assertEqual(next_ws["documentFormat"], "markdown")

    def test_blank_can_be_saved_but_not_published_and_long_paragraph_is_supported(self):
        ws = self.edit(self.start(), document=" \n\t")
        with self.assertRaises(ValueError):
            self.publish(ws)
        self.assertIsNone(self.growth.current_charter())
        ws = self.edit(ws, document="字" * 30000)
        self.assertEqual(self.publish(ws)["charter"]["document"], "字" * 30000)
        next_ws = self.start("long-new-session")
        with self.assertRaises(ValueError):
            self.edit(next_ws, document="字" * 30001)

    def test_unchanged_control_and_aspiration_keep_metadata_but_rewrite_removes_control(self):
        original = fixture.clause("private", "只在本地处理我的资料", kind="boundary", control="local_only")
        wish = fixture.clause("wish", "我希望慢一点生活", kind="aspiration", scope="contextual", context="周末")
        before = self.edit(self.start(), clauses=[original, wish])
        self.store.workspace_action(before["id"], scope=self.scope, cid=self.cid, revision=before["revision"],
            request_id="legacy-initial-publish", action="publish", selected_ids=["private", "wish"])
        ws = self.start("markdown-convert-start")
        ws = self.edit(ws, document=ws["document"] + "\n\n## 对话方式\n\n我想先表达完整再讨论。\n")
        by_id = {c["id"]: c for c in ws["clauses"]}
        self.assertEqual(by_id["private"]["control"], "local_only")
        self.assertEqual(by_id["wish"]["kind"], "aspiration")
        self.assertEqual(by_id["wish"]["context"], "周末")
        self.assertTrue(by_id["private"]["sources"])
        ws = self.edit(ws, document="# 新约定\n\n可以先问我是否需要在线处理。\n")
        self.assertEqual(ws["controlChanges"][0]["control"], "local_only")
        with self.assertRaises(ValueError):
            self.publish(ws)
        published = self.publish(ws, confirm=True)["charter"]
        self.assertTrue(all(c.get("control") is None for c in published["clauses"]))
        self.assertTrue(published["metadata"]["sources"], "manual rewriting must retain original ancestry")

    def test_new_prose_and_code_examples_never_enable_a_program_control(self):
        previous = [fixture.clause("private", "只在本地处理我的资料", kind="boundary", control="local_only")]
        for text in ("```text\n只在本地处理我的资料\n```", "> 只在本地处理我的资料", "不要继续要求：只在本地处理我的资料"):
            with self.subTest(text=text):
                clauses = derive_document_clauses(text, previous, [])
                self.assertTrue(all(c.get("control") is None for c in clauses))
        handwritten = derive_document_clauses("只在本地处理我的资料", [], [])
        self.assertEqual(handwritten[0]["kind"], "preference")
        self.assertIsNone(handwritten[0]["control"])

    def test_moved_to_cancelled_or_new_context_section_does_not_keep_old_control(self):
        previous = [fixture.clause("old", "不主动提醒", section="我的约定", kind="boundary", control="no_proactive")]
        original = derive_document_clauses("## 我的约定\n\n不主动提醒", previous, [])
        self.assertTrue(any(c.get("control") == "no_proactive" for c in original))
        for section in ("过去的限制（现已取消）", "只有旅行时适用"):
            with self.subTest(section=section):
                moved = derive_document_clauses("## " + section + "\n\n不主动提醒", previous, [])
                self.assertTrue(all(c.get("control") is None for c in moved))
        nested = derive_document_clauses("# 过去已取消的约定\n\n## 我的约定\n\n不主动提醒", previous, [])
        self.assertTrue(all(c.get("control") is None for c in nested))
        contextual = derive_document_clauses("## 我的约定\n\n以下旧约定已经取消：\n\n不主动提醒", previous, [])
        self.assertTrue(all(c.get("control") is None for c in contextual))
        preamble = derive_document_clauses("以下旧约定已取消\n\n## 我的约定\n\n不主动提醒", previous, [])
        self.assertTrue(all(c.get("control") is None for c in preamble))

    def test_legacy_pending_suggestion_cannot_silently_merge_after_markdown_conversion(self):
        original = self.start()
        legacy = self.edit(original, source_text="旧版原始想法")
        legacy = self.generated(original)
        self.assertEqual(legacy["suggestions"][0]["documentFormat"], "clauses")
        ws = self.edit(legacy, document=DOCUMENT)
        with self.assertRaises(GrowthConflictError):
            self.store.workspace_action(ws["id"], scope=self.scope, cid=self.cid,
                revision=ws["revision"], request_id="legacy-suggestion-merge", action="merge",
                suggestion_id=ws["suggestions"][0]["id"])
        self.assertEqual(self.store.get_workspace(ws["id"])["document"], DOCUMENT)

    def test_manual_body_is_not_overwritten_and_pending_is_explicit_whole_document(self):
        original = self.start()
        edited = self.edit(original, document=DOCUMENT)
        latest = self.generated(original)
        self.assertEqual(latest["document"], DOCUMENT)
        self.assertEqual(latest["manualRevision"], edited["manualRevision"])
        suggestion = latest["suggestions"][0]
        self.assertIn("我希望为家人留出时间", suggestion["document"])
        self.assertIn("保留代码块", suggestion["document"])
        merged = self.store.workspace_action(latest["id"], scope=self.scope, cid=self.cid,
            revision=latest["revision"], request_id="markdown-merge-001", action="merge", suggestion_id=suggestion["id"])["workspace"]
        self.assertEqual(merged["document"], suggestion["document"])
        self.assertIsNone(self.growth.current_charter())
        self.publish(merged)
        with self.assertRaises(GrowthConflictError):
            self.generated(original, request="late-generation")

    def test_ai_proposed_control_is_not_silently_activated_by_full_document_confirmation(self):
        ws = self.generated(self.start(), clauses=[fixture.clause("private", "只在本地处理我的资料",
            kind="boundary", control="local_only")])
        self.assertEqual(ws["clauses"][0]["control"], "local_only", "legacy proposal is still pending")
        ws = self.edit(ws, document=ws["document"])
        self.assertTrue(all(c.get("control") is None for c in ws["clauses"]))
        published = self.publish(ws)["charter"]
        self.assertTrue(all(c.get("control") is None for c in published["clauses"]))

    def test_document_edit_cas_and_legacy_writes_cannot_overwrite_markdown(self):
        initial = self.start()
        ws = self.edit(initial, document=DOCUMENT)
        self.assertEqual(ws, self.edit(initial, document=DOCUMENT))
        with self.assertRaises(GrowthConflictError):
            self.store.edit_workspace(ws["id"], scope=self.scope, cid=self.cid, revision=1,
                request_id="stale-markdown-edit", document="旧正文")
        with self.assertRaises(GrowthConflictError):
            self.edit(ws, clauses=[fixture.clause()])
        with self.assertRaises(GrowthConflictError):
            self.store.workspace_action(ws["id"], scope=self.scope, cid=self.cid, revision=ws["revision"],
                request_id="legacy-publish", action="publish", selected_ids=[ws["clauses"][0]["id"]])
        self.publish(ws)
        with self.assertRaises(GrowthConflictError):
            self.growth.create_charter({"goals": ["旧版表单改写"]})
        self.assertEqual(self.growth.current_charter()["document"], DOCUMENT)


class MarkdownApiTests(unittest.TestCase):
    setUp = routing_fixture.RoutingTests.setUp
    tearDown = routing_fixture.RoutingTests.tearDown
    enable = routing_fixture.RoutingTests.enable
    grant = routing_fixture.RoutingTests.grant

    def client_for_charter(self):
        app = FastAPI()
        app.include_router(build_router())
        return TestClient(app), self.url + "/charter"

    def test_no_model_handwritten_api_publish_and_hidden_guidance_are_used(self):
        client, base = self.client_for_charter()
        ws = client.post(base + "/workspace/start", json={"requestId": "markdown-start-api"}).json()["workspace"]
        edited = client.put(base + "/workspace/" + ws["id"], json={"revision": ws["revision"],
            "requestId": "markdown-edit-api", "document": DOCUMENT})
        self.assertEqual(edited.status_code, 200, edited.text)
        ws = edited.json()["workspace"]
        self.assertEqual(ws["document"], DOCUMENT)
        response = client.post(base + "/workspace/" + ws["id"] + "/publish", json={"revision": ws["revision"],
            "requestId": "markdown-publish-api", "publishDocument": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["charter"]["document"], DOCUMENT)
        self.assertEqual(self.local.requests, [])
        self.assertEqual(self.online.requests, [])
        router = Router(self.onto, self.convs, self.cid)
        plan = prepare_chat(router, "今天聊什么？")
        self.assertIn("不确定时直接说明", plan.preview["request"]["system"])
        self.assertTrue(any(s["kind"] == "charter_clause" for s in plan.preview["sources"]))

    def test_markdown_workspace_requires_separate_external_authorization(self):
        self.enable()
        store = CharterDraftStore()
        ws = store.start_workspace(self.cid, "global", "markdown-private-start")["workspace"]
        ws = store.edit_workspace(ws["id"], scope="global", cid=self.cid, revision=1,
            request_id="markdown-private-edit", document=DOCUMENT)["workspace"]
        router = Router(self.onto, self.convs, self.cid)
        req = DraftRequest(requestId="markdown-private-generate", previewOnly=True)
        preview = generate_workspace(router, ws["id"], req)["routePreview"]
        self.assertTrue(preview["missing"])
        self.assertIn(DOCUMENT, preview["request"]["messages"][0]["content"].replace("\\n", "\n"))
        self.assertEqual(self.online.requests, [])
        refs = router.resolve(router.ref("charter_workspace", ws["id"] + ":" + str(ws["revision"])))
        self.assertIn("document", refs[0]["text"])
        self.assertIn("我的生活章程", refs[0]["text"])

    def test_charter_chat_reads_manual_draft_only_after_authorization_and_rechecks_edits(self):
        store = CharterDraftStore()
        ws = store.start_workspace(self.cid, "global", "chat-draft-start")["workspace"]
        ws = store.edit_workspace(ws["id"], scope="global", cid=self.cid, revision=1,
            request_id="chat-draft-edit", document=DOCUMENT)["workspace"]
        local_plan = prepare_chat(Router(self.onto, self.convs, self.cid), "请帮我完善这份章程")
        self.assertIn(DOCUMENT, local_plan.preview["request"]["system"])
        self.assertTrue(any(ref["kind"] == "charter_workspace" for ref in local_plan.refs))
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        plan = prepare_chat(router, "请帮我完善这份章程")
        self.assertTrue(plan.preview["missing"])
        request = ChatRequest(**plan.preview["request"])
        with self.assertRaises(HTTPException):
            list(GuardedProvider(router, self.online, "chat", plan.refs, revision=plan.preview["revision"]).stream(request))
        self.assertEqual(self.online.requests, [])
        self.grant(plan.preview)
        allowed = prepare_chat(router, "请帮我完善这份章程")
        self.assertEqual(allowed.preview["missing"], [])
        store.edit_workspace(ws["id"], scope="global", cid=self.cid, revision=ws["revision"],
            request_id="chat-draft-changed", document=DOCUMENT + "修改：先讨论生活。")
        with self.assertRaises(HTTPException):
            list(GuardedProvider(router, self.online, "chat", allowed.refs, revision=allowed.preview["revision"]).stream(
                ChatRequest(**allowed.preview["request"])))
        self.assertEqual(self.online.requests, [])

    def test_omit_draft_does_not_send_markdown_or_claim_it_was_read(self):
        self.enable()
        store = CharterDraftStore()
        ws = store.start_workspace(self.cid, "global", "omit-draft-start")["workspace"]
        store.edit_workspace(ws["id"], scope="global", cid=self.cid, revision=1,
            request_id="omit-draft-edit", document=DOCUMENT)
        plan = prepare_chat(Router(self.onto, self.convs, self.cid), "请帮我完善章程", omit=True)
        self.assertNotIn(DOCUMENT, plan.preview["request"]["system"])
        self.assertFalse(any(ref["kind"] == "charter_workspace" for ref in plan.refs))

    def test_full_publication_checks_removed_source_ancestry(self):
        client, base = self.client_for_charter()
        store = CharterDraftStore()
        ws = store.start_workspace(self.cid, "global", "markdown-lineage-start")["workspace"]
        source = self.convs.append_message(self.cid, "user", "希望为家人留出时间", meta={"routingSources": []})
        router = Router(self.onto, self.convs, self.cid)
        ref = router.resolve(router.ref("message", source["id"]))[0]["ref"]
        ws = store.apply_generated(ws["id"], scope="global", cid=self.cid, generation=1, source_revision=1,
            manual_revision=0, base_version=0, clauses=[fixture.clause()], sources=[ref],
            context_revision="source-context", request_id="markdown-lineage-generate")["workspace"]
        ws = store.edit_workspace(ws["id"], scope="global", cid=self.cid, revision=ws["revision"],
            request_id="markdown-lineage-edit", document="# 新的正文\n\n我希望慢慢探索。")["workspace"]
        self.convs.update_message(source["id"], content="已改变的原话")
        published = client.post(base + "/workspace/" + ws["id"] + "/publish", json={"revision": ws["revision"],
            "requestId": "markdown-lineage-publish", "publishDocument": True})
        self.assertEqual(published.status_code, 409, published.text)
        self.assertIsNone(GrowthStore.instance().current_charter())

    def test_changed_publish_mode_cannot_reuse_old_nonce(self):
        client, base = self.client_for_charter()
        ws = client.post(base + "/workspace/start", json={"requestId": "markdown-nonce-start"}).json()["workspace"]
        ws = client.put(base + "/workspace/" + ws["id"], json={"revision": ws["revision"],
            "requestId": "markdown-nonce-edit", "document": DOCUMENT}).json()["workspace"]
        payload = {"revision": ws["revision"], "requestId": "markdown-nonce-publish", "publishDocument": True}
        published = client.post(base + "/workspace/" + ws["id"] + "/publish", json=payload)
        self.assertEqual(published.status_code, 200, published.text)
        repeated = client.post(base + "/workspace/" + ws["id"] + "/publish", json=payload)
        self.assertEqual(repeated.json(), published.json())
        changed = client.post(base + "/workspace/" + ws["id"] + "/publish", json={**payload, "publishDocument": False})
        self.assertEqual(changed.status_code, 409, changed.text)

    def test_changed_automatic_limit_requires_explicit_confirmation_before_publication(self):
        client, base = self.client_for_charter()
        store = CharterDraftStore()
        ws = store.start_workspace(self.cid, "global", "rules-old-start")["workspace"]
        ws = store.edit_workspace(ws["id"], scope="global", cid=self.cid, revision=1, request_id="rules-old-edit",
            clauses=[fixture.clause("private", "只在本地处理我的资料", section="我的约定", kind="boundary", control="local_only")])["workspace"]
        original = store.workspace_action(ws["id"], scope="global", cid=self.cid, revision=ws["revision"],
            request_id="rules-old-publish", action="publish", selected_ids=["private"])["charter"]
        ws = store.start_workspace(self.cid, "global", "rules-markdown-start")["workspace"]
        changed = client.put(base + "/workspace/" + ws["id"], json={"revision": ws["revision"],
            "requestId": "rules-markdown-edit", "document": "## 我的约定\n\n以下旧约定已经取消：\n\n只在本地处理我的资料"})
        self.assertEqual(changed.status_code, 200, changed.text)
        ws = changed.json()["workspace"]
        self.assertEqual(ws["controlChanges"], [{"id": "private", "text": "只在本地处理我的资料", "control": "local_only"}])
        payload = {"revision": ws["revision"], "requestId": "rules-publish-attempt", "publishDocument": True}
        blocked = client.post(base + "/workspace/" + ws["id"] + "/publish", json=payload)
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertIn("请确认规则变化", blocked.text)
        self.assertEqual(GrowthStore.instance().current_charter()["id"], original["id"])
        confirmed = client.post(base + "/workspace/" + ws["id"] + "/publish", json={**payload, "confirmControlChanges": True})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertTrue(all(c.get("control") is None for c in confirmed.json()["charter"]["clauses"]))
        replay = client.post(base + "/workspace/" + ws["id"] + "/publish", json={**payload, "confirmControlChanges": True})
        self.assertEqual(replay.json(), confirmed.json())
        wrong_mode = client.post(base + "/workspace/" + ws["id"] + "/publish", json=payload)
        self.assertEqual(wrong_mode.status_code, 409)
