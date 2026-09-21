"""P1 内观：新增 burdens（心里的事）与 self_view（我眼中的我）两个分区，以及 C7「内心不外流」。

C7 是全产品唯一一条漏了就无法补救的约束：一句心里话被另一个 AI 读走之后，
道歉没有意义。所以这里对每一个外发通道都单独断言一次，而不是只测共用函数。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mindos.stores.conversation_store import ConversationStore
from mindos.stores.ontology_store import (
    DEFAULT_PREDICATE, INWARD_SECTIONS, PREDICATES, SECTION_TITLES, SECTIONS,
    TAKEAWAY_SECTIONS, OntologyStore, _migrate_claims_sections,
)
from mindos.zhijun import context_pack, projection


class VocabularyTests(unittest.TestCase):
    def test_eight_sections_and_twenty_seven_predicates(self):
        self.assertEqual(len(SECTIONS), 8)
        self.assertIn("burdens", SECTIONS)
        self.assertIn("self_view", SECTIONS)
        self.assertEqual(sum(len(v) for v in PREDICATES.values()), 27)

    def test_every_section_has_predicates_a_default_and_a_title(self):
        """少一处默认谓词，不传 predicate 的调用就会 KeyError。"""
        for table in (PREDICATES, DEFAULT_PREDICATE, SECTION_TITLES):
            self.assertEqual(set(table), set(SECTIONS))
        for section, default in DEFAULT_PREDICATE.items():
            self.assertIn(default, PREDICATES[section], section)

    def test_avoids_facing_is_spelled_differently_from_direction_avoids(self):
        """混用会让张力检测失效：一个是「不想要的方向」，一个是「知道该做但在躲」。"""
        self.assertIn("avoids_facing", PREDICATES["burdens"])
        self.assertIn("avoids", PREDICATES["direction"])
        self.assertNotIn("avoids", PREDICATES["burdens"])

    def test_takeaway_set_is_sections_minus_inward(self):
        self.assertEqual(set(TAKEAWAY_SECTIONS), set(SECTIONS) - set(INWARD_SECTIONS))
        self.assertEqual(len(TAKEAWAY_SECTIONS), 6)


class NeverLeavesTests(unittest.TestCase):
    """C7：三个外发通道逐个验，不靠共用函数一处过。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.onto = OntologyStore(root / "ontology.db")
        self.convs = ConversationStore(root / "conversations.db")
        self.patch = patch.object(ConversationStore, "_instance", self.convs)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # 全部按「最该被带走」的样子造：已确认、可导出、非敏感、长期。
        # 它们唯一的共同点是分区——只有分区能拦住它们，才说明 C7 不靠别的条件。
        self.ordinary = self._claim("我是十二人公司的技术负责人", "who")
        self._claim("和林岚那次谈话一直没谈", "burdens", "avoids_facing")
        self._claim("我觉得自己不够狠", "self_view", "sees_self_as")

    def _claim(self, text, section, predicate=None):
        return self.onto.create_claim(
            {"content": text, "section": section, "layer": "self_declared",
             "predicate": predicate or DEFAULT_PREDICATE[section],
             "device_scope": "global", "export_allowed": True},
            [], trust_state="confirmed", trust_origin="user_created")

    def test_context_pack_never_returns_inward_claims(self):
        got = [c["content"] for c in context_pack.exportable_claims(self.onto)]
        self.assertEqual(got, ["我是十二人公司的技术负责人"])

    def test_asking_for_inward_sections_explicitly_still_returns_nothing(self):
        """用减法而不是「默认不含」：显式点名也拿不到。"""
        for wanted in (("burdens",), ("self_view",), ("burdens", "self_view"), ("who", "burdens")):
            with self.subTest(sections=wanted):
                got = [c["content"] for c in context_pack.exportable_claims(self.onto, sections=wanted)]
                self.assertNotIn("和林岚那次谈话一直没谈", got)
                self.assertNotIn("我觉得自己不够狠", got)

    def test_user_md_projection_contains_neither_content_nor_section_heading(self):
        full, export = projection.render(self.onto)
        for text in ("和林岚那次谈话一直没谈", "我觉得自己不够狠", "心里的事", "我眼中的我"):
            with self.subTest(text=text):
                self.assertNotIn(text, export, "USER.md 是别的 AI 读得到的")
                self.assertNotIn(text, full, "本机那半也不写：它落在 MEMORY_DIR 里")
        self.assertIn("我是十二人公司的技术负责人", export)

    def test_mcp_section_list_is_derived_and_excludes_inward(self):
        from zhijun_mcp.models import SECTIONS as mcp_sections
        self.assertEqual(set(mcp_sections), set(TAKEAWAY_SECTIONS))
        for section in INWARD_SECTIONS:
            self.assertNotIn(section, mcp_sections)


class NoPryingTests(unittest.TestCase):
    """内观分区不能被当成待填的空去问。"""

    def test_gap_inquiry_never_targets_inward_sections(self):
        """「你有什么事压在心里」当缺口去问就是窥探。心里的事只能从反复提及里看出来，
        自我评价只收用户自己说出口的原话（PRD 6.1），这也是第 11 节不给它们定覆盖指标的原因。"""
        from mindos.zhijun import inquiry
        for section in INWARD_SECTIONS:
            with self.subTest(section=section):
                self.assertNotIn(section, inquiry.GAP_QUESTIONS,
                                 "连问题文案都不该有——有了下一个人就会把它接回循环里")
                self.assertNotIn(section, inquiry.GAP_WHY)

    def test_core_profile_caps_inward_sections_tightly(self):
        from mindos.zhijun.core_profile import CAPS, DROP_ORDER
        for section in INWARD_SECTIONS:
            with self.subTest(section=section):
                self.assertEqual(CAPS[section], 2, "贵在准不贵在多")
                self.assertIn(section, DROP_ORDER, "超预算时要有明确的丢弃位置")
        self.assertLess(DROP_ORDER.index("burdens"), DROP_ORDER.index("self_view"),
                        "self_view 是张力检测的锚，比 burdens 更晚丢")


class ExtractionRestraintTests(unittest.TestCase):
    """PRD 6.1：这两个分区的抽取比别的都紧。错记一条会让用户下次不敢说。"""

    class _Meta:
        def __init__(self): self.v = {}
        def meta_get(self, k, d=None): return self.v.get(k, d)
        def meta_set(self, k, val): self.v[k] = val

    def test_a_burden_needs_two_mentions_before_it_becomes_a_candidate(self):
        from mindos.zhijun import burdens
        store = self._Meta()
        self.assertEqual(burdens.record_and_count(store, "和林岚那次谈话一直没谈",
                                                  object_name="林岚", message_id="m1"), 1)
        self.assertEqual(burdens.record_and_count(store, "一直推着没跟林岚谈",
                                                  object_name="林岚", message_id="m2"), 2,
                         "换个说法仍是同一件事")

    def test_retrying_the_same_message_does_not_count_twice(self):
        """否则任务重试一次就能把第一次提及变成「提过两次」，正好绕开这道闸。"""
        from mindos.zhijun import burdens
        store = self._Meta()
        burdens.record_and_count(store, "年底融资的节奏压得慌", message_id="m1")
        self.assertEqual(burdens.record_and_count(store, "年底融资的节奏压得慌", message_id="m1"), 1)

    def test_mention_count_is_read_only(self):
        from mindos.zhijun import burdens
        store = self._Meta()
        burdens.record_and_count(store, "年底融资的节奏压得慌", message_id="m1")
        for _ in range(3):
            self.assertEqual(burdens.mention_count(store, "年底融资的节奏压得慌"), 1)

    def test_self_view_only_accepts_an_explicit_self_evaluation_quote(self):
        """替人断定他怎么看自己，越界且几乎必错，所以只收原话里的自评句式。"""
        from mindos.zhijun.extract import _SELF_VIEW_RE
        for quote in ("我觉得自己不够狠", "我是个失败者", "我这人就是心软"):
            with self.subTest(quote=quote):
                self.assertTrue(_SELF_VIEW_RE.search(quote))
        for quote in ("我今天很忙", "我太累了", "这个方案不够好"):
            with self.subTest(quote=quote):
                self.assertFalse(_SELF_VIEW_RE.search(quote), quote)

    def test_burden_language_requires_persistence_not_a_bad_day(self):
        from mindos.zhijun.extract import _BURDEN_RE
        self.assertTrue(_BURDEN_RE.search("和林岚那次谈话我一直推着没谈"))
        self.assertTrue(_BURDEN_RE.search("这件事压在心里很久了"))
        self.assertFalse(_BURDEN_RE.search("今天有点累"))


class StalledBurdenTests(unittest.TestCase):
    """张力 B（说了没动）：反复回来却一直没动静。不调模型——这是数出来的，不是判出来的。"""

    def _run(self, claims, *, mentions=5, days=60, entity=None):
        from datetime import datetime, timedelta, timezone
        from mindos.zhijun import consolidate
        now = datetime.now(timezone.utc)
        nudges = []
        conv = type("C", (), {"create_nudge": lambda self, **kw: nudges.append(kw)})()
        report = {"tensions": 0}
        store = type("S", (), {"get_entity": lambda self, eid: entity})()
        with patch("mindos.zhijun.charter_policy.check_action", return_value={"allowed": True}), \
                patch("mindos.zhijun.burdens.mention_count", return_value=mentions):
            for c in claims:
                c.setdefault("firstSeen", (now - timedelta(days=days)).isoformat())
                c.setdefault("lastReaffirmed", now.isoformat())
                c.setdefault("trustState", "confirmed")
            consolidate._stalled_burdens(store, conv, claims, {}, now, report)
        return nudges, report

    def _burden(self, text="和林岚那次谈话一直没谈", entity="ent_linlan"):
        return {"id": "b1", "section": "burdens", "content": text, "objectEntityId": entity}

    def test_a_long_running_burden_with_no_progress_is_raised(self):
        nudges, report = self._run([self._burden()])
        self.assertEqual(len(nudges), 1)
        self.assertEqual(report["tensions"], 1)
        self.assertEqual(nudges[0]["kind"], "burden_stalled")
        self.assertIn("60 天", nudges[0]["message"], "要给出跨度这个事实")
        self.assertIn("5 次", nudges[0]["message"], "要给出次数这个事实")

    def test_related_progress_silences_it(self):
        """相关事项有了新进展就不该再提——他已经动了。"""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        # 指向同一个人：分词会把「林岚」和「林岚谈」切开，按词面几乎必然漏判。
        matter = {"id": "m1", "section": "matters", "content": "和林岚谈一次",
                  "objectEntityId": "ent_linlan",
                  "firstSeen": (now - timedelta(days=5)).isoformat()}
        nudges, _ = self._run([self._burden(), matter])
        self.assertEqual(nudges, [])

    def test_too_few_mentions_or_too_short_a_span_stay_quiet(self):
        self.assertEqual(self._run([self._burden()], mentions=2)[0], [], "提得不够多")
        self.assertEqual(self._run([self._burden()], days=10)[0], [], "跨度不够长")

    def test_at_most_one_per_run(self):
        """这种话说多了就成了催。"""
        many = [dict(self._burden(f"第 {i} 件压着的事", entity=f"ent_{i}"), id=f"b{i}") for i in range(4)]
        nudges, _ = self._run(many)
        self.assertEqual(len(nudges), 1)


class PushBackToPeopleTests(unittest.TestCase):
    """原则 7：知君的目标是减少孤独，不是替代人。"""

    def _nudge(self, entity):
        return StalledBurdenTests._run(StalledBurdenTests(), [
            {"id": "b1", "section": "burdens", "content": "和林岚那次谈话一直没谈",
             "objectEntityId": "ent_linlan"}], entity=entity)[0]

    def test_a_burden_about_a_person_suggests_talking_to_that_person_by_name(self):
        nudges = self._nudge({"type": "person", "canonicalName": "林岚"})
        self.assertEqual(len(nudges), 1)
        self.assertIn("林岚", nudges[0]["message"])
        self.assertIn("不是我", nudges[0]["message"], "要说清该找的人不是知君")
        self.assertEqual(nudges[0]["trigger_ref"].get("talkToEntityId"), "ent_linlan")

    def test_a_burden_about_a_project_says_nothing_of_the_kind(self):
        """「你该和『年底融资』谈谈」是胡话。"""
        nudges = self._nudge({"type": "project", "canonicalName": "年底融资"})
        self.assertEqual(len(nudges), 1)
        self.assertNotIn("不是我", nudges[0]["message"])
        self.assertNotIn("talkToEntityId", nudges[0]["trigger_ref"])

    def test_an_unknown_entity_stays_quiet_rather_than_guessing(self):
        nudges = self._nudge(None)
        self.assertEqual(len(nudges), 1)
        self.assertNotIn("不是我", nudges[0]["message"])


class ObservationReachesTheUserTests(unittest.TestCase):
    """照见要真的走到用户面前。

    `_from_nudge` 对未知 kind 返回 None，所以只在 consolidate 里产出提醒是不够的——
    不接进来就会被静默丢掉，整条照见等于没做。
    """

    def _store(self, claims):
        return type("S", (), {"get_claim": lambda self, cid, **kw: claims.get(cid)})()

    def _nudge(self, kind, ref, message="合成消息"):
        return {"id": "n1", "kind": kind, "triggerKey": f"{kind}:1", "triggerRef": ref,
                "whyNow": "合成理由", "message": message, "scheduledFor": "2026-09-21T00:00:00Z"}

    def test_self_view_tension_becomes_a_candidate_with_both_claims_as_sources(self):
        from mindos.zhijun import proactive
        store = self._store({
            "v1": {"id": "v1", "content": "我不够狠", "trustState": "confirmed"},
            "b1": {"id": "b1", "content": "三次决定各拖了一个月", "trustState": "confirmed"},
        })
        got = proactive._from_nudge(self._nudge("self_view_tension", {"selfViewId": "v1", "behaviourId": "b1"}), store, None)
        self.assertIsNotNone(got, "不接进来就会被静默丢掉")
        self.assertEqual({r["id"] for r in got["refs"]}, {"v1", "b1"})
        self.assertIn("我不够狠", got["opening"])
        self.assertIn("三次决定各拖了一个月", got["opening"])
        self.assertIn("没看明白", got["opening"], "第一句是观察不是评价")

    def test_a_retracted_claim_stops_the_observation_from_coming_back(self):
        """撤回的永不回流（原则 3）。任意一条被撤回，这条照见就不该再出现。"""
        from mindos.zhijun import proactive
        for gone in ("v1", "b1"):
            with self.subTest(retracted=gone):
                claims = {
                    "v1": {"id": "v1", "content": "我不够狠", "trustState": "confirmed"},
                    "b1": {"id": "b1", "content": "三次决定各拖了一个月", "trustState": "confirmed"},
                }
                claims[gone]["trustState"] = "retracted"
                got = proactive._from_nudge(
                    self._nudge("self_view_tension", {"selfViewId": "v1", "behaviourId": "b1"}),
                    self._store(claims), None)
                self.assertIsNone(got)

    def test_burden_stalled_keeps_the_facts_and_the_push_back_line(self):
        from mindos.zhijun import proactive
        message = "「和林岚那次谈话」这件事，60 天里你提过 5 次。\n\n还有一句：这件事你真正要谈的人可能是林岚，不是我。"
        store = self._store({"b9": {"id": "b9", "content": "和林岚那次谈话", "trustState": "confirmed"}})
        got = proactive._from_nudge(
            self._nudge("burden_stalled", {"burdenId": "b9", "talkToEntityId": "ent_linlan"}, message), store, None)
        self.assertIsNotNone(got)
        self.assertIn("60 天", got["opening"])
        self.assertIn("5 次", got["opening"])
        self.assertIn("不是我", got["opening"], "把人推回人那句要活着到用户面前")
        self.assertIn("\n\n", got["opening"], "那句是刻意单起一段的，不能被压成一行")

    def test_a_retracted_burden_stops_it_too(self):
        from mindos.zhijun import proactive
        store = self._store({"b9": {"id": "b9", "content": "和林岚那次谈话", "trustState": "retracted"}})
        self.assertIsNone(proactive._from_nudge(self._nudge("burden_stalled", {"burdenId": "b9"}), store, None))

    def test_observations_rank_below_things_with_an_agreed_time(self):
        """回访和承诺有确定的时间约定，照见没有，所以排在它们后面。"""
        from mindos.zhijun.proactive import _PRIORITY
        for kind in ("self_view_tension", "burden_stalled", "principle_tension"):
            with self.subTest(kind=kind):
                self.assertIn(kind, _PRIORITY)
                self.assertGreater(_PRIORITY[kind], _PRIORITY["review_due"])
                self.assertGreater(_PRIORITY[kind], _PRIORITY["commitment_due"])
                self.assertLess(_PRIORITY[kind], _PRIORITY["gap"])

    def test_every_nudge_kind_consolidate_emits_has_a_home(self):
        """防回归：以后再加一种张力，忘了接进 proactive 就会在这里挂掉。"""
        from mindos.zhijun import persona
        from mindos.zhijun.proactive import _PRIORITY
        emitted = ("principle_tension", "self_view_tension", "burden_stalled")
        for kind in emitted:
            with self.subTest(kind=kind):
                self.assertIn(kind, _PRIORITY)
                self.assertNotEqual(persona.proactive_opening(kind, {}), persona.PROACTIVE_GREETING,
                                    "落到了兜底问候，等于这条照见没有自己的说法")
                self.assertNotEqual(persona.proactive_title(kind, {}), "好几天没聊了")


class SchemaMigrationTests(unittest.TestCase):
    """老库的 CHECK 里没有新分区，写入会被 SQLite 直接拒绝。"""

    def _old_db(self):
        path = Path(tempfile.mkdtemp()) / "ontology.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
        CREATE TABLE claims (id TEXT PRIMARY KEY,
          section TEXT NOT NULL CHECK(section IN ('who','people','matters','principles','ways','direction')),
          content TEXT NOT NULL, content_hash TEXT);
        CREATE INDEX idx_claims_state ON claims(section, id);
        CREATE UNIQUE INDEX ux_claims_active_hash ON claims(content_hash);
        """)
        conn.execute("ALTER TABLE claims ADD COLUMN why_it_matters TEXT")   # 老库 ALTER 补出来的列
        conn.execute("INSERT INTO claims (id,section,content,content_hash,why_it_matters)"
                     " VALUES ('c1','who','我是技术负责人','h1','很重要')")
        conn.commit()
        return conn

    def test_migration_keeps_rows_altered_columns_and_indexes(self):
        conn = self._old_db()
        self.assertTrue(_migrate_claims_sections(conn))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT why_it_matters FROM claims").fetchone()[0], "很重要",
                         "新表 DDL 由旧表改写而来，ALTER 补的列不能丢")
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='claims' AND sql IS NOT NULL")}
        self.assertEqual(names, {"idx_claims_state", "ux_claims_active_hash"})

    def test_after_migration_inward_sections_are_writable_and_junk_is_still_rejected(self):
        conn = self._old_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO claims (id,section,content,content_hash) VALUES ('x','burdens','a','hx')")
        conn.rollback()
        _migrate_claims_sections(conn)
        conn.execute("INSERT INTO claims (id,section,content,content_hash) VALUES ('c2','burdens','压着的事','h2')")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO claims (id,section,content,content_hash) VALUES ('c3','nope','x','h3')")

    def test_migration_is_idempotent(self):
        conn = self._old_db()
        self.assertTrue(_migrate_claims_sections(conn))
        self.assertFalse(_migrate_claims_sections(conn), "第二次不该再动表")

    def test_unrecognised_constraint_is_left_alone(self):
        """约束长得不认识就不动：重建一张核心表的风险高于少支持两个分区。"""
        path = Path(tempfile.mkdtemp()) / "o.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE claims (id TEXT PRIMARY KEY, section TEXT NOT NULL CHECK(section <> ''))")
        conn.commit()
        self.assertFalse(_migrate_claims_sections(conn))


if __name__ == "__main__":
    unittest.main()
