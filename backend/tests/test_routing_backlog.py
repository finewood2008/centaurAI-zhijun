"""Scoped, revocable charter opt-in and lossless paused-turn recovery."""
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests import test_task_routing as harness
from mindos.stores.growth_store import GrowthStore
from mindos.stores.routing_store import RoutingStore
from mindos.zhijun import jobs
from mindos.zhijun.provider import ChatRequest
from mindos.zhijun.routing import Router, GuardedProvider, service_info
from fastapi import HTTPException


class BacklogTests(unittest.TestCase):
    setUp = harness.RoutingTests.setUp
    tearDown = harness.RoutingTests.tearDown
    enable = harness.RoutingTests.enable
    default_consent = harness.RoutingTests.default_consent

    def message(self, cid=None, text="我目前长期负责合成项目研发，希望先理清安排。"):
        return self.convs.append_message(cid or self.cid, "user", text, meta={"routingSources": []})

    def pause(self, message, *, reason="consent_required", preview_id="", owner=None):
        jid = self.onto.enqueue_job("extract_turn", owner or message["id"], payload={"conversationId": message["conversationId"], "messageId": message["id"]})
        job = self.onto.claim_next_job("synthetic-worker")
        self.assertEqual(job["jobId"], jid)
        self.onto.finish_job(jid, "synthetic-worker", result={"state": "paused", "reason": reason, "previewId": preview_id})
        return jid

    def routing_state(self):
        response = self.client.get(self.url + "/routing")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def resume(self, **extra):
        response = self.client.post(self.url + "/routing/resume", json={"task": "extract_turn", **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_existing_policy_migrates_charter_off_without_changing_grants_or_revision(self):
        # Recreate the pre-feature shape in this synthetic database only.
        with self.onto._connect() as db:
            db.execute("DROP TABLE routing_auto_consent")
            db.execute("CREATE TABLE routing_auto_consent(scope TEXT PRIMARY KEY,enabled INTEGER,service TEXT,service_name TEXT,include_files INTEGER,purposes_json TEXT,revision INTEGER,updated_at TEXT)")
            db.execute("INSERT INTO routing_auto_consent VALUES(?,?,?,?,?,?,?,?)", ("global", 1, "old-service", "old", 1, '["extract_turn"]', 7, "old-time"))
        value = RoutingStore(self.onto).policy("global")
        self.assertTrue(value["enabled"])
        self.assertTrue(value["includeFiles"])
        self.assertFalse(value["includeCharter"])
        self.assertEqual(value["revision"], 7)
        self.assertEqual(value["purposes"], ["extract_turn"])
        self.assertFalse(RoutingStore(self.onto).policy("other-device")["includeCharter"])

    def test_charter_scope_requires_explicit_optin_and_never_authorizes_other_ancestors(self):
        self.enable()
        self.assertEqual(self.default_consent().status_code, 200)
        router = Router(self.onto, self.convs, self.cid)
        service = service_info(self.online)["id"]
        source = {"key": "charter_clause:synthetic", "kind": "charter_clause", "version": "v1", "blocked": "", "ordinaryService": ""}
        self.assertFalse(router.allowed(source, service, "extract_turn"))
        self.assertEqual(self.default_consent(includeCharter=True, acknowledge=False).status_code, 409)
        self.assertFalse(self.store.policy("global")["includeCharter"])
        result = self.default_consent(includeCharter=True)
        self.assertTrue(result.json()["defaultAuthorization"]["includeCharter"])
        for kind in ("charter", "charter_clause", "charter_document", "charter_workspace", "charter_draft"):
            self.assertTrue(router.allowed({**source, "kind": kind}, service, "extract_turn"))
        self.assertFalse(router.allowed(source, service, "new_unapproved_purpose"))
        self.assertFalse(router.allowed(source, "different-service", "extract_turn"))
        self.assertFalse(router.allowed({**source, "blocked": "来源失效"}, service, "extract_turn"))
        self.assertFalse(router.allowed({**source, "kind": "material"}, service, "extract_turn"))
        self.store.revoke("global", source["key"])
        self.assertFalse(router.allowed(source, service, "extract_turn"))
        self.assertEqual(self.default_consent(includeCharter=True).status_code, 200)
        self.assertFalse(router.allowed(source, service, "extract_turn"), "reenabling does not remove a specific revocation")
        with self.onto._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM routing_grants").fetchone()[0], 0)

    def test_disabling_charter_scope_stops_prepared_background_payload(self):
        GrowthStore.instance().create_charter({"challengeStyle": "先把我的问题听完整"})
        self.enable()
        self.default_consent(includeCharter=True)
        message = self.message()
        router = Router(self.onto, self.convs, self.cid)
        guard = GuardedProvider(router, self.online, "extract_turn", [router.ref("message", message["id"])], background=True)
        request = ChatRequest(system="合成提取", messages=[{"role": "user", "content": message["content"]}])
        guard.check(request)
        self.default_consent(includeCharter=False)
        with self.assertRaises(HTTPException):
            guard.complete_json(request)
        self.assertEqual(self.online.requests, [])

    def test_recovers_all_old_turns_not_only_latest_global_hundred_and_click_is_idempotent(self):
        messages = [self.message(text=f"第 {i} 个合成项目由我持续负责研发。") for i in range(3)]
        expected = [self.pause(message) for message in messages]
        foreign = self.convs.create_conversation(device_scope="another-device")["id"]
        for i in range(105):
            self.pause(self.message(foreign, f"另一设备的合成任务 {i}"))
        pending = self.routing_state()["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["count"], 3)
        self.assertEqual(set(pending[0]["messageIds"]), {m["id"] for m in messages})
        resumed = self.resume()
        self.assertEqual(resumed["queuedCount"], 3)
        self.assertEqual(set(resumed["jobIds"]), set(expected))
        self.assertEqual(self.resume()["queuedCount"], 0)
        self.assertEqual(len(self.store.paused_jobs(foreign)), 105)
        self.assertEqual(self.routing_state()["pending"], [])
        with self.onto._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ontology_jobs").fetchone()[0], 108)

    def test_later_completed_attempt_suppresses_old_pause_and_does_not_reextract(self):
        message = self.message()
        self.pause(message)
        jid = jobs.enqueue_extraction(self.cid, message["id"], store=self.onto)
        self.onto.claim_next_job("completed-worker")
        self.onto.finish_job(jid, "completed-worker", result={"state": "done", "created": []})
        self.assertEqual(self.routing_state()["pending"], [])
        self.assertEqual(self.resume()["queuedCount"], 0)

    def test_real_worker_pauses_each_message_then_resumes_all_without_confirming_memories(self):
        GrowthStore.instance().create_charter({"challengeStyle": "先澄清当前处境", "quietDomains": ["不主动推测家庭关系"]})
        self.enable()
        self.default_consent()
        messages = [self.message(text=f"我持续负责合成项目 {i} 的研发协调。") for i in range(3)]
        for message in messages:
            jobs.enqueue_extraction(self.cid, message["id"], store=self.onto)
        self.online.result = {"claims": [], "entities": []}
        with patch("mindos.zhijun.memory.extraction_allowed", return_value=True):
            self.assertEqual(jobs.drain(store=self.onto, conv_store=self.convs), 3)
            self.assertEqual(self.online.requests, [])
            self.assertEqual(self.routing_state()["pending"][0]["count"], 3)
            self.default_consent(includeCharter=True)
            self.assertEqual(self.resume()["queuedCount"], 3)
            self.assertEqual(jobs.drain(store=self.onto, conv_store=self.convs), 3)
        self.assertEqual(len(self.online.requests), 3)
        self.assertEqual(self.routing_state()["pending"], [])
        self.assertEqual(self.onto.list_claims(trust_states=("confirmed",)), [])
        self.assertEqual(self.resume()["queuedCount"], 0)

    def test_resume_without_new_authorization_pauses_again_and_local_requires_explicit_choice(self):
        GrowthStore.instance().create_charter({"challengeStyle": "先澄清当前处境"})
        self.enable()
        message = self.message()
        self.pause(message)
        with patch("mindos.zhijun.memory.extraction_allowed", return_value=True):
            self.resume()
            jobs.drain(store=self.onto, conv_store=self.convs)
            self.assertEqual(len(self.store.paused_jobs(self.cid)), 1)
            self.assertEqual(self.online.requests, [])
            self.assertEqual(self.local.requests, [])
            self.local.result = {"claims": [], "entities": []}
            self.resume(localOnly=True)
            jobs.drain(store=self.onto, conv_store=self.convs)
        self.assertEqual(len(self.local.requests), 1)
        self.assertEqual(self.online.requests, [])

    def test_expired_pending_preview_can_refresh_but_not_grant_old_token_or_changed_source(self):
        self.enable()
        message = self.message()
        router = Router(self.onto, self.convs, self.cid)
        request = ChatRequest(system="合成理解提议", messages=[{"role": "user", "content": message["content"]}])
        preview = router.prepare("extract_turn", request, [router.ref("message", message["id"])], self.online, background=True)
        self.pause(message, preview_id=preview["revision"])
        with self.onto._connect() as db:
            db.execute("UPDATE routing_previews SET created_at=?", (time.time() - 7200,))
        self.assertTrue(self.routing_state()["pending"][0]["previewExpired"])
        denied = self.client.post(self.url + "/routing/grant", json={"revision": preview["revision"], "keys": preview["missing"]})
        self.assertEqual(denied.status_code, 409)
        refreshed = self.client.get(self.url + "/routing/pending/" + preview["revision"])
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(self.online.requests, [])
        self.convs.update_message(message["id"], content="修改后的合成输入内容")
        changed = self.client.get(self.url + "/routing/pending/" + preview["revision"])
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json()["detail"]["code"], "SOURCE_CHANGED")

    def test_purged_preview_keeps_backlog_resumable_and_changed_service_is_not_silently_used(self):
        self.enable()
        message = self.message()
        router = Router(self.onto, self.convs, self.cid)
        preview = router.prepare("extract_turn", ChatRequest(system="合成", messages=[]), [router.ref("message", message["id"])], self.online)
        self.pause(message, preview_id=preview["revision"])
        self.online.model = "changed-model"
        response = self.client.get(self.url + "/routing/pending/" + preview["revision"])
        self.assertEqual(response.status_code, 409)
        with self.onto._connect() as db:
            db.execute("DELETE FROM routing_previews")
        self.assertEqual(self.client.get(self.url + "/routing/pending/" + preview["revision"]).status_code, 409)
        self.assertEqual(self.resume()["queuedCount"], 1)
        self.assertEqual(self.online.requests, [])

    def test_pauses_are_individual_even_when_later_job_succeeds(self):
        first, second = self.message(), self.message(text="另外一个合成输入，表达了我长期负责的事。")
        self.pause(first, reason="source_unavailable")
        jid = jobs.enqueue_extraction(self.cid, second["id"], store=self.onto)
        self.onto.claim_next_job("success-worker")
        self.onto.finish_job(jid, "success-worker", result={"state": "done"})
        self.store.pending(self.cid, "extract_turn", None)
        pending = self.routing_state()["pending"]
        self.assertEqual(pending[0]["count"], 1)
        self.assertEqual(pending[0]["reason"], "source_unavailable")
        self.assertEqual(pending[0]["messageIds"], [first["id"]])

    def test_concurrent_restore_reuses_each_job_once_and_preserves_mixed_reasons(self):
        first, second = self.message(), self.message(text="我长期承担第二个合成项目的研发安排。")
        ids = [self.pause(first), self.pause(second, reason="source_unavailable")]
        item = self.routing_state()["pending"][0]
        self.assertEqual(item["reason"], "multiple_reasons")
        self.assertEqual({r["code"]: r["count"] for r in item["reasons"]}, {"consent_required": 1, "source_unavailable": 1})
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.store.resume_jobs(self.cid, "extract_turn"), range(3)))
        self.assertEqual(sorted(jid for batch in results for jid in batch), sorted(ids))
        self.assertEqual(sum(bool(batch) for batch in results), 1)

    def test_revoke_after_requeue_is_rechecked_before_model_dispatch(self):
        GrowthStore.instance().create_charter({"challengeStyle": "先听完整问题"})
        self.enable()
        self.default_consent(includeCharter=True)
        message = self.message()
        self.pause(message)
        self.resume()
        self.store.revoke("global")
        with patch("mindos.zhijun.memory.extraction_allowed", return_value=True):
            jobs.drain(store=self.onto, conv_store=self.convs)
        self.assertEqual(self.online.requests, [])
        self.assertEqual(self.local.requests, [])
        self.assertEqual(self.routing_state()["pending"][0]["count"], 1)

    def test_changed_charter_rejects_prepared_provider_despite_standing_optin(self):
        GrowthStore.instance().create_charter({"challengeStyle": "先问清现状"})
        self.enable()
        self.default_consent(includeCharter=True)
        message = self.message()
        router = Router(self.onto, self.convs, self.cid)
        guard = GuardedProvider(router, self.online, "extract_turn", [router.ref("message", message["id"])], background=True)
        request = ChatRequest(system="合成整理", messages=[{"role": "user", "content": message["content"]}])
        guard.check(request)
        GrowthStore.instance().create_charter({"challengeStyle": "先列事实，再谈推测"})
        with self.assertRaises(HTTPException) as caught:
            guard.complete_json(request)
        self.assertEqual(caught.exception.detail["code"], "CHARTER_CHANGED")
        self.assertEqual(self.online.requests, [])

    def test_stale_derived_source_stays_paused_even_with_local_recovery(self):
        claim = harness.RoutingTests.claim(self)
        router = Router(self.onto, self.convs, self.cid)
        old_ref = router.resolve(router.ref("claim", claim["id"]))[0]["ref"]
        message = self.convs.append_message(self.cid, "user", "我继续补充这条合成理解的适用情境。", meta={"routingSources": [old_ref]})
        self.pause(message)
        self.onto.add_evidence(claim["id"], [{"kind": "user_edit", "quote": "版本发生了变化"}])
        self.resume(localOnly=True)
        with patch("mindos.zhijun.memory.extraction_allowed", return_value=True):
            jobs.drain(store=self.onto, conv_store=self.convs)
        self.assertEqual(self.local.requests, [])
        self.assertEqual(self.online.requests, [])
        self.assertEqual(self.routing_state()["pending"][0]["reason"], "source_changed")

    def test_virtual_home_backlog_is_scoped_before_latest_selection(self):
        for scope in ("global", "another-device"):
            jid = self.onto.enqueue_job("home_brief", "today:" + scope, payload={"scope": scope, "sourceHash": "synthetic"})
            job = self.onto.claim_next_job("home-worker")
            self.assertEqual(job["jobId"], jid)
            self.onto.finish_job(jid, "home-worker", result={"state": "paused", "reason": "consent_required"})
        restored = self.store.resume_jobs("scope:global", "home_brief")
        self.assertEqual(len(restored), 1)
        self.assertEqual(len(self.store.paused_jobs("scope:another-device", "home_brief")), 1)
