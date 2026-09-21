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
