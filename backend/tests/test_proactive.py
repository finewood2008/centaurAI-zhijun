"""知君的主动性（V3 M8）：策略往返、各触发源的开场与 meta、节律闸门、发起会话、回应、今日列表、调度。"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import conversations, nudges, zhijun_home
from mindos.stores import conversation_store as conversation_store_module
from mindos.stores import growth_store as growth_store_module
from mindos.stores import ontology_store as ontology_store_module
from mindos.stores.charter_draft_store import render_document
from mindos.stores.conversation_store import PROACTIVE_DEFAULTS, ConversationError, ConversationStore
from mindos.zhijun import inquiry, jobs, persona, proactive
from mindos.zhijun import nudges as nudge_service


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decision(title: str, review_at: datetime | None, **extra) -> dict:
    return {"title": title, "context": "背景", "options": ["A", "B"], "choice": "A", "rationale": "因为 A 更稳", "confidence": 60,
            "expectedOutcome": "两周内见效", "reviewAt": _iso(review_at) if review_at else None, "relatedEntityIds": [], "evidenceRefs": [], **extra}


class ProactiveBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.onto = ontology_store_module.reset_for_tests(root / "ontology.db")
        self.convs = conversation_store_module.reset_for_tests(root / "conversations.db")
        self.growth = growth_store_module.reset_for_tests(root / "growth.db")
        self._env = patch.dict(os.environ, {"ZHIJUN_PROVIDER": "fake", "ZHIJUN_MATERIAL_EVIDENCE": "0", "ZHIJUN_EXTRACTION": "0"})
        self._env.start()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        # 本地时间固定为上午 10 点附近：默认安静时段（22:00–08:00）之外，+4h / +8h 仍在同一天。
        offset = (10 - self.now.hour) % 24
        self._tz = patch.object(proactive, "local_tz", return_value=timezone(timedelta(hours=offset)))
        self._tz.start()
        app = FastAPI()
        app.include_router(conversations.router)
        app.include_router(nudges.router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._tz.stop()
        self._env.stop()
        self._tmp.cleanup()

    # ---- 取材
    def claim(self, content, *, section="who", predicate=None, trust="confirmed", privacy="private", valid_to=None):
        payload = {"content": content, "section": section, "layer": "self_declared" if trust == "confirmed" else "hypothesis", "privacy_level": privacy, "valid_to": valid_to}
        if predicate:
            payload["predicate"] = predicate
        return self.onto.create_claim(payload, [{"kind": "user_edit", "quote": content}], trust_state=trust,
                                      trust_origin="user_created" if trust == "confirmed" else "model")

    def conversation_at(self, at: datetime, **kwargs) -> dict:
        with patch("mindos.stores.conversation_store.utc_now", return_value=_iso(at)):
            return self.convs.create_conversation(**kwargs)

    def said(self, conversation_id: str, text: str, at: datetime, role: str = "user") -> dict:
        with patch("mindos.stores.conversation_store.utc_now", return_value=_iso(at)):
            return self.convs.append_message(conversation_id, role, text)

    def initiated_at(self, at: datetime, *, answered: bool = False, kind="greeting") -> dict:
        info = {"by": "zhijun", "kind": kind, "whyNow": "测试", "createdAt": _iso(at), "answeredAt": _iso(at + timedelta(hours=1)) if answered else None}
        return self.conversation_at(at, mode="chat", title="知君发起", initiated=info)

    def publish(self, clauses):
        current = (self.growth.current_charter(scope="global") or {}).get("version", 0)
        return self.growth.create_charter({"document": render_document(clauses), "clauses": clauses, "expectedVersion": current,
                                           "workspaceId": "synthetic-test-workspace", "metadata": {"scope": "global", "origin": "workspace", "sources": []}})

    def candidates(self, now=None, scope="global"):
        return proactive.candidates(now or self.now, store=self.onto, convs=self.convs, growth=self.growth, scope=scope)

    def scan(self, now=None, scope="global"):
        return proactive.run(now or self.now, store=self.onto, convs=self.convs, growth=self.growth, scope=scope)

    def allowed(self, now=None):
        return proactive.allowed(now or self.now, self.convs.nudge_policy(), self.convs, "global")


class PolicyTests(ProactiveBase):
    def test_defaults_round_trip_partial_update_and_old_clients(self) -> None:
        self.assertEqual(self.client.get("/api/mindos/nudges/policy").json()["proactive"], PROACTIVE_DEFAULTS)
        snooze = _iso(self.now + timedelta(days=3))
        res = self.client.put("/api/mindos/nudges/policy", json={"proactive": {"maxPerDay": 3, "quietHours": {"start": "23:00"}, "snoozeUntil": snooze}})
        self.assertEqual(res.status_code, 200, res.text)
        expected = {**PROACTIVE_DEFAULTS, "maxPerDay": 3, "quietHours": {"start": "23:00", "end": "08:00"}, "snoozeUntil": snooze}
        self.assertEqual(res.json()["proactive"], expected)
        self.assertEqual(self.client.get("/api/mindos/nudges/policy").json()["proactive"], expected)
        # null 清除 snooze；没传的字段保留
        cleared = self.client.put("/api/mindos/nudges/policy", json={"proactive": {"snoozeUntil": None, "greetAfterDays": 0}}).json()["proactive"]
        self.assertEqual(cleared, {**expected, "snoozeUntil": None, "greetAfterDays": 0})
        # 旧客户端只改提醒字段：proactive 不动
        old = self.client.put("/api/mindos/nudges/policy", json={"maxPerDay": 5, "enabled": False}).json()
        self.assertEqual((old["maxPerDay"], old["enabled"], old["proactive"]), (5, False, cleared))
        # 静默 / 退避标记与其它字段一起往返
        self.convs.save_nudge_policy(proactive={"backoffUntil": snooze})
        self.assertEqual(self.client.get("/api/mindos/nudges/policy").json()["proactive"]["backoffUntil"], snooze)

    def test_settings_page_shapes_full_object_and_snooze_only(self) -> None:
        """前端设置页把读回的整个 proactive 原样 PUT（含只读 backoffUntil）；「先别找我」只 PUT {proactive: {...existing, snoozeUntil}}。"""
        server_backoff = _iso(self.now + timedelta(days=5))
        self.convs.save_nudge_policy(proactive={"backoffUntil": server_backoff})
        read_back = self.client.get("/api/mindos/nudges/policy").json()["proactive"]
        self.assertEqual(read_back["backoffUntil"], server_backoff)
        edited = {**read_back, "maxPerDay": 1, "minGapHours": 6, "quietHours": {"start": "21:30", "end": "09:00"}, "greetAfterDays": 10, "backoffUntil": None}
        res = self.client.put("/api/mindos/nudges/policy", json={"proactive": edited})
        self.assertEqual(res.status_code, 200, res.text)
        saved = res.json()
        self.assertEqual(saved["proactive"], {**edited, "backoffUntil": server_backoff})  # 客户端的 backoffUntil 被忽略
        self.assertEqual((saved["enabled"], saved["maxPerDay"], saved["silencedRefs"]), (True, 3, []))  # 顶层字段没传就不动
        stale_backoff = {**saved["proactive"], "backoffUntil": _iso(self.now + timedelta(days=30))}
        self.assertEqual(self.client.put("/api/mindos/nudges/policy", json={"proactive": stale_backoff}).json()["proactive"]["backoffUntil"], server_backoff)
        # snoozeProactive：只带 snoozeUntil 的完整对象
        snooze = _iso(self.now + timedelta(days=3))
        res = self.client.put("/api/mindos/nudges/policy", json={"proactive": {**saved["proactive"], "snoozeUntil": snooze}})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["proactive"], {**saved["proactive"], "snoozeUntil": snooze})
        self.assertEqual(proactive.allowed(self.now, res.json(), self.convs, "global"), (False, "snoozed"))
        res = self.client.put("/api/mindos/nudges/policy", json={"proactive": {**res.json()["proactive"], "snoozeUntil": None}})
        self.assertIsNone(res.json()["proactive"]["snoozeUntil"])
        self.assertEqual(res.json()["proactive"]["backoffUntil"], server_backoff)

    def test_validation_ranges(self) -> None:
        for body in ({"maxPerDay": 6}, {"maxPerDay": -1}, {"minGapHours": 0}, {"minGapHours": 25}, {"greetAfterDays": -1},
                     {"quietHours": {"start": "25:00"}}, {"quietHours": {"end": "8:00"}}, {"quietHours": {"noon": "12:00"}}, {"unknown": 1}, {"enabled": "yes"}):
            self.assertEqual(self.client.put("/api/mindos/nudges/policy", json={"proactive": body}).status_code, 422, body)
        self.assertEqual(self.client.put("/api/mindos/nudges/policy", json={"proactive": {"snoozeUntil": "not-a-date"}}).status_code, 400)
        for bad in ({"maxPerDay": 9}, {"minGapHours": "4"}, {"quietHours": {"start": "22:00", "end": "0800"}}, {"backoffUntil": 12}, {"nope": True}):
            with self.assertRaises(ConversationError, msg=bad):
                self.convs.save_nudge_policy(proactive=bad)
        self.assertEqual(self.convs.nudge_policy()["proactive"], PROACTIVE_DEFAULTS)

    def test_legacy_policy_row_without_proactive_column_reads_defaults(self) -> None:
        legacy = Path(self._tmp.name) / "legacy.db"
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE nudge_policies(key TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, max_per_day INTEGER NOT NULL DEFAULT 3,"
                       " silenced_refs_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL)")
            db.execute("INSERT INTO nudge_policies VALUES('default', 0, 4, '[\"x\"]', '2025-01-01')")
        store = ConversationStore(legacy)
        policy = store.nudge_policy()
        self.assertEqual((policy["enabled"], policy["maxPerDay"], policy["silencedRefs"], policy["proactive"]), (False, 4, ["x"], PROACTIVE_DEFAULTS))
        self.assertEqual(store.save_nudge_policy(proactive={"maxPerDay": 1})["proactive"]["maxPerDay"], 1)
        self.assertIsNone(store.create_conversation()["initiated"])


class TriggerTests(ProactiveBase):
    def test_each_trigger_kind_has_opening_title_why_now_and_priority(self) -> None:
        start = self.now - timedelta(days=6)  # 认识第 7 天
        origin = self.conversation_at(start)
        self.said(origin["id"], "我是产品负责人", start)  # 6 天没聊
        self.convs.save_nudge_policy(proactive={"greetAfterDays": 3})
        me = self.claim("我是产品负责人", predicate="role")
        stale = self.claim("我是一个很久没提过的身份", predicate="role")
        with self.onto._connect() as db:
            db.execute("UPDATE claims SET last_reaffirmed=? WHERE id=?", (_iso(self.now - timedelta(days=90)), stale["id"]))
        self.claim("我可能更适合上午处理难题", predicate="has_trait", trust="working")
        self.convs.save_summary(origin["id"], up_to_seq=1, summary="s", key_points=["主题", "待办：约投资人"], meta={"routingSources": []})
        overdue = self.growth.create_decision(_decision("要不要涨价", self.now - timedelta(days=2)))
        commit = self.claim("我承诺三个月内把团队招齐", section="matters", predicate="committed_to", valid_to=_iso(self.now - timedelta(hours=2)))
        self.assertEqual(nudge_service.scan(conv_store=self.convs, growth=self.growth, now=self.now)["created"], 2)
        a = self.claim("我坚持周末不工作", section="principles")
        b = self.claim("我最近周末都在加班", section="ways")
        basis = {"charterBasis": {"scope": "global"}}
        self.convs.create_nudge(kind="principle_tension", trigger_key="tension:x", trigger_ref={"claimIds": [a["id"], b["id"]], **basis},
                                why_now="两条理解放在一起有张力", message="有张力", scheduled_for=_iso(self.now), now=_iso(self.now))
        self.convs.create_nudge(kind="weekly_review", trigger_key="weekly:x", trigger_ref={"summary": "这周你记下了 3 条关于自己的理解", **basis},
                                why_now="一周过去了", message="要不要看看", scheduled_for=_iso(self.now), now=_iso(self.now))
        self.convs.create_nudge(kind="review_due", trigger_key="future", trigger_ref={"decisionId": overdue["id"], **basis},
                                why_now="还没到", message="m", scheduled_for=_iso(self.now + timedelta(days=1)), now=_iso(self.now))

        found = self.candidates()
        by_kind = {item["kind"]: item for item in found}
        self.assertEqual([item["kind"] for item in found], sorted((item["kind"] for item in found), key=lambda k: proactive._PRIORITY[k]))
        self.assertEqual(found[0]["kind"], "review_due")
        self.assertEqual(set(by_kind), {"review_due", "commitment_due", "principle_tension", "weekly_review", "open_loop", "milestone", "nod", "stale", "gap", "greeting"})
        for item in found:
            self.assertTrue(item["whyNow"] and item["title"] and item["key"] and item["opening"], item)
            self.assertLessEqual(len(item["title"]), 30)
            self.assertLessEqual(item["opening"].count("？"), 1, item["opening"])
        self.assertEqual(by_kind["review_due"]["opening"], "「要不要涨价」到了你当时定的回访日。先不用急着说结果，这段时间感觉怎么样？")
        self.assertEqual(by_kind["review_due"]["refs"], [{"kind": "decision", "id": overdue["id"]}])
        self.assertIn("已经过了 2 天", by_kind["review_due"]["whyNow"])
        self.assertEqual(by_kind["review_due"]["nudgeId"], next(n["id"] for n in self.convs.list_nudges() if n["triggerKey"] == nudge_service.trigger_key_for(overdue["id"])))
        self.assertTrue(by_kind["commitment_due"]["opening"].startswith("你说过「我承诺三个月内把团队招齐」，期限是"))
        self.assertTrue(by_kind["commitment_due"]["opening"].endswith("进展怎么样？不想聊也可以先放着。"))
        self.assertEqual(by_kind["commitment_due"]["refs"], [{"kind": "claim", "id": commit["id"]}])
        self.assertEqual(by_kind["principle_tension"]["opening"], "「我坚持周末不工作」是你确认过的原则，而最近「我最近周末都在加班」。是原则变了，还是这次情况特殊？")
        self.assertEqual(by_kind["weekly_review"]["opening"], "一周过去了。这周你记下了 3 条关于自己的理解——要不要花几分钟一起看看？不想看也没关系。")
        self.assertEqual(by_kind["open_loop"]["opening"], "上次你说要「约投资人」，后来怎么样了？")
        self.assertEqual(by_kind["open_loop"]["title"], "上次说到：约投资人")
        self.assertEqual(by_kind["open_loop"]["refs"], [{"kind": "summary", "id": f"{origin['id']}:1"}])
        self.assertTrue(by_kind["milestone"]["opening"].startswith("今天是我们认识的第 7 天。这段时间你记下的事里，有一件我一直记着："), by_kind["milestone"]["opening"])
        quoted = by_kind["milestone"]["opening"].split("记着：", 1)[1].rstrip("。")
        confirmed = {c["id"]: c["content"] for c in self.onto.list_claims(trust_states=("confirmed",), limit=50)}
        self.assertEqual(confirmed.get(by_kind["milestone"]["refs"][0]["id"]), quoted)
        self.assertIn(me["content"], confirmed.values())
        self.assertEqual(by_kind["milestone"]["key"], "milestone:7")
        self.assertTrue(by_kind["nod"]["opening"].startswith("有件事我一直没把握。我印象里我可能更适合上午处理难题"))
        self.assertTrue(by_kind["stale"]["opening"].startswith("有段时间没听你提起了。上次你说「我是一个很久没提过的身份」"))
        self.assertTrue(by_kind["gap"]["opening"].startswith("我们认识不久，有些地方还不了解。"))
        self.assertEqual(by_kind["greeting"]["opening"], persona.PROACTIVE_GREETING)
        self.assertEqual(by_kind["greeting"]["whyNow"], "已经 6 天没聊了")
        # 同一判断只出现一次：到期判断的求知目标被 review_due 提醒吸收；未到计划时间的提醒不算。
        self.assertEqual(sum(1 for item in found if any(r.get("id") == overdue["id"] for r in item["refs"])), 1)
        self.assertNotIn("nudge:future", {item["key"] for item in found})

    def test_gap_only_in_first_two_weeks_and_greeting_can_be_off(self) -> None:
        start = self.now - timedelta(days=20)
        origin = self.conversation_at(start)
        self.said(origin["id"], "我是产品负责人", start)
        self.claim("我是产品负责人", predicate="role")
        kinds = {item["kind"] for item in self.candidates()}
        self.assertNotIn("gap", kinds)
        self.assertIn("greeting", kinds)
        self.convs.save_nudge_policy(proactive={"greetAfterDays": 0})
        self.assertNotIn("greeting", {item["kind"] for item in self.candidates()})
        self.assertNotIn("milestone", kinds)
        self.assertEqual(proactive.relationship_days(self.onto, self.convs, self.growth, "global", now=self.now), 21)

    def test_milestone_without_quotable_line(self) -> None:
        self.conversation_at(self.now - timedelta(days=29))
        found = [item for item in self.candidates() if item["kind"] == "milestone"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["opening"], "今天是我们认识的第 30 天。想聊什么都可以，不聊也没关系。")
        self.assertEqual(found[0]["refs"], [])

    def test_quiet_words_charter_and_cooldown_filter_candidates(self) -> None:
        quiet = self.growth.create_decision(_decision("家庭矛盾要不要摊开说", self.now - timedelta(days=1)))
        loud = self.growth.create_decision(_decision("要不要涨价", self.now - timedelta(days=1)))
        basis = {"charterBasis": {"scope": "global"}}
        for decision in (quiet, loud):
            self.convs.create_nudge(kind="review_due", trigger_key=nudge_service.trigger_key_for(decision["id"]), trigger_ref={"decisionId": decision["id"], **basis},
                                    why_now="到了回访日", message="m", scheduled_for=_iso(self.now), now=_iso(self.now))
        self.assertEqual({item["payload"]["decisionId"] for item in self.candidates()}, {quiet["id"], loud["id"]})
        self.growth.create_charter({"vision": "v", "roles": [], "principles": [], "boundaries": [], "goals": [], "challengeStyle": "温和", "quietDomains": ["家庭"]})
        self.assertEqual([item["payload"]["decisionId"] for item in self.candidates()], [loud["id"]])
        # 7 天不重复：发过的 key 冷却，过期后回来
        proactive.mark_sent(self.onto, "global", "nudge:" + nudge_service.trigger_key_for(loud["id"]), now=self.now)
        self.assertEqual(self.candidates(), [])
        self.assertEqual([item["payload"]["decisionId"] for item in self.candidates(self.now + timedelta(days=8))], [loud["id"]])
        self.assertEqual(proactive.recent_keys(self.onto, "global", now=self.now + timedelta(days=8)), {})
        # 章程关闭主动：什么都不找
        self.publish([{"id": "quiet", "section": "与知君合作", "text": "不要主动提醒我", "kind": "boundary", "scope": "global", "context": "",
                       "control": "no_proactive", "sources": [], "quote": "不要主动提醒我"}])
        self.assertEqual(self.candidates(self.now + timedelta(days=8)), [])
        self.assertEqual(self.scan(self.now + timedelta(days=8)), {"created": 0, "reason": "charter_no_proactive"})


class RhythmTests(ProactiveBase):
    def test_enabled_snooze_and_quiet_hours(self) -> None:
        self.assertEqual(self.allowed(), (True, "ok"))
        self.convs.save_nudge_policy(proactive={"enabled": False})
        self.assertEqual(self.allowed(), (False, "disabled"))
        self.convs.save_nudge_policy(proactive={"enabled": True, "snoozeUntil": _iso(self.now + timedelta(days=3))})
        self.assertEqual(self.allowed(), (False, "snoozed"))
        self.assertEqual(self.allowed(self.now + timedelta(days=3, minutes=1)), (True, "ok"))
        self.convs.save_nudge_policy(proactive={"snoozeUntil": None})
        self.assertEqual(self.allowed(self.now + timedelta(hours=12)), (False, "quiet_hours"))  # 本地 22 点
        self.assertEqual(self.allowed(self.now + timedelta(hours=21)), (False, "quiet_hours"))  # 本地 07 点
        self.assertEqual(self.allowed(self.now + timedelta(hours=22)), (True, "ok"))  # 本地 08 点
        naive = datetime(2026, 1, 1, 23, 30)  # naive 视为本地时间
        self.assertEqual(self.allowed(naive), (False, "quiet_hours"))
        self.assertEqual(self.allowed(datetime(2026, 1, 1, 12, 0)), (True, "ok"))
        self.convs.save_nudge_policy(proactive={"quietHours": {"start": "12:00", "end": "13:00"}})
        self.assertEqual(self.allowed(self.now + timedelta(hours=2)), (False, "quiet_hours"))
        self.assertEqual(self.allowed(self.now + timedelta(hours=12)), (True, "ok"))
        self.convs.save_nudge_policy(proactive={"quietHours": {"start": "00:00", "end": "00:00"}})
        self.assertEqual(self.allowed(self.now + timedelta(hours=12)), (True, "ok"))

    def test_daily_cap_min_gap_and_recent_user_activity(self) -> None:
        self.initiated_at(self.now - timedelta(hours=1))
        self.assertEqual(self.allowed(), (False, "min_gap"))
        self.assertEqual(self.allowed(self.now + timedelta(hours=3, minutes=1)), (True, "ok"))
        self.initiated_at(self.now - timedelta(hours=5))
        self.assertEqual(self.allowed(self.now + timedelta(hours=3, minutes=1)), (False, "daily_cap"))
        self.convs.save_nudge_policy(proactive={"maxPerDay": 3})
        self.assertEqual(self.allowed(self.now + timedelta(hours=3, minutes=1)), (True, "ok"))
        self.convs.save_nudge_policy(proactive={"maxPerDay": 0})
        self.assertEqual(self.allowed(self.now + timedelta(days=2)), (False, "daily_cap"))
        self.convs.save_nudge_policy(proactive={"maxPerDay": 2, "minGapHours": 1})
        self.assertEqual(self.allowed(self.now + timedelta(days=1)), (True, "ok"))
        conv = self.conversation_at(self.now - timedelta(days=3))
        self.said(conv["id"], "刚聊过", self.now + timedelta(days=1) - timedelta(minutes=30))
        self.assertEqual(self.allowed(self.now + timedelta(days=1)), (False, "user_active"))
        self.assertEqual(self.allowed(self.now + timedelta(days=1, hours=2)), (True, "ok"))
        self.said(conv["id"], "知君的话不算活跃", self.now + timedelta(days=1, hours=2), role="assistant")
        self.assertEqual(self.allowed(self.now + timedelta(days=1, hours=2)), (True, "ok"))

    def test_backoff_after_three_unanswered_and_release_on_reply(self) -> None:
        for days in (3, 2, 1):
            self.initiated_at(self.now - timedelta(days=days))
        self.assertEqual(self.allowed(), (False, "backoff"))
        self.assertEqual(self.convs.nudge_policy()["proactive"]["backoffUntil"], _iso(self.now - timedelta(days=1) + timedelta(days=7)))
        self.assertEqual(self.allowed(self.now + timedelta(days=5)), (False, "backoff"))
        self.assertEqual(self.allowed(self.now + timedelta(days=6, hours=1)), (True, "ok"))
        # 一周只找一次：又发了一条没回的 → 再退一周
        self.initiated_at(self.now + timedelta(days=6, hours=1))
        self.assertEqual(self.allowed(self.now + timedelta(days=7)), (False, "backoff"))
        self.assertEqual(self.allowed(self.now + timedelta(days=13, hours=2)), (True, "ok"))
        # 他回了最近一条：退避解除、标记清掉（但两小时内算活跃）
        latest = self.convs.list_initiated(device_scope="global", limit=1)[0]
        self.said(latest["id"], "在的", self.now + timedelta(days=7, hours=1))
        self.assertEqual(self.allowed(self.now + timedelta(days=7, hours=4)), (True, "ok"))
        self.assertIsNone(self.convs.nudge_policy()["proactive"]["backoffUntil"])
        self.assertEqual(self.allowed(self.now + timedelta(days=7, hours=2)), (False, "user_active"))


class RunTests(ProactiveBase):
    def test_run_creates_one_initiated_conversation_marks_nudge_acted_and_reply_answers(self) -> None:
        origin = self.conversation_at(self.now - timedelta(days=3))
        self.said(origin["id"], "三个月内把团队招齐", self.now - timedelta(days=3))
        commit = self.claim("我承诺三个月内把团队招齐", section="matters", predicate="committed_to", valid_to=_iso(self.now - timedelta(hours=2)))
        nudge_service.scan(conv_store=self.convs, growth=self.growth, now=self.now)
        nudge = next(n for n in self.convs.list_nudges() if n["kind"] == "commitment_due")
        self.assertEqual(self.client.get("/api/mindos/nudges/today").json()["items"][0]["id"], nudge["id"])

        with patch("mindos.zhijun.provider.build_provider", side_effect=AssertionError("proactive opening must not call a model")):
            result = self.scan()
        self.assertEqual((result["created"], result["reason"], result["kind"]), (1, "commitment_due", "commitment_due"))
        conversation = self.convs.get_conversation(result["conversationId"])
        self.assertEqual(conversation["mode"], "chat")
        self.assertEqual(conversation["title"], "承诺到期：我承诺三个月内把团队招齐")
        self.assertEqual(conversation["initiated"], {"by": "zhijun", "kind": "commitment_due", "whyNow": nudge["whyNow"], "createdAt": _iso(self.now), "answeredAt": None})
        messages = self.convs.list_messages(conversation["id"])
        self.assertEqual(len(messages), 1)
        opening = messages[0]
        self.assertEqual((opening["role"], opening["provider"], opening["model"], opening["external"]), ("assistant", "template", "template", False))
        self.assertEqual(opening["content"], persona.proactive_opening("commitment_due", {"content": commit["content"], "date": opening["content"].split("期限是", 1)[1].split("。", 1)[0]}))
        meta = opening["meta"]
        self.assertEqual({k: meta[k] for k in ("kind", "reason", "whyNow", "routingOrigin", "nudgeId")},
                         {"kind": "zhijun_initiated", "reason": "commitment_due", "whyNow": nudge["whyNow"], "routingOrigin": {"service": "", "external": False}, "nudgeId": nudge["id"]})
        self.assertEqual([(r["kind"], r["id"]) for r in meta["routingSources"]], [("claim", commit["id"])])
        self.assertTrue(all(r.get("version") for r in meta["routingSources"]))
        self.assertEqual(meta["charterBasis"]["scope"], "global")
        # 来源提醒已 acted，顶部条不再显示；7 天内不重复找同一件事
        self.assertEqual(self.convs.get_nudge(nudge["id"])["status"], "acted")
        self.assertEqual(self.client.get("/api/mindos/nudges/today").json()["items"], [])
        self.assertIn("nudge:" + nudge["triggerKey"], proactive.recent_keys(self.onto, "global", now=self.now))
        # 节律：紧接着的第二次不会再开
        self.assertEqual(self.scan(self.now + timedelta(minutes=5)), {"created": 0, "reason": "min_gap"})
        # 列表与详情都带 initiated；今日页「知君想和你聊」
        listed = self.client.get("/api/mindos/conversations").json()["items"]
        self.assertEqual(next(item for item in listed if item["id"] == conversation["id"])["initiated"], conversation["initiated"])
        detail = self.client.get(f"/api/mindos/conversations/{conversation['id']}").json()
        self.assertEqual(detail["conversation"]["initiated"], conversation["initiated"])
        self.assertEqual(detail["messages"][0]["meta"]["kind"], "zhijun_initiated")
        home = zhijun_home.build_home_overview(now=self.now, enqueue=False, ontology=self.onto, conversations=self.convs, growth=self.growth)
        self.assertEqual(home["initiated"], [{"conversationId": conversation["id"], "title": conversation["title"], "whyNow": nudge["whyNow"], "kind": "commitment_due", "createdAt": _iso(self.now)}])
        self.assertEqual(home["map"]["relationshipDays"], 4)
        # 用户开口：answeredAt 落库；今日列表清空
        res = self.client.post(f"/api/mindos/conversations/{conversation['id']}/messages", json={"content": "招到两个了"})
        self.assertEqual(res.status_code, 200, res.text)
        answered = self.convs.get_conversation(conversation["id"])["initiated"]
        self.assertTrue(answered["answeredAt"] and answered["answeredAt"].endswith("Z"))
        self.assertEqual({k: v for k, v in answered.items() if k != "answeredAt"}, {k: v for k, v in conversation["initiated"].items() if k != "answeredAt"})
        self.assertEqual(zhijun_home.build_home_overview(now=self.now, enqueue=False, ontology=self.onto, conversations=self.convs, growth=self.growth)["initiated"], [])
        # 标题不会被用户第一句覆盖（发起时已命名）
        self.assertEqual(self.convs.get_conversation(conversation["id"])["title"], conversation["title"])

    def test_assistant_messages_do_not_answer_and_archived_are_not_listed_on_home(self) -> None:
        first = self.initiated_at(self.now - timedelta(days=2), kind="greeting")
        second = self.initiated_at(self.now - timedelta(days=1), kind="milestone")
        third = self.initiated_at(self.now - timedelta(hours=5), kind="nod")
        fourth = self.initiated_at(self.now - timedelta(hours=1), kind="stale")
        self.convs.append_message(first["id"], "assistant", "知君再说一句")
        self.assertIsNone(self.convs.get_conversation(first["id"])["initiated"]["answeredAt"])
        self.convs.append_message(first["id"], "user", "x", status="aborted")
        self.assertIsNone(self.convs.get_conversation(first["id"])["initiated"]["answeredAt"])
        self.convs.append_message(first["id"], "user", "在")
        self.assertIsNotNone(self.convs.get_conversation(first["id"])["initiated"]["answeredAt"])
        self.convs.update_metadata(second["id"], expected_revision=0, status="archived")
        home = zhijun_home.build_home_overview(now=self.now, enqueue=False, ontology=self.onto, conversations=self.convs, growth=self.growth)
        self.assertEqual([item["conversationId"] for item in home["initiated"]], [fourth["id"], third["id"]])
        self.assertEqual({item["kind"] for item in home["initiated"]}, {"stale", "nod"})
        # 其它设备的发起不混进来
        other = self.conversation_at(self.now, device_scope="device-b", initiated={"by": "zhijun", "kind": "gap", "whyNow": "w", "createdAt": _iso(self.now), "answeredAt": None})
        self.assertNotIn(other["id"], [c["id"] for c in self.convs.list_initiated(device_scope="global")])
        self.assertEqual([c["id"] for c in self.convs.list_initiated(device_scope="device-b")], [other["id"]])

    def test_review_due_without_recoverable_sources_still_opens_and_binds_decision(self) -> None:
        overdue = self.growth.create_decision(_decision("要不要涨价", self.now - timedelta(days=2)))
        nudge_service.scan(conv_store=self.convs, growth=self.growth, now=self.now)
        result = self.scan()
        self.assertEqual(result["reason"], "review_due")
        conversation = self.convs.get_conversation(result["conversationId"])
        self.assertEqual(conversation["decisionId"], overdue["id"])
        self.assertEqual(conversation["title"], "回访日：要不要涨价")
        opening = self.convs.list_messages(conversation["id"])[0]
        self.assertEqual(opening["content"], "「要不要涨价」到了你当时定的回访日。先不用急着说结果，这段时间感觉怎么样？")
        self.assertEqual(opening["meta"]["routingSources"], [])  # 不写不可追溯的引用
        self.assertEqual(self.client.get(f"/api/mindos/conversations/{conversation['id']}").json()["decision"]["id"], overdue["id"])
        self.assertEqual([n["status"] for n in self.convs.list_nudges()], ["acted"])

    def test_inquiry_candidate_with_unverifiable_source_is_skipped(self) -> None:
        origin = self.conversation_at(self.now - timedelta(days=3))
        self.said(origin["id"], "x", self.now - timedelta(days=3))
        self.convs.save_summary(origin["id"], up_to_seq=1, summary="s", key_points=["待办：约投资人"], meta={})  # 旧摘要缺来源
        found = self.candidates()
        self.assertEqual([item["kind"] for item in found], ["open_loop"])
        self.assertEqual(self.scan(), {"created": 0, "reason": "sources_unavailable"})
        self.assertEqual(self.convs.list_initiated(), [])
        # 修好来源后可用，且把求知冷却一起记上
        message = self.convs.list_messages(origin["id"])[0]
        self.convs.save_summary(origin["id"], up_to_seq=1, summary="s", key_points=["待办：约投资人"], meta={"routingSources": [{"kind": "message", "id": message["id"]}]})
        result = self.scan()
        self.assertEqual(result["reason"], "open_loop")
        opening = self.convs.list_messages(result["conversationId"])[0]
        self.assertEqual(opening["content"], "上次你说要「约投资人」，后来怎么样了？")
        self.assertEqual([(r["kind"], r["id"]) for r in opening["meta"]["routingSources"]], [("summary", f"{origin['id']}:2")])
        self.assertIn(f"loop:{origin['id']}:2:0", inquiry.recent_keys(self.onto, "global", now=self.now))
        self.assertIn(f"loop:{origin['id']}:2:0", proactive.recent_keys(self.onto, "global", now=self.now))

    def test_run_respects_rhythm_before_looking_for_candidates(self) -> None:
        self.growth.create_decision(_decision("要不要涨价", self.now - timedelta(days=2)))
        nudge_service.scan(conv_store=self.convs, growth=self.growth, now=self.now)
        self.convs.save_nudge_policy(proactive={"enabled": False})
        self.assertEqual(self.scan(), {"created": 0, "reason": "disabled"})
        self.convs.save_nudge_policy(proactive={"enabled": True})
        self.assertEqual(self.scan(self.now + timedelta(hours=12)), {"created": 0, "reason": "quiet_hours"})
        self.assertEqual(self.scan()["created"], 1)
        self.assertEqual(self.scan(self.now + timedelta(hours=5)), {"created": 0, "reason": "no_candidate"})


class SchedulingTests(ProactiveBase):
    def test_job_kind_handler_and_hourly_enqueue(self) -> None:
        self.assertIn("proactive_scan", ontology_store_module.JOB_KINDS)
        self.growth.create_decision(_decision("要不要涨价", self.now - timedelta(days=2)))
        scan_id = jobs.enqueue_nudge_scan(store=self.onto)
        job_id = jobs.enqueue_proactive_scan(store=self.onto)
        self.assertTrue(scan_id and job_id)
        self.assertIsNone(jobs.enqueue_proactive_scan(store=self.onto))  # 同 owner 幂等
        self.assertEqual(self.onto.get_job(job_id)["priority"], 0)
        self.assertEqual(jobs.drain(store=self.onto, conv_store=self.convs), 2)
        done = self.onto.get_job(job_id)
        self.assertEqual(done["state"], "done", done)
        self.assertEqual((done["result"]["created"], done["result"]["reason"]), (1, "review_due"))
        self.assertEqual(len(self.convs.list_initiated()), 1)
        # 手动触发（无 HTTP 路由）：直接调 run
        self.assertEqual(self.scan(self.now + timedelta(hours=5))["reason"], "no_candidate")

    def test_worker_loop_and_domain_tick_enqueue_alongside_nudge_scan(self) -> None:
        with patch("mindos.zhijun.jobs.enqueue_nudge_scan") as scan, patch("mindos.zhijun.jobs.enqueue_proactive_scan") as pro, \
                patch("mindos.zhijun.jobs.OntologyStore.instance", return_value=self.onto), \
                patch("mindos.zhijun.jobs.ConversationStore.instance", return_value=self.convs), \
                patch.dict(os.environ, {"ZHIJUN_WORKSPACE_ID": ""}):
            worker = jobs.OntologyWorker()
            worker._stop_event.set()
            worker._stop_event.clear()
            original_claim = self.onto.claim_next_job

            def stop_after_first(*args, **kwargs):
                worker._stop_event.set()
                return None
            with patch.object(self.onto, "claim_next_job", side_effect=stop_after_first):
                worker._run()
            self.assertEqual((scan.call_count, pro.call_count), (1, 1))
        from zhijun_worker import events
        event = {"eventId": "a" * 32, "type": "domain.tick", "payload": {"scheduledAt": int(time.time()) // 60},
                 "executionRequestId": "req-proactive-1", "operationId": "system_material_event"}
        result = events.handle(events.parse(json.dumps(event).encode()))
        self.assertTrue(result["accepted"])
        self.assertEqual(len(result["jobIds"]), 2)
        with self.onto._connect() as db:
            kinds = {row[0]: json.loads(row[1]) for row in db.execute("SELECT kind, payload_json FROM ontology_jobs WHERE job_id IN (%s)" % ",".join("?" for _ in result["jobIds"]), result["jobIds"])}
        self.assertEqual(set(kinds), {"nudge_scan", "proactive_scan"})
        self.assertEqual(kinds["proactive_scan"], {"scope": "global"})
        again = {**event, "eventId": "b" * 32}
        self.assertEqual(events.handle(events.parse(json.dumps(again).encode()))["jobIds"], [])  # 同一小时不重复


if __name__ == "__main__":
    unittest.main()
