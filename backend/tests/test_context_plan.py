"""Isolated ContextPlan payload/permission checks, with no external model calls."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from tests import test_task_routing as harness
from mindos.stores.growth_store import GrowthStore
from mindos.stores.learning_store import LearningStore
from mindos.stores.chat_import_store import ChatImportStore
from mindos.zhijun.context_plan import build_context_plan, fit_context_plan
from mindos.zhijun import context_sources
from mindos.zhijun.provider import ChatRequest
from mindos.zhijun.routing import Router, GuardedProvider, service_info


class ContextPlanTests(unittest.TestCase):
    setUp = harness.RoutingTests.setUp
    tearDown = harness.RoutingTests.tearDown
    enable = harness.RoutingTests.enable

    def claim(self, text, *, working=False, who=False, evidence=None):
        return self.onto.create_claim({"content": text, "section": "who" if who else "matters",
            "predicate": "role" if who else "working_on", "layer": "hypothesis" if working else "self_declared"},
            evidence or [{"kind": "user_edit", "quote": text}],
            trust_state="working" if working else "confirmed", trust_origin="model" if working else "user_created")

    def grant(self, router, refs):
        sources = [s for ref in refs for s in router.resolve(ref)]
        self.store.grant(router.scope, sources, service_info(self.online)["id"], "chat")
        files = [s["materialRef"] for s in sources if s["kind"] == "material"]
        if files:
            ChatImportStore(self.convs).grant(files, service_info(self.online)["id"])

    def decision(self, message, title="星桥项目保密协议", text="星桥项目合作要先签保密协议"):
        return GrowthStore.instance().create_decision({"title": title, "context": text, "options": ["先签约", "公开"],
            "choice": "先签约", "rationale": "保护核心方案", "confidence": 70, "expectedOutcome": "合作边界更清楚",
            "relatedEntityIds": [], "evidenceRefs": [json.dumps({"conversationId": message["conversationId"], "messageId": message["id"]})]})

    def test_multiple_lanes_are_real_citable_text_in_actual_authorized_payload(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        original = self.convs.append_message(self.cid, "user", "星桥项目合作要先签保密协议；我还需要先找法律顾问，核对服务费用和协议有效期限。", meta={"routingSources": []})
        c = self.claim("星桥项目合作要先签保密协议")
        who = self.claim("我是星桥项目的产品负责人", who=True)
        summary = self.convs.save_summary(self.cid, up_to_seq=original["seq"], summary="星桥项目合作尚未完成法律咨询",
            meta={"routingSources": [router.resolve(router.ref("message", original["id"]))[0]["ref"]]})
        decision = self.decision(original)
        episode = LearningStore(self.onto).start(decision, c, self.cid,
            {"situation": "星桥项目合作", "prediction": "签保密协议后可能减少担忧"})
        refs = [router.ref("claim", c["id"]), router.ref("claim", who["id"]), router.ref("message", original["id"]),
                router.ref("summary", f"{self.cid}:{summary['revision']}"), router.ref("decision", decision["id"]), router.ref("episode", decision["id"])]
        self.grant(router, refs)
        plan = build_context_plan(router, "星桥项目合作的保密协议", [], provider=self.online)
        categories = {i["category"] for i in plan["background"] + plan["evidence"]}
        self.assertTrue({"background", "ontology", "history", "summary", "decision", "episode"}.issubset(categories), categories)
        self.assertIn("事前观察预期（不是事实）", plan["system"])
        self.assertIn("派生内容，不是新增证据", plan["system"])
        request = ChatRequest(system=plan["system"], messages=[{"role": "user", "content": "星桥项目合作的保密协议"}])
        preview = router.prepare("chat", request, plan["refs"], self.online)
        self.assertEqual(preview["missing"], [])
        GuardedProvider(router, self.online, "chat", plan["refs"], revision=preview["revision"]).complete_json(request)
        actual = self.online.requests[-1].system
        for item in plan["background"] + plan["evidence"]:
            self.assertIn(f"[{item['citationId']}]", actual)
            self.assertIn(item["text"], actual)
            self.assertTrue(item["ref"]["version"])
        self.assertEqual(plan["revision"], build_context_plan(router, "星桥项目合作的保密协议", [], provider=self.online)["revision"])

    def test_authorized_backfill_and_working_cap_do_not_expand_consent(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        denied = [self.claim(f"星桥项目未授权记录{i}") for i in range(10)]
        approved = [self.claim(f"星桥项目已许可方案{i}") for i in range(7)]
        working = [self.claim(f"星桥项目可能的推测{i}", working=True) for i in range(3)]
        self.grant(router, [router.ref("claim", c["id"]) for c in approved + working])
        ranked = [{**c, "score": 1 - i * .01} for i, c in enumerate(denied + approved + working)]
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=ranked):
            plan = build_context_plan(router, "星桥项目", [], provider=self.online)
        ids = {i["id"] for i in plan["evidence"]}
        self.assertEqual(len(plan["evidence"]), 8)
        self.assertFalse(ids & {c["id"] for c in denied})
        self.assertEqual(sum(i.get("claim", {}).get("trustState") == "working" for i in plan["evidence"]), 1)
        self.assertTrue(plan["excluded"])
        self.assertNotIn("未授权记录", plan["system"])

    def test_background_permission_backfills_but_never_blocks_general_chat(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        denied = [self.claim(f"我是未许可团队{i}的负责人", who=True) for i in range(5)]
        good = self.claim("我是已许可项目的设计师", who=True)
        self.grant(router, [router.ref("claim", good["id"])])
        with patch("mindos.zhijun.memory_retrieval.confirmed_background", return_value=denied + [good]), \
                patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[]):
            plan = build_context_plan(router, "你好", [], provider=self.online)
        self.assertEqual([i["id"] for i in plan["background"]], [good["id"]])
        self.assertEqual(plan["evidence"], [])
        self.assertNotIn("未许可团队", plan["system"])

    def test_missing_source_pending_is_bounded_and_default_omit_is_respected(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        claims = [self.claim(f"星桥项目保密协议{i}") for i in range(3)]
        ranked = [{**c, "score": .8} for c in claims]
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=ranked):
            plan = build_context_plan(router, "星桥项目保密协议", [], provider=self.online)
        self.assertEqual(len(plan["evidence"]), 1)
        request = ChatRequest(system=plan["system"], messages=[])
        preview = router.prepare("chat", request, plan["refs"], self.online)
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", plan["refs"], revision=preview["revision"]).complete_json(request)
        self.assertEqual(self.online.requests, [])
        self.store.set_handling("global", enabled=True, action="omit", service=service_info(self.online)["id"], expected_revision=0)
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=ranked):
            omitted = build_context_plan(router, "星桥项目保密协议", [], provider=self.online)
        self.assertEqual(omitted["evidence"], [])
        self.assertNotIn("保密协议0", omitted["system"])

    def test_explicit_personal_wish_surfaces_missing_source_then_reads_only_after_grant(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        text = "我希望三年后成为能培养接班人的管理者，目前还没有培养出接班人"
        wish = self.onto.create_claim({"content": text, "section": "direction", "layer": "aspirational",
            "predicate": "wants_to"}, [{"kind": "user_edit", "quote": text}],
            trust_state="confirmed", trust_origin="user_created")
        who = self.claim("我是制造企业的运营负责人", who=True)
        question = "我已经实现了培养接班人的愿望吗？你实际知道什么，还有什么不知道？"
        with patch("mindos.zhijun.memory_retrieval._index_scores", return_value={}):
            plan = build_context_plan(router, question, [], provider=self.online)
        self.assertIn(wish["id"], [i["id"] for i in plan["evidence"]])
        self.assertLess(next(i["relevanceScore"] for i in plan["evidence"] if i["id"] == wish["id"]), .45)
        self.assertNotIn(who["id"], [r["id"] for r in plan["refs"]])
        request = ChatRequest(system=plan["system"], messages=[{"role": "user", "content": question}])
        preview = router.prepare("chat", request, plan["refs"], self.online)
        self.assertEqual(preview["missing"], ["claim:" + wish["id"]])
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", plan["refs"], revision=preview["revision"]).complete_json(request)
        self.assertEqual(self.online.requests, [])
        router.authorize(preview, preview["missing"])
        with patch("mindos.zhijun.memory_retrieval._index_scores", return_value={}):
            allowed = build_context_plan(router, question, [], provider=self.online)
        request = ChatRequest(system=allowed["system"], messages=[{"role": "user", "content": question}])
        preview = router.prepare("chat", request, allowed["refs"], self.online)
        self.assertEqual(preview["missing"], [])
        GuardedProvider(router, self.online, "chat", allowed["refs"], revision=preview["revision"]).complete_json(request)
        self.assertIn(text, self.online.requests[-1].system)
        self.assertIn("aspirational", self.online.requests[-1].system)
        self.assertNotIn(who["content"], self.online.requests[-1].system)
        self.assertEqual(self.onto.get_claim(wish["id"])["trustState"], "confirmed")
        self.assertIsNone(self.onto.get_claim(wish["id"])["selfAlignment"]["level"])

    def test_explicit_personal_fact_bypasses_only_missing_topic_not_existing_coverage(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        fact = self.claim("星桥项目预算是三十万元，团队只有三个人")
        unrelated = self.claim("我的团队今年要组织公开分享")
        self.grant(router, [router.ref("claim", unrelated["id"])])
        question = "我目前负责的星桥项目，预算上限是多少？"
        ranked = [{**unrelated, "score": .55}, {**fact, "score": .22}]
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=ranked):
            plan = build_context_plan(router, question, [], provider=self.online)
        self.assertIn(fact["id"], [r["id"] for r in plan["refs"]])
        request = ChatRequest(system=plan["system"], messages=[])
        self.assertEqual(router.prepare("chat", request, plan["refs"], self.online)["missing"], ["claim:" + fact["id"]])
        approved = self.claim("星桥项目预算上限已经核对为三十万元")
        self.grant(router, [router.ref("claim", approved["id"])])
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[{**approved, "score": .4}, {**fact, "score": .22}]):
            covered = build_context_plan(router, question, [], provider=self.online)
        self.assertNotIn(fact["id"], [r["id"] for r in covered["refs"]])

    def test_personal_fact_authorization_does_not_follow_old_topic_or_generic_similarity(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        wish = self.claim("我希望培养接班人，让团队更稳定")
        other_person = self.onto.upsert_entity("合成同事", "person")
        foreign_subject = self.onto.create_claim({"subject_entity_id": other_person["id"],
            "content": "合成同事已经培养出接班人", "section": "matters", "predicate": "happened", "layer": "self_declared"},
            [{"kind": "user_edit", "quote": "合成同事已经培养出接班人"}], trust_state="confirmed", trust_origin="user_created")
        pending = [{**wish, "score": .25}, {**foreign_subject, "score": .25}]
        history = [self.convs.append_message(self.cid, "user", "我想培养接班人", meta={"routingSources": []})]
        for question in ("换个话题，我已经实现开咖啡店的愿望了吗？", "我会不会很担心我的愿望？", "帮我解释培养接班人的一般方法"):
            with self.subTest(question=question), patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=pending):
                plan = build_context_plan(router, question, history, provider=self.online)
                self.assertNotIn(wish["id"], [r["id"] for r in plan["refs"]])
                self.assertNotIn(foreign_subject["id"], [r["id"] for r in plan["refs"]])
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[pending[1]]):
            not_about_me = build_context_plan(router, "我已经培养出接班人了吗？", [], provider=self.online)
        self.assertNotIn(foreign_subject["id"], [r["id"] for r in not_about_me["refs"]])
        self.store.set_handling("global", enabled=True, action="omit", service=service_info(self.online)["id"], expected_revision=0)
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=pending):
            omitted = build_context_plan(router, "我已经实现培养接班人的愿望了吗？", [], provider=self.online)
        self.assertNotIn(wish["id"], [r["id"] for r in omitted["refs"]])
        self.assertNotIn(foreign_subject["id"], [r["id"] for r in omitted["refs"]])
        self.assertNotIn("让团队更稳定", omitted["system"])

    def test_scope_and_deleted_ancestry_exclusions_do_not_poison_remaining_items(self):
        router = Router(self.onto, self.convs, self.cid)
        other = self.convs.create_conversation(device_scope="device:other")
        foreign = self.convs.append_message(other["id"], "user", "星桥项目另一设备机密", meta={"routingSources": []})
        lost = self.convs.create_conversation()
        message = self.convs.append_message(lost["id"], "user", "星桥项目旧资料", meta={"routingSources": []})
        bad = self.claim("星桥项目旧资料", evidence=[{"kind": "conversation_turn", "conversation_id": lost["id"], "message_id": message["id"], "quote": message["content"]}])
        self.convs.delete_conversation(lost["id"])
        good = self.claim("星桥项目当前有效经验")
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[{**bad, "score": 1}, {**good, "score": .8}]):
            plan = build_context_plan(router, "星桥项目", [], provider=self.local)
        self.assertNotIn("另一设备机密", plan["system"])
        self.assertNotIn("旧资料", plan["system"])
        self.assertIn("当前有效经验", plan["system"])
        self.assertIsNotNone(self.onto.get_claim(bad["id"]))

    def test_focus_preserves_slot_question_sources_and_does_not_promote_assistant_words(self):
        router = Router(self.onto, self.convs, self.cid)
        event = self.convs.append_message(self.cid, "user", "我想找人合作做星桥项目", meta={"routingSources": []})
        question = self.convs.append_message(self.cid, "assistant", "你希望对方主导，还是承担辅助角色？", meta={"routingSources": []})
        history = [event, question]
        plan = build_context_plan(router, "辅助角色", history, provider=self.local)
        self.assertTrue(plan["focus"]["continuation"])
        self.assertIn("星桥项目", plan["focus"]["query"])
        self.assertTrue({event["id"], question["id"]}.issubset({r["id"] for r in plan["focusRefs"]}))
        self.assertIn("最近的助手问题（是询问，不是用户事实）", plan["system"])
        tiny = fit_context_plan(plan, 1)
        self.assertEqual(tiny["focus"], plan["focus"])
        self.assertIn(question["id"], [r["id"] for r in tiny["refs"]])
        self.assertGreater(len(tiny["system"].encode()), 1, "mandatory focus is not silently chopped")

    def test_fit_rebuilds_ids_refs_and_revision_without_touching_input(self):
        router = Router(self.onto, self.convs, self.cid)
        claims = [self.claim(f"星桥项目需要先明确合作职责{i}") for i in range(12)]
        ranked = [{**c, "score": .9 - i * .01} for i, c in enumerate(claims)]
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=ranked):
            plan = build_context_plan(router, "星桥项目", [], provider=self.local, complex=True)
        self.assertLessEqual(len(plan["system"]), 1800)
        fitted = fit_context_plan(plan, 1200)
        self.assertLessEqual(len(fitted["system"].encode()), 1200)
        self.assertLess(len(fitted["evidence"]), len(plan["evidence"]))
        self.assertNotEqual(fitted["revision"], plan["revision"])
        self.assertEqual(fitted["providedRefs"], [i["citationId"] for i in fitted["background"] + fitted["evidence"]])
        self.assertEqual({r["id"] for r in fitted["refs"]}, {i["id"] for i in fitted["background"] + fitted["evidence"]})

    def test_revoked_or_changed_source_never_reaches_actual_external_payload(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        claim = self.claim("星桥项目预算必须先核对")
        self.grant(router, [router.ref("claim", claim["id"])])
        plan = build_context_plan(router, "星桥项目预算", [], provider=self.online)
        request = ChatRequest(system=plan["system"], messages=[])
        preview = router.prepare("chat", request, plan["refs"], self.online)
        self.store.revoke("global", "claim:" + claim["id"])
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", plan["refs"], revision=preview["revision"]).complete_json(request)
        self.assertEqual(self.online.requests, [])

    def test_material_snippet_requires_current_snapshot_and_explicit_attachments_are_not_duplicated(self):
        router = Router(self.onto, self.convs, self.cid)
        record = {"materialId": "synthetic-file", "versionNumber": 2, "fileName": "星桥合作方案.txt"}
        snapshot = {"snapshot_id": "synthetic-snapshot"}
        body = "星桥项目合作需要先明确保密范围。"
        item = context_sources._material_item(router, record, snapshot, body, body, .9)
        self.assertIsNone(context_sources._material_item(router, record, snapshot, body, "旧索引内容或AI总结", .9))
        with patch("mindos.zhijun.context_sources.material_candidates", return_value=[item]), \
                patch("mindos.zhijun.routing.read_ref", return_value=(record, snapshot, body)):
            plan = build_context_plan(router, "星桥项目合作", [], provider=self.local)
            self.assertIn(body, plan["system"])
            material = next(i["material"] for i in plan["evidence"] if i["kind"] == "material")
            self.assertEqual(material["locator"]["offset"], 0)
            self.assertEqual(material["snapshotId"], snapshot["snapshot_id"])
            excluded = build_context_plan(router, "星桥项目合作", [], provider=self.local,
                material_refs=[{"materialId": record["materialId"], "version": 2}])
            self.assertFalse(any(i["kind"] == "material" for i in excluded["evidence"]))

    def test_current_conversation_cutoff_blocks_history_and_summary_but_not_independent_memory(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        old = self.convs.append_message(self.cid, "user", "星桥项目旧聊天中的临时保密密码是合成暗号", meta={"routingSources": []})
        source = router.resolve(router.ref("message", old["id"]))[0]["ref"]
        summary = self.convs.save_summary(self.cid, up_to_seq=old["seq"], summary="星桥项目旧聊天中的临时保密密码是合成暗号", meta={"routingSources": [source]})
        claim = self.claim("星桥项目必须明确保密边界")
        self.grant(router, [source, router.ref("summary", f"{self.cid}:{summary['revision']}"), router.ref("claim", claim["id"])])
        self.store.set_mode(self.cid, "online", service_info(self.online)["id"], cutoff=old["seq"])
        router = Router(self.onto, self.convs, self.cid)
        plan = build_context_plan(router, "星桥项目保密", [], provider=self.online)
        self.assertNotIn("合成暗号", plan["system"])
        self.assertIn("必须明确保密边界", plan["system"])
        self.assertFalse(any(i["kind"] in ("message", "summary") for i in plan["evidence"]))
        self.assertTrue(any("不通过搜索" in item["reason"] for item in plan["excluded"]))

    def test_same_original_message_is_not_three_independent_pieces_of_evidence(self):
        router = Router(self.onto, self.convs, self.cid)
        text = "星桥项目合作必须先签保密协议"
        message = self.convs.append_message(self.cid, "user", text, meta={"routingSources": []})
        claim = self.claim(text, evidence=[{"kind": "conversation_turn", "conversation_id": self.cid,
            "message_id": message["id"], "quote": text}])
        self.convs.save_summary(self.cid, up_to_seq=message["seq"], summary=text,
            meta={"routingSources": [router.resolve(router.ref("message", message["id"]))[0]["ref"]]})
        plan = build_context_plan(router, "星桥项目保密协议", [], provider=self.local)
        self.assertEqual(len(plan["evidence"]), 1, plan["evidence"])
        self.assertEqual(len([x for x in plan["excluded"] if "内容重复" in x["reason"]]), 2)
        self.assertEqual(plan["evidence"][0]["supportSourceIds"], ["message:" + message["id"]])

    def test_context_and_exceptions_remain_whole_or_whole_item_is_dropped(self):
        router = Router(self.onto, self.convs, self.cid)
        claim = self.claim("星桥项目只在指定条件下共享资料")
        with self.onto._connect() as db:
            db.execute("UPDATE claims SET context_json=? WHERE id=?", (json.dumps({"situation": "公开路演" * 70,
                "exceptions": "但未签保密协议时绝不共享核心设计", "framing": "context_only"}), claim["id"]))
            db.commit()
        qualified = {**self.onto.get_claim(claim["id"]), "score": .9}
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[qualified]):
            plan = build_context_plan(router, "星桥项目共享资料", [], provider=self.local)
        self.assertIn("但未签保密协议时绝不共享核心设计", plan["system"])
        small = fit_context_plan(plan, 500)
        self.assertFalse(small["evidence"])
        self.assertNotIn("只在指定条件下共享", small["system"])

    def test_history_and_summary_scan_full_scope_before_ranking(self):
        router = Router(self.onto, self.convs, self.cid)
        old = self.convs.append_message(self.cid, "user", "星桥项目早期保密约定", meta={"routingSources": []})
        self.convs.save_summary(self.cid, up_to_seq=old["seq"], summary="星桥项目早期保密约定",
            meta={"routingSources": [router.ref("message", old["id"])]})
        with self.convs._connect() as db:
            db.execute("UPDATE messages SET created_at='2000-01-01' WHERE id=?", (old["id"],))
            db.execute("UPDATE conversation_summaries SET created_at='2000-01-01'")
            db.executemany("INSERT INTO messages(id,conversation_id,seq,role,content,meta_json,created_at) VALUES(?,?,?,'user','今天天气晴朗','{}','2026-01-01')",
                [(f"filler-{n}", self.cid, n + 2) for n in range(1201)])
            db.executemany("INSERT INTO conversations(id,mode,device_scope,created_at,updated_at) VALUES(?,'chat','global','2026-01-01','2026-01-01')",
                [(f"filler-conv-{n}",) for n in range(121)])
            db.executemany("INSERT INTO conversation_summaries(conversation_id,revision,up_to_seq,summary,generated_by,created_at) VALUES(?,1,1,'今天天气晴朗','synthetic','2026-01-01')",
                [(f"filler-conv-{n}",) for n in range(121)])
            db.commit()
        self.assertIn(old["id"], [c["ref"]["id"] for c in context_sources.history_candidates(router, ["星桥项目保密"])])
        self.assertIn(self.cid + ":1", [c["ref"]["id"] for c in context_sources.summary_candidates(router, ["星桥项目保密"])])

    def test_derived_summary_decision_and_episode_strip_old_markers_without_rewriting_storage(self):
        router = Router(self.onto, self.convs, self.cid)
        message = self.convs.append_message(self.cid, "user", "星桥项目需要复盘", meta={"routingSources": []})
        summary = self.convs.save_summary(
            self.cid,
            up_to_seq=message["seq"],
            summary="星桥项目摘要 [p0][p01]",
            key_points=["核心边界 [m0][m007]"],
            meta={"routingSources": [router.ref("message", message["id"])]},
        )
        summary_item = context_sources.summary_candidates(router, ["星桥项目摘要 核心边界"])[0]
        self.assertNotRegex(summary_item["text"], r"\[(?:p|m)\d+\]")
        stored_summary = self.convs.get_summary(self.cid, summary["revision"])
        self.assertIn("[p0]", stored_summary["summary"])
        self.assertIn("[m007]", stored_summary["keyPoints"][0])

        claim = self.claim("星桥项目复盘要核对结果")
        decision = self.decision(message, title="星桥项目复盘 [p00]", text="星桥项目当时情境 [p01][m0]")
        LearningStore(self.onto).start(decision, claim, self.cid, {
            "situation": "星桥项目合作 [p0]",
            "prediction": "边界会更清楚 [m007]",
        })
        items = context_sources.decision_candidates(router, ["星桥项目复盘 合作"])
        self.assertTrue({"decision", "episode"}.issubset({item["category"] for item in items}))
        for item in items:
            self.assertNotRegex(item["title"], r"\[(?:p|m)\d+\]")
            self.assertNotRegex(item["text"], r"\[(?:p|m)\d+\]")
        self.assertIn("[p01]", GrowthStore.instance().get_decision(decision["id"])["context"])
        self.assertIn("[m007]", LearningStore(self.onto).get(decision["id"])["expectation"]["prediction"])

    def test_retry_upper_bound_excludes_current_future_and_derived_text(self):
        router = Router(self.onto, self.convs, self.cid)
        old = self.convs.append_message(self.cid, "user", "星桥项目过去谈过合作范围", meta={"routingSources": []})
        router.context_before_seq = old["seq"] + 1
        before = build_context_plan(router, "星桥项目", [], provider=self.local)
        current = self.convs.append_message(self.cid, "user", "星桥项目本轮请求不能重复检索", meta={"routingSources": []})
        self.convs.append_message(self.cid, "user", "星桥项目未来事实合成标记", meta={"routingSources": []})
        after = build_context_plan(router, "星桥项目", [], provider=self.local)
        self.assertEqual(before["revision"], after["revision"], "persisting the current turn must not change its own preview")
        self.convs.save_summary(self.cid, up_to_seq=current["seq"], summary="星桥项目本轮请求不能重复检索",
            meta={"routingSources": [router.ref("message", current["id"])]})
        claim = self.claim("星桥项目确认过的独立保密原则")
        decision = self.decision(current)
        LearningStore(self.onto).start(decision, claim, self.cid,
            {"situation": "星桥项目合作", "prediction": "星桥项目未来事实合成标记"})
        plan = build_context_plan(router, "星桥项目", [], provider=self.local)
        self.assertNotIn("本轮请求不能重复检索", plan["system"])
        self.assertNotIn("未来事实合成标记", plan["system"])
        self.assertIn(claim["content"], plan["system"])
        self.assertFalse(any(i["category"] in ("summary", "episode", "decision") for i in plan["evidence"]))

    def test_explicit_lookup_relevant_missing_source_is_not_hidden_by_other_evidence(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        permitted = self.claim("星桥项目当前在做设计")
        missing = self.claim("星桥保密协议")
        self.grant(router, [router.ref("claim", permitted["id"])])
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[{**permitted, "score": .9}, {**missing, "score": 1}]):
            plan = build_context_plan(router, "星桥项目", [], provider=self.online, queries=["星桥保密协议"])
        self.assertEqual({i["id"] for i in plan["evidence"]}, {permitted["id"], missing["id"]})
        request = ChatRequest(system=plan["system"], messages=[])
        preview = router.prepare("chat", request, plan["refs"], self.online)
        self.assertIn("claim:" + missing["id"], preview["missing"])
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", plan["refs"], revision=preview["revision"]).complete_json(request)
        self.assertEqual(self.online.requests, [])

    def test_retry_can_use_latest_summary_before_its_upper_bound(self):
        router = Router(self.onto, self.convs, self.cid)
        old = self.convs.append_message(self.cid, "user", "星桥项目旧合作约定", meta={"routingSources": []})
        first = self.convs.save_summary(self.cid, up_to_seq=old["seq"], summary="星桥项目旧合作约定",
            meta={"routingSources": [router.ref("message", old["id"])]})
        current = self.convs.append_message(self.cid, "user", "星桥项目新请求", meta={"routingSources": []})
        self.convs.save_summary(self.cid, up_to_seq=current["seq"], summary="星桥项目新请求",
            meta={"routingSources": [router.ref("message", current["id"])]})
        router.context_before_seq = current["seq"]
        candidates = context_sources.summary_candidates(router, ["星桥项目"])
        self.assertEqual([i["ref"]["id"] for i in candidates], [f"{self.cid}:{first['revision']}"])

    def test_snapshot_search_covers_older_file_and_later_text(self):
        router = Router(self.onto, self.convs, self.cid)
        files = [{"material_id": f"material-{n}"} for n in range(81)]
        def require(ident, scope):
            self.assertEqual(scope, "global")
            return {"materialId": ident, "versionNumber": 1, "fileName": "合成资料.txt"}
        def read(ref, scope):
            body = "与查询无关的内容" if ref["materialId"] != "material-80" else "空白 " * 41000 + "星桥保密协议需要签字"
            return require(ref["materialId"], scope), {"snapshot_id": "synthetic-snapshot"}, body
        with patch.dict("os.environ", {"ZHIJUN_MATERIAL_EVIDENCE": "1"}), \
             patch("mindos.chat_imports.require_material", side_effect=require), \
             patch("mindos.chat_imports.read_ref", side_effect=read), \
             patch("mindos.services.ingestion.JobStore.instance", return_value=SimpleNamespace(list=lambda **_: files)):
            items = context_sources.material_candidates(router, ["星桥保密协议"])
        self.assertTrue(any(i["ref"]["id"] == "material-80" and i["material"]["locator"]["offset"] >= 120000 for i in items))

    def test_two_distinct_file_windows_keep_one_parent_and_at_most_two_slots(self):
        router = Router(self.onto, self.convs, self.cid)
        record = {"materialId": "synthetic-file", "versionNumber": 2, "fileName": "星桥合作方案.txt"}
        snapshot = {"snapshot_id": "synthetic-snapshot"}
        snippets = ["预算金额五万元，购买仪器时分期支付。", "截止日期是九月三十日，验收时需要提交设计图。", "联系人姓名写作张三，会议在办公室举行。"]
        body = ("\n" * 1000).join(snippets)
        items = [context_sources._material_item(router, record, snapshot, body, snippet, 1 - n * .1) for n, snippet in enumerate(snippets)]
        with patch("mindos.zhijun.context_sources.material_candidates", return_value=items), \
                patch("mindos.zhijun.routing.read_ref", return_value=(record, snapshot, body)):
            plan = build_context_plan(router, "预算和截止日期", [], provider=self.local)
        self.assertEqual(len(plan["evidence"]), 2)
        self.assertIn(snippets[0], plan["system"])
        self.assertIn(snippets[1], plan["system"])
        self.assertNotIn(snippets[2], plan["system"])
        self.assertEqual(len(plan["refs"]), 1, "two windows inherit one material-version authorization")
        self.assertEqual(plan["evidence"][0]["supportSourceIds"], plan["evidence"][1]["supportSourceIds"])
        self.assertNotEqual(plan["evidence"][0]["material"]["locator"], plan["evidence"][1]["material"]["locator"])
        self.assertEqual(plan["providedRefs"], ["p1", "p2"])

    def test_changed_claim_or_background_between_retrieval_and_resolve_is_not_relabelled(self):
        router = Router(self.onto, self.convs, self.cid)
        claim = self.claim("星桥项目旧秘密正文")
        background = self.claim("我是旧秘密团队的负责人", who=True)
        with self.onto._connect() as db:
            db.execute("UPDATE claims SET content='已替换的新内容' WHERE id IN (?,?)", (claim["id"], background["id"]))
            db.commit()
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[{**claim, "score": .9}]), \
             patch("mindos.zhijun.memory_retrieval.confirmed_background", return_value=[background]):
            plan = build_context_plan(router, "星桥项目", [], provider=self.local)
        self.assertFalse(plan["background"])
        self.assertFalse(plan["evidence"])
        self.assertNotIn("旧秘密", plan["system"])
        self.assertEqual(len(plan["excluded"]), 2)

    def test_changed_history_and_snapshot_do_not_bind_old_text_to_new_version(self):
        router = Router(self.onto, self.convs, self.cid)
        message = self.convs.append_message(self.cid, "user", "星桥项目原始合成秘密", meta={"routingSources": []})
        candidates = context_sources.history_candidates(router, ["星桥项目"])
        record = {"materialId": "synthetic-file", "versionNumber": 2, "fileName": "合成材料.txt"}
        old_snapshot, new_snapshot = {"snapshot_id": "old-snapshot"}, {"snapshot_id": "new-snapshot"}
        item = context_sources._material_item(router, record, old_snapshot, "星桥项目原始合成秘密", "星桥项目原始合成秘密", .9)
        with self.convs._connect() as db:
            db.execute("UPDATE messages SET content='已经替换' WHERE id=?", (message["id"],))
            db.commit()
        with patch("mindos.zhijun.context_sources.history_candidates", return_value=candidates), \
             patch("mindos.zhijun.context_sources.material_candidates", return_value=[item]), \
             patch("mindos.zhijun.routing.read_ref", return_value=(record, new_snapshot, "已经替换")):
            plan = build_context_plan(router, "星桥项目", [], provider=self.local)
        self.assertFalse(plan["evidence"])
        self.assertNotIn("原始合成秘密", plan["system"])

    def test_changed_focus_source_requires_reassembly(self):
        router = Router(self.onto, self.convs, self.cid)
        message = self.convs.append_message(self.cid, "user", "我想找人合作做星桥项目", meta={"routingSources": []})
        question = self.convs.append_message(self.cid, "assistant", "你希望对方主导，还是承担辅助角色？", meta={"routingSources": []})
        with self.convs._connect() as db:
            db.execute("UPDATE messages SET content='这段话已被替换' WHERE id=?", (message["id"],))
            db.commit()
        with self.assertRaises(HTTPException):
            build_context_plan(router, "辅助角色", [message, question], provider=self.local)

    def test_layer_or_time_change_invalidates_claim_snapshot_version(self):
        router = Router(self.onto, self.convs, self.cid)
        claim = self.claim("星桥项目现在只适用于阶段工作")
        original = context_sources.claim_ref(router, claim)
        with self.onto._connect() as db:
            db.execute("UPDATE claims SET valid_to='2026-09-30' WHERE id=?", (claim["id"],))
            db.commit()
        resolved = router.resolve(original)
        self.assertEqual(resolved[0]["blockedReason"], "version_changed")
        with self.assertRaises(HTTPException):
            router.check_lifecycle(resolved)
        current = self.onto.get_claim(claim["id"])
        self.assertEqual(router.resolve(context_sources.claim_ref(router, current))[0]["blocked"], "")

    def test_background_does_not_silently_truncate_an_identity_exception(self):
        router = Router(self.onto, self.convs, self.cid)
        claim = self.claim("我是星桥产品负责人", who=True)
        exception = "阶段角色需要结合实际职责说明。" * 12 + "但这不代表私人生活中的所有角色"
        with self.onto._connect() as db:
            db.execute("UPDATE claims SET context_json=? WHERE id=?", (json.dumps({"exceptions": exception}), claim["id"]))
            db.commit()
        plan = build_context_plan(router, "你好", [], provider=self.local)
        self.assertTrue(plan["background"])
        self.assertIn(exception, plan["background"][0]["text"])
        self.assertLessEqual(sum(len(i["text"]) for i in plan["background"]), 600)


if __name__ == "__main__":
    unittest.main()
