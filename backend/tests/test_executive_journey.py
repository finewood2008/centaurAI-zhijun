"""One fictional executive across real routes/stores with deterministic models.

This proves workflow/state/provenance, not language-model answer quality; the
separate opt-in synthetic provider smoke evaluates that at the actual boundary.
"""
import json
import os
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests import test_task_routing as routing_fixture
from tests import test_zhijun_turn_sse as sse_fixture
from mindos import conversations, ontology, zhijun_onboarding, growth, memory_routes
from mindos.stores.growth_store import GrowthStore
from mindos.stores.charter_draft_store import CharterDraftStore
from mindos.stores.alignment_store import AlignmentStore
from mindos.zhijun import jobs, charter
from mindos.zhijun.routing import Router, prepare_chat, service_info


class ExecutiveJourneyTests(unittest.TestCase):
    setUp = routing_fixture.RoutingTests.setUp
    tearDown = routing_fixture.RoutingTests.tearDown
    enable = routing_fixture.RoutingTests.enable
    preview = routing_fixture.RoutingTests.preview
    grant = routing_fixture.RoutingTests.grant

    def setup_journey(self):
        app = FastAPI()
        for router in (conversations._build_router(), ontology._build_router(),
                       zhijun_onboarding._build_router(), growth.router,
                       charter.build_router(), memory_routes.build_router()):
            app.include_router(router)
        self.client = TestClient(app, headers={"X-Requested-By": "centaur-vdb"})
        self.stack.enter_context(patch.dict(os.environ, {"ZHIJUN_EXTRACTION": "1"}))
        self.assertEqual(self.client.get("/api/mindos/zhijun/onboarding").json()["state"], "welcome")
        started = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "start"}).json()
        self.cid = started["conversationId"]
        self.url = "/api/mindos/conversations/" + self.cid
        self.enable(True)

    def send_and_extract(self, text, section, predicate, *, layer="self_declared", scope="long_term"):
        body, preview = self.preview(text)
        if preview["missing"]:
            self.grant(preview)  # explicit consent in this synthetic device only
            body, preview = self.preview(text)
        response = self.client.post(self.url + "/messages", json={**body,
            "routeRevision": preview["revision"], "requestId": uuid.uuid4().hex})
        self.assertEqual(response.status_code, 200, response.text)
        events = sse_fixture._parse_sse(response.text)
        self.assertFalse([payload for event, payload in events if event == "error"], response.text)
        extraction = next(payload for event, payload in events if event == "extraction")
        self.assertEqual(extraction["state"], "queued", extraction)
        self.online.result = {"claims": [{"content": text, "quote": text, "section": section,
            "layer": layer, "predicate": predicate, "subject": "me", "object": None,
            "confidence": .92, "scope_hint": scope, "privacy_hint": "private", "merge_into": None,
            "why_it_matters": "后续安排工作、取舍资源及跟进这件事时需要核对此条件。", "date": None}], "entities": []}
        job = self.onto.get_job(extraction["jobId"])
        result = jobs.run_job(job, store=self.onto, conv_store=self.convs)
        if result.get("state") == "paused" and result.get("reason") == "consent_required":
            pending = self.store.get_preview(result["previewId"], self.cid)
            self.assertFalse(pending["blocked"], pending)
            self.grant({**pending, "revision": result["previewId"]})  # separate purpose
            result = jobs.run_job(job, store=self.onto, conv_store=self.convs)
        self.assertEqual(result.get("state"), "done", result)
        return result

    def test_zero_to_personal_context_calibration_charter_and_event(self):
        self.setup_journey()
        samples = [
            ("我叫林舟，是一家制造企业的运营负责人", "who", "role"),
            ("我目前负责星桥项目，预算上限是三十万元", "matters", "working_on"),
            ("我的原则是不为短期业绩牺牲客户信任", "principles", "holds_principle"),
            ("我希望三年后成为能培养接班人的管理者", "direction", "wants_to"),
        ]
        created = []
        for text, section, predicate in samples:
            result = self.send_and_extract(text, section, predicate,
                layer="aspirational" if section == "direction" else "self_declared")
            self.assertEqual(len(result["created"]), 1, (text, result))
            created.extend(result["created"])
        self.assertEqual(self.onto.list_claims(trust_states=("confirmed",)), [])
        for ident in created:
            claim = self.onto.get_claim(ident)
            self.assertIsNone(claim["selfAlignment"]["level"])
            response = self.client.post("/api/mindos/ontology/claims/" + ident + "/review",
                json={"action": "confirm", "conversationId": self.cid, "surface": "conversation"})
            self.assertEqual(response.status_code, 200, response.text)
        self.client.post("/api/mindos/zhijun/onboarding", json={"action": "profile_ready", "conversationId": self.cid})
        response = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "profile_confirmed"})
        self.assertEqual(response.json()["state"], "ready")
        self.assertIsNone(GrowthStore.instance().current_charter(), "onboarding completion is not charter publication")
        project = self.onto.get_claim(created[1])
        version = project["selfAlignment"]
        AlignmentStore(self.onto).review(project["id"], {"requestId": uuid.uuid4().hex,
            "action": "calibrate", "level": 0, "framing": "long_term", "note": "公司安排的职责，不代表我的个人追求",
            "expectedRevision": version["revision"], "claimVersion": version["claimVersion"], "evidenceVersion": version["evidenceVersion"]})
        router = Router(self.onto, self.convs, self.cid, provider=self.local)
        plan = prepare_chat(router, "星桥项目预算上限是多少？", local=True)
        self.assertIn("三十万元", plan.assembled.system, "low alignment cannot hide a relevant fact")
        self.assertIn("普通对话里的理解、复述和回复不会自动成为正式记录", plan.assembled.system)
        self.assertIn(project["id"], plan.assembled.confirmed_ids)
        # Stable charter is a separate explicit Markdown publication.
        store = CharterDraftStore()
        ws = store.start_workspace(self.cid, "global", "executive-charter-start")["workspace"]
        document = "# 林舟的人生章程\n\n## 合作方式\n先说明依据和不确定性，不替我决定。\n\n## 在意的事\n我希望兼顾客户信任与团队成长。\n"
        ws = store.edit_workspace(ws["id"], cid=self.cid, scope="global", revision=ws["revision"],
            request_id="executive-charter-edit", document=document)["workspace"]
        self.assertIsNone(GrowthStore.instance().current_charter())
        published = store.workspace_action(ws["id"], cid=self.cid, scope="global", revision=ws["revision"],
            request_id="executive-charter-publish", action="publish", publish_document=True)
        self.assertEqual(published["charter"]["document"], document)
        self.assertEqual(len(self.onto.list_claims()), 4)
        self.assertIsNone(self.onto.get_claim(created[0])["selfAlignment"]["level"])
        # Charter remains unapproved for external use until separately consented.
        online = Router(self.onto, self.convs, self.cid, provider=self.online)
        plan = prepare_chat(online, "结合我的职责，怎样让星桥项目先小范围试点？")
        self.assertTrue(any(key.startswith("charter") for key in plan.preview["missing"]))
        self.assertIn("客户信任", plan.assembled.system)
        self.assertIn("星桥项目", plan.assembled.system)
        self.assertIn("三十万元", plan.assembled.system)
        # An event is a context draft, never automatically a personality point.
        # Explicitly authorize its necessary charter source for this test purpose.
        self.store.set_mode(self.cid, "local", "")
        self.local.result = {"claims": [], "entities": []}
        from mindos.zhijun.extract import ValidatedClaim
        from mindos.zhijun.memory import process_candidates
        event = "明天的星桥工作坊面向企业主，上午讲课、下午实操"
        message = self.convs.append_message(self.cid, "user", event, meta={"routingSources": []})
        result = process_candidates([ValidatedClaim("matters", "self_declared", "happened", "me", None,
            event, event, .9, "context_only", "private", why_it_matters="帮助跟进明天的活动流程与准备情况")], [],
            store=self.onto, conversation_id=self.cid, message_id=message["id"], user_text=event, routing_sources=[])
        self.assertFalse(result["created"])
        self.assertIn("draftId", result)
        self.assertEqual(GrowthStore.instance().current_charter()["id"], published["charter"]["id"])
        # Restart preserves confirmed facts, alignment and exactly the user's MD.
        self.assertEqual(type(self.convs)(self.convs.db_path).get_conversation(self.cid)["messageCount"], self.convs.count_messages(self.cid))
        self.assertEqual(type(self.onto)(self.onto.db_path).get_claim(project["id"])["selfAlignment"]["level"], 0)
        self.assertEqual(GrowthStore(GrowthStore.instance()._db_path).current_charter()["document"], document)
