"""Synthetic two-device ontology boundaries; no models or production records."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mindos import ontology
from mindos.stores import conversation_store, ontology_store
from mindos.zhijun import alignment, memory
from mindos.zhijun.memory_retrieval import confirmed_background


class OntologyScopeApiTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.onto = ontology_store.reset_for_tests(Path(self.tmp.name) / "ontology.db")
        self.convs = conversation_store.reset_for_tests(Path(self.tmp.name) / "conversations.db")
        self.a = self.convs.create_conversation(device_scope="device:alpha")["id"]
        self.b = self.convs.create_conversation(device_scope="device:beta")["id"]
        app = FastAPI()

        @app.middleware("http")
        async def scoped_device(request, call_next):
            request.state.mindos_device_context = SimpleNamespace(device_id=request.headers.get("x-test-device"))
            return await call_next(request)

        app.include_router(ontology.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, path, body=None, device="alpha", **kwargs):
        return self.client.request(method, "/api/mindos/ontology" + path, json=body,
            headers={"x-test-device": device} if device else {}, **kwargs)

    def manual(self, content, device="alpha", **fields):
        result = self.call("POST", "/claims", {"content": content, "section": "who", "layer": "self_declared", **fields}, device)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def candidate(self, content, cid=None, scope="device:alpha"):
        cid = cid or self.a
        msg = self.convs.append_message(cid, "user", content)
        return self.onto.create_claim({"content": content, "section": "who", "layer": "self_declared", "device_scope": scope},
            [{"kind": "conversation_turn", "conversation_id": cid, "message_id": msg["id"], "quote": content}])

    def test_identical_manual_claims_and_partial_edits_have_independent_device_hashes(self):
        a = self.manual("我是一名总经理")
        b = self.manual("我是一名总经理", "beta")
        global_claim = self.manual("我是一名总经理", None)
        self.assertEqual(len({a["id"], b["id"], global_claim["id"]}), 3)
        self.assertEqual(a["deviceScope"], "device:alpha")
        self.assertEqual(self.call("POST", "/claims", {"content": "我是一名总经理", "section": "who", "layer": "self_declared"}).status_code, 409)
        for device, item in (("alpha", a), ("beta", b)):
            response = self.call("POST", f"/claims/{item['id']}/review", {"action": "partial", "editedContent": "我是一名产品负责人"}, device)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["replacedBy"]["deviceScope"], "device:" + device)
        found = self.onto.find_active_by_hash("ent_me", "is", "我是一名总经理")
        self.assertEqual(found["id"], global_claim["id"])

    def test_list_detail_stats_inbox_filter_before_limit(self):
        own = self.manual("我在合成甲公司工作")
        pending = self.candidate("我希望核对甲设备的岗位")
        self.manual("我在合成乙公司工作", "beta")
        self.candidate("我希望核对乙设备的岗位", self.b, "device:beta")
        self.assertEqual([c["id"] for c in self.call("GET", "/claims?limit=1").json()["items"]], [own["id"]])
        self.assertEqual(self.call("GET", f"/claims/{own['id']}", device="beta").status_code, 404)
        self.assertEqual(self.call("GET", f"/claims/{own['id']}", device=None).status_code, 404)
        stats = self.call("GET", "/stats").json()
        self.assertEqual((stats["claims"]["confirmed"], stats["claims"]["working"]), (1, 1))
        self.assertEqual([c["id"] for c in self.call("GET", "/inbox?limit=1").json()["items"]], [pending["id"]])
        self.assertEqual(self.call("GET", "/claims", device=None).json()["items"], [])

    def test_review_export_and_forged_conversation_cannot_modify_other_device(self):
        own = self.candidate("我负责甲设备合成项目")
        foreign = self.candidate("我负责乙设备合成项目", self.b, "device:beta")
        for path, body in ((f"/claims/{foreign['id']}/review", {"action": "confirm"}),
                           (f"/claims/{foreign['id']}/export", {"allowed": True})):
            self.assertEqual(self.call("POST", path, body).status_code, 404)
        self.assertEqual(self.call("POST", f"/claims/{own['id']}/review", {"action": "confirm", "conversationId": self.b}).status_code, 404)
        self.assertEqual(self.onto.get_claim(own["id"])["trustState"], "working")
        self.assertEqual(self.call("POST", f"/claims/{own['id']}/review", {"action": "confirm", "conversationId": self.a}).status_code, 200)
        self.assertEqual(self.convs.list_messages(self.b)[-1]["role"], "user")
        self.assertFalse(self.onto.get_claim(foreign["id"])["exportAllowed"])

    def test_manual_records_have_scope_for_alignment_and_background_retrieval(self):
        own = self.manual("我是一名合成产品总监", predicate="role")
        self.assertTrue(alignment.visible(own, self.convs, "device:alpha"))
        self.assertFalse(alignment.visible(own, self.convs, "global"))
        self.assertFalse(alignment.visible(own, self.convs, "device:beta"))
        own_background = confirmed_background(self.onto, conversations=self.convs, scope="device:alpha")
        self.assertEqual([c["id"] for c in own_background], [own["id"]])
        self.assertEqual(confirmed_background(self.onto, conversations=self.convs, scope="device:beta"), [])

    def test_unknown_or_deleted_origins_never_become_global(self):
        missing = self.onto.create_claim({"content": "原会话已删除的合成记录", "section": "who", "layer": "self_declared"},
            [{"kind": "conversation_turn", "conversation_id": "conv_missing", "quote": "原会话"}], trust_state="confirmed")
        own = self.candidate("我负责一个之后删除会话的项目")
        self.convs.delete_conversation(self.a)
        for device in (None, "alpha", "beta"):
            for item in (missing, own):
                self.assertEqual(self.call("GET", f"/claims/{item['id']}", device=device).status_code, 404)
        self.assertIsNotNone(self.onto.get_claim(own["id"]), "read isolation must not erase retained records")

    def test_material_derived_legacy_record_requires_the_actual_material_scope(self):
        record = self.onto.create_claim({"content": "设备甲文件里的合成事实", "section": "who", "layer": "observed"},
            [{"kind": "material_span", "material_id": "synthetic-material", "quote": "合成事实"}])
        def require(_ident, scope):
            if scope != "device:alpha":
                raise HTTPException(404, "不可用")
            return {"id": "synthetic-material"}
        with patch("mindos.chat_imports.require_material", side_effect=require):
            self.assertEqual(self.call("GET", f"/claims/{record['id']}").status_code, 200)
            self.assertEqual(self.call("GET", f"/claims/{record['id']}", device=None).status_code, 404)
            self.assertEqual(self.call("GET", f"/claims/{record['id']}", device="beta").status_code, 404)
        with patch("mindos.chat_imports.require_material", side_effect=HTTPException(404, "已删除")):
            self.assertEqual(self.call("GET", f"/claims/{record['id']}").status_code, 404)

    def test_same_alias_does_not_reuse_another_devices_entity(self):
        secret = self.onto.upsert_entity("合成机密称呼", aliases=["老周"], device_scope="device:beta")
        own = self.manual("我和老周是长期搭档", section="people", objectName="老周")
        self.assertNotEqual(own["objectEntityId"], secret["id"])
        self.assertEqual(own["objectName"], "老周")
        entities = self.call("GET", "/entities").json()["items"]
        self.assertNotIn("合成机密称呼", str(entities))
        self.assertIn("老周", str(entities))
        self.assertEqual(self.onto.get_entity(secret["id"])["canonicalName"], "合成机密称呼")

    def test_export_contains_only_own_claims_events_and_entities(self):
        own = self.manual("我与合成甲关系合作", section="people", objectName="合成甲")
        foreign = self.manual("我与合成乙关系合作", "beta", section="people", objectName="合成乙")
        exported = self.call("GET", "/export").json()
        self.assertEqual([c["id"] for c in exported["claims"]], [own["id"]])
        self.assertTrue(exported["reviewEvents"])
        self.assertTrue(all(e["targetId"] == own["id"] for e in exported["reviewEvents"]))
        self.assertNotIn(foreign["id"], str(exported))
        self.assertNotIn("合成乙", str(exported))

    def test_mixed_conflict_is_hidden_and_cannot_be_resolved(self):
        own = self.manual("我重视合成原则甲", section="principles")
        foreign = self.manual("我重视合成原则乙", "beta", section="principles")
        conflict = self.onto.create_conflict(own["id"], foreign["id"])
        self.assertEqual(self.call("GET", "/proposals").json()["conflicts"], [])
        self.assertEqual(self.call("POST", f"/proposals/conflicts/{conflict['id']}/resolve", {"keep": "a"}).status_code, 404)
        self.assertEqual(self.onto.get_claim(foreign["id"])["trustState"], "confirmed")

    def test_global_maintenance_is_explicitly_unavailable_to_device(self):
        own = self.manual("我拥有甲设备的合成记忆")
        for method, path, body in (("POST", "/purge", {"confirm": "删除全部记忆"}),
                                  ("POST", "/consolidate", None), ("GET", "/context-pack", None),
                                  ("POST", "/proposals/merges/not-mine/resolve", {"accept": True})):
            with self.subTest(path=path):
                self.assertEqual(self.call(method, path, body).status_code, 403)
        self.assertIsNotNone(self.onto.get_claim(own["id"]))

    def test_request_cannot_choose_another_device_scope(self):
        response = self.call("POST", "/claims", {"content": "我是一名合成负责人", "deviceScope": "device:beta"})
        self.assertEqual(response.status_code, 422)

    def test_projections_are_read_only_scoped_views(self):
        self.manual("甲设备的合成身份")
        self.manual("乙设备的合成身份", "beta")
        own = self.call("GET", "/projection")
        self.assertEqual(own.status_code, 200, own.text)
        self.assertIn("甲设备", own.json()["markdown"])
        self.assertNotIn("乙设备", own.text)
        local = self.call("GET", "/projection", device=None)
        self.assertNotIn("甲设备", local.text)
        self.assertNotIn("乙设备", local.text)

    def test_extracted_same_facts_do_not_dedupe_across_devices(self):
        from mindos.zhijun import extract
        text = "我是一名制造企业总经理"
        claim = extract.ValidatedClaim(section="who", layer="self_declared", predicate="role", subject="me", object=None,
            content=text, quote=text, confidence=.9, scope="long_term", privacy_level="private",
            why_it_matters="讨论组织管理时需要考虑用户承担的岗位责任")
        ids = []
        for cid in (self.a, self.b):
            msg = self.convs.append_message(cid, "user", text)
            result = memory.process_candidates([claim], [], store=self.onto, conversation_id=cid,
                message_id=msg["id"], user_text=text)
            self.assertEqual(len(result["created"]), 1)
            ids.extend(result["created"])
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(self.onto.get_claim(cid)["trustState"] == "working" for cid in ids))
