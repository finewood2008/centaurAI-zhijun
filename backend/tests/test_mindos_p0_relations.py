"""MindOS P0-1 实体/关系三元组抽取专项测试。

覆盖 Review 收敛结论的 1~5：
- 谓词规范化：精确匹配 + 否定拒绝（不属于/未采用/并非属于）；
- 证据校验：必须含 subject→谓词→object 且按序同窗，拒绝「只命中主语」的幻觉放行；
- LLM JSON 解析：端点必须选自实体产物、confidence 范围、幻觉输出→None；
- fallback：保留实体产物的真实 type（不硬编码 term）；
- 幂等 hash：正文 + 实体产物 + 生成器复合；
- 竞态自愈：实体未就绪→unavailable；实体落库后链式触发关系任务。

依赖项目 .venv，可独立于 server 运行：
    ..\\.venv\\Scripts\\python.exe -m unittest test_mindos_p0_relations -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos import derived, graph
from mindos.services import ingestion
from mindos.stores import derived_store


def _entity(type_, name):
    return {"type": type_, "name": name}


class RelationPredicateTests(unittest.TestCase):
    def test_canonical_and_synonym(self):
        self.assertEqual(derived._normalize_relation_predicate("替代"), "替代")
        self.assertEqual(derived._normalize_relation_predicate("取代"), "替代")
        self.assertEqual(derived._normalize_relation_predicate("属于"), "属于")
        self.assertEqual(derived._normalize_relation_predicate("隶属"), "属于")
        self.assertEqual(derived._normalize_relation_predicate("采用"), "采用")
        self.assertEqual(derived._normalize_relation_predicate("使用"), "采用")

    def test_tolerance_punctuation(self):
        # 去除空格/标点后再精确匹配
        self.assertEqual(derived._normalize_relation_predicate(" 采用。"), "采用")

    def test_negative_predicates_rejected(self):
        for bad in ("不属于", "未采用", "并非属于", "不是采用", "没有采用", "不替代"):
            self.assertIsNone(derived._normalize_relation_predicate(bad))

    def test_unknown_and_empty_rejected(self):
        self.assertIsNone(derived._normalize_relation_predicate(""))
        self.assertIsNone(derived._normalize_relation_predicate("  "))
        self.assertIsNone(derived._normalize_relation_predicate("根本不成立"))


class RelationEvidenceTests(unittest.TestCase):
    def test_requires_ordered_subject_predicate_object(self):
        text = "我们决定用新流程替代旧流程，从而提升效率。"
        ev = derived._relation_evidence(text, "新流程", "替代", "旧流程")
        self.assertTrue("新流程" in ev and "替代" in ev and "旧流程" in ev)

    def test_synonym_predicate_literal_matched(self):
        # 原文用「取代」，规范谓词是「替代」→ 仍能截取证据
        text = "公司用方案B取代方案A，成本更低。"
        ev = derived._relation_evidence(text, "方案B", "替代", "方案A")
        self.assertTrue(ev and "取代" in ev)

    def test_no_full_triple_returns_empty(self):
        # 只出现主语、没有「采用 宾语」完整关系 → 必须返回 ""（不放行幻觉）
        text = "方案A 被多次讨论，但没有采用方案B 的描述。"
        self.assertEqual(derived._relation_evidence(text, "方案A", "采用", "方案B"), "")

    def test_negated_predicate_returns_empty(self):
        text = "该系统不属于内部网络。"
        self.assertEqual(derived._relation_evidence(text, "该系统", "属于", "内部网络"), "")


class RelationParseJsonTests(unittest.TestCase):
    def test_endpoints_must_come_from_entity_index(self):
        ei = {"A": _entity("term", "A"), "B": _entity("term", "B")}
        # 端点 B 不在实体产物 → 该三元组被丢弃 → 视为幻觉返回 None
        r = derived._parse_relation_json(
            '[{"subject":{"name":"A"},"predicate":"替代","object":{"name":"C"},"confidence":0.8}]',
            ei, "A替代C",
        )
        self.assertIsNone(r)

    def test_type_overridden_from_entity(self):
        ei = {"人甲": _entity("person", "人甲"), "公司乙": _entity("organization", "公司乙")}
        r = derived._parse_relation_json(
            '[{"subject":{"name":"人甲"},"predicate":"任职于","object":{"name":"公司乙"},"confidence":0.9}]',
            ei, "人甲任职于公司乙",
        )
        self.assertIsNotNone(r)
        self.assertEqual(r[0]["subject"]["type"], "person")
        self.assertEqual(r[0]["object"]["type"], "organization")

    def test_confidence_range_enforced(self):
        ei = {"A": _entity("term", "A"), "B": _entity("term", "B")}
        for conf in (-0.1, 1.5, "abc"):
            r = derived._parse_relation_json(
                f'[{{"subject":{{"name":"A"}},"predicate":"替代","object":{{"name":"B"}},"confidence":{conf}}}]'
                if isinstance(conf, (int, float)) else
                '[{"subject":{"name":"A"},"predicate":"替代","object":{"name":"B"},"confidence":"abc"}]',
                ei, "A替代B",
            )
            self.assertIsNone(r)

    def test_all_hallucination_returns_none(self):
        ei = {"A": _entity("term", "A"), "B": _entity("term", "B")}
        # 谓词不在白名单 → 全部被过滤 → 幻觉
        r = derived._parse_relation_json(
            '[{"subject":{"name":"A"},"predicate":"正在跑","object":{"name":"B"},"confidence":0.8}]',
            ei, "A正在跑B",
        )
        self.assertIsNone(r)

    def test_invalid_model_output_is_recorded_as_retryable_failure(self):
        """非法 JSON 不能被空 fallback 固化为 ok，模型恢复后必须能重试。"""
        tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(tmp / "derived.db")
        self.addCleanup(lambda: derived_store.reset_for_tests())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        store = derived_store.DerivedStore.instance()
        store.set_derived_record(
            "material", "m", derived.KIND_ENTITY_EXTRACTION, "ok",
            {"items": [_entity("term", "A"), _entity("term", "B")]}, "h", "g",
        )
        with patch.object(derived, "_input_text", return_value="A替代B"), \
                patch.object(derived, "_call_relation_model", return_value="not-json"):
            derived._generate_relations("m", "/tmp/a.pdf")
        rec = store.get_derived_record("material", "m", derived.KIND_RELATION_EXTRACTION)
        self.assertEqual(rec["status"], "failed")

    def test_empty_array_is_legal(self):
        r = derived._parse_relation_json("[]", {}, "text")
        self.assertEqual(r, [])

    def test_self_reference_dropped(self):
        ei = {"A": _entity("term", "A")}
        r = derived._parse_relation_json(
            '[{"subject":{"name":"A"},"predicate":"替代","object":{"name":"A"},"confidence":0.8}]',
            ei, "A替代A",
        )
        self.assertIsNone(r)


class RelationFallbackTests(unittest.TestCase):
    def test_preserves_entity_type(self):
        ei = {
            "张三": _entity("person", "张三"),
            "未知公司": _entity("organization", "未知公司"),
        }
        text = "张三任职于未知公司，负责研发。"
        res = derived._relation_fallback(text, ei)
        self.assertTrue(res.executed)
        self.assertTrue(res.items)
        rel = res.items[0]
        self.assertEqual(rel["subject"]["type"], "person")
        self.assertEqual(rel["object"]["type"], "organization")

    def test_returns_empty_executed_without_full_triple(self):
        ei = {"A": _entity("term", "A"), "B": _entity("term", "B")}
        res = derived._relation_fallback("这里只有A，也提到B但无谓词", ei)
        self.assertTrue(res.executed)  # fallback 正常执行（只是无匹配）→ 不是失败
        self.assertEqual(res.items, [])


class RelationHashTests(unittest.TestCase):
    def test_hash_changes_when_entity_items_change(self):
        ent1 = {"content": {"items": [{"type": "term", "name": "A"}]},
                "generator": "g", "source": "llm"}
        ent2 = {"content": {"items": [{"type": "term", "name": "A"}, {"type": "person", "name": "张三"}]},
                "generator": "g", "source": "llm"}
        h1 = derived._relation_input_hash("正文", ent1)
        h2 = derived._relation_input_hash("正文", ent2)
        self.assertNotEqual(h1, h2)

    def test_hash_changes_when_generator_changes(self):
        ent1 = {"content": {"items": [{"type": "term", "name": "A"}]},
                "generator": "g1", "source": "llm"}
        ent2 = {"content": {"items": [{"type": "term", "name": "A"}]},
                "generator": "g2", "source": "llm"}
        self.assertNotEqual(
            derived._relation_input_hash("正文", ent1),
            derived._relation_input_hash("正文", ent2),
        )


class GraphSemanticEdgeTests(unittest.TestCase):
    """图谱语义边（graph.py）：桥接、方向、优先级、统计。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        derived_store.reset_for_tests(Path(self._tmp) / "derived.db")
        self.store = derived_store.DerivedStore.instance()
        self.pairs: set = set()

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed_entity(self, mid, names):
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, mid, derived.KIND_ENTITY_EXTRACTION, "ok",
            {"items": [_entity("term", n) for n in names]}, "h", "g",
        )

    def _seed_relation(self, mid, items):
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, mid, derived.KIND_RELATION_EXTRACTION, "ok",
            {"items": items, "source": "fallback"}, "h", "g",
        )

    def _nodes(self, labels):
        return {mid: {"id": mid, "type": "material", "label": label} for mid, label in labels.items()}

    def _rel(self, sub, pred, obj, confidence=0.9):
        return {"subject": {"type": "term", "name": sub},
                "predicate": pred,
                "object": {"type": "term", "name": obj},
                "confidence": confidence}

    def test_cross_material_double_hit_directed(self):
        nodes = self._nodes({"M1": "a.pdf", "M2": "b.pdf", "M3": "c.pdf"})
        self._seed_entity("M1", ["旧流程Y", "新流程X"])
        self._seed_entity("M2", ["旧流程Y"])
        self._seed_entity("M3", ["新流程X"])
        self._seed_relation("M1", [self._rel("旧流程Y", "替代", "新流程X")])

        edges = graph._semantic_edges(nodes, graph._load_material_relations(), self.pairs)
        sem = [e for e in edges if e["relation"] == "semantic"]
        self.assertEqual(len(sem), 1)
        self.assertEqual(sem[0]["source"], "M2")
        self.assertEqual(sem[0]["target"], "M3")
        self.assertIs(sem[0]["directed"], True)  # 两端命中 → 方向 S→O
        self.assertEqual(sem[0]["reason"], "旧流程Y 替代 新流程X")

    def test_single_hit_undirected(self):
        nodes = self._nodes({"M1": "a.pdf", "M2": "b.pdf"})
        self._seed_entity("M1", ["旧流程Y", "特殊实体"])
        self._seed_entity("M2", ["旧流程Y"])
        self._seed_relation("M1", [self._rel("旧流程Y", "替代", "特殊实体")])

        edges = graph._semantic_edges(nodes, graph._load_material_relations(), self.pairs)
        sem = [e for e in edges if e["relation"] == "semantic"]
        self.assertEqual(len(sem), 1)
        # 单端命中（object 只在 M1 自身）→ 材料↔资源，无方向
        self.assertEqual(sem[0]["source"], "M1")
        self.assertEqual(sem[0]["target"], "M2")
        self.assertIs(sem[0]["directed"], False)

    def test_no_edge_without_external_hit(self):
        # 单材料，实体均未命中其他资源 → 不产自环边
        nodes = self._nodes({"M1": "a.pdf"})
        self._seed_entity("M1", ["旧流程Y", "新流程X"])
        self._seed_relation("M1", [self._rel("旧流程Y", "替代", "新流程X")])
        edges = graph._semantic_edges(nodes, graph._load_material_relations(), self.pairs)
        self.assertEqual([e for e in edges if e["relation"] == "semantic"], [])

    def test_edge_skipped_when_both_ends_same_resource(self):
        # S/O 两端命中同一资源 → 不建自环
        nodes = self._nodes({"M1": "a.pdf", "M2": "b.pdf"})
        self._seed_entity("M1", ["旧流程Y", "新流程X"])
        self._seed_entity("M2", ["旧流程Y", "新流程X"])  # M2 同时命中两端
        self._seed_relation("M1", [self._rel("旧流程Y", "替代", "新流程X")])
        edges = graph._semantic_edges(nodes, graph._load_material_relations(), self.pairs)
        self.assertEqual([e for e in edges if e["relation"] == "semantic"], [])

    def test_double_hit_uses_next_distinct_resource_when_first_pair_self_loops(self):
        """两侧排序首项相同不应掩盖后续可用的跨资源桥接。"""
        nodes = self._nodes({"M1": "a.pdf", "M2": "b.pdf", "M3": "c.pdf"})
        self._seed_entity("M1", ["A", "B"])
        self._seed_entity("M2", ["A", "B"])
        self._seed_entity("M3", ["B"])
        self._seed_relation("M1", [self._rel("A", "替代", "B")])

        edges = graph._semantic_edges(nodes, graph._load_material_relations(), self.pairs)
        sem = [e for e in edges if e["relation"] == "semantic"]
        self.assertEqual(len(sem), 1)
        self.assertEqual((sem[0]["source"], sem[0]["target"]), ("M2", "M3"))
        self.assertIs(sem[0]["directed"], True)

    def test_priority_over_shared_tag(self):
        # 同一节点对同时满足 shared-tag：图上保留 semantic（插在 shared-tag 前）
        # 这里用同一 pairs 先跑 semantic 再跑 shared-tag，验证 semantic 存活
        nodes = {"M2": {"id": "M2", "type": "material", "label": "b.pdf", "tags": ["T"]},
                 "M3": {"id": "M3", "type": "material", "label": "c.pdf", "tags": ["T"]}}
        self._seed_entity("M1", ["旧流程Y", "新流程X"])
        self._seed_entity("M2", ["旧流程Y"])
        self._seed_entity("M3", ["新流程X"])
        self._seed_relation("M1", [self._rel("旧流程Y", "替代", "新流程X")])
        nodes["M1"] = {"id": "M1", "type": "material", "label": "a.pdf", "tags": []}

        edges = graph._semantic_edges(nodes, graph._load_material_relations(), self.pairs)
        edges += graph._shared_tag_edges(nodes, self.pairs)
        sem = [e for e in edges if e["relation"] == "semantic"]
        self.assertEqual(len(sem), 1)
        # 同对节点未被 shared-tag 占用（semantic 优先保留）
        self.assertEqual([e for e in edges if e["relation"] == "shared-tag"], [])

    def test_source_priority_yields_to_semantic(self):
        # 同对已有 source 边 → semantic 让位（不覆盖）
        nodes = self._nodes({"M1": "a.pdf", "M2": "b.pdf", "M3": "c.pdf"})
        self._seed_entity("M1", ["旧流程Y", "新流程X"])
        self._seed_entity("M2", ["旧流程Y"])
        self._seed_entity("M3", ["新流程X"])
        self._seed_relation("M1", [self._rel("旧流程Y", "替代", "新流程X")])

        edges = []
        graph._add_edge(edges, self.pairs, "M2", "M3", "source", "来源")  # source 先插入
        edges += graph._semantic_edges(nodes, graph._load_material_relations(), self.pairs)
        self.assertEqual([e for e in edges if e["relation"] == "semantic"], [])
        self.assertEqual(len([e for e in edges if e["relation"] == "source"]), 1)

    def test_stats_semantic_edges(self):
        nodes = self._nodes({"M1": "a.pdf", "M2": "b.pdf"})
        edges = [{"source": "M1", "target": "M2", "relation": "semantic", "reason": "X 替代 Y", "directed": False}]
        stats = graph._stats(nodes, edges)
        self.assertEqual(stats["semanticEdges"], 1)
        self.assertIn("semanticEdges", stats)

    def test_deterministic_winner_higher_confidence(self):
        # 多材料同时桥接到同一节点对：即使低 confidence 的材料先迭代，
        # 全局 confidence DESC 排序后高 confidence 的候选确定性胜出。
        nodes = self._nodes({"M1": "a.pdf", "M2": "b.pdf", "P": "pp.pdf", "Q": "qq.pdf"})
        self._seed_entity("P", ["唯一A"])
        self._seed_entity("Q", ["唯一B"])
        self._seed_relation("M1", [{"subject": {"type": "term", "name": "唯一A"}, "predicate": "替代",
                                     "object": {"type": "term", "name": "唯一B"},
                                     "confidence": 0.4, "relationId": "r1"}])
        self._seed_relation("M2", [{"subject": {"type": "term", "name": "唯一A"}, "predicate": "属于",
                                     "object": {"type": "term", "name": "唯一B"},
                                     "confidence": 0.9, "relationId": "r2"}])
        rels = graph._load_material_relations()
        ordered = {"M1": rels["M1"], "M2": rels["M2"]}  # M1 先迭代（低 confidence）
        edges = graph._semantic_edges(nodes, ordered, self.pairs)
        sem = [e for e in edges if e["relation"] == "semantic"]
        self.assertEqual(len(sem), 1)
        self.assertEqual(sem[0]["reason"], "唯一A 属于 唯一B")  # 高 confidence(M2) 胜出
        self.assertEqual({sem[0]["source"], sem[0]["target"]}, {"P", "Q"})

    def test_reverse_pair_deterministic_winner(self):
        # A→B 与 B→A（正向/反向）同时命中同一节点对：pair 去重只保留一条，
        # 高 confidence 的反向候选确定性胜出，且方向仍为 S→O。
        nodes = self._nodes({"M1": "a.pdf", "M2": "b.pdf", "P": "pp.pdf", "Q": "qq.pdf"})
        self._seed_entity("P", ["唯一A"])
        self._seed_entity("Q", ["唯一B"])
        # M1 产出正向：唯一A 替代 唯一B → P→Q，confidence 0.5
        self._seed_relation("M1", [{"subject": {"type": "term", "name": "唯一A"}, "predicate": "替代",
                                     "object": {"type": "term", "name": "唯一B"},
                                     "confidence": 0.5, "relationId": "r_fwd"}])
        # M2 产出反向：唯一B 替代 唯一A → Q→P，confidence 0.9
        self._seed_relation("M2", [{"subject": {"type": "term", "name": "唯一B"}, "predicate": "替代",
                                     "object": {"type": "term", "name": "唯一A"},
                                     "confidence": 0.9, "relationId": "r_rev"}])
        edges = graph._semantic_edges(nodes, graph._load_material_relations(), self.pairs)
        sem = [e for e in edges if e["relation"] == "semantic"]
        self.assertEqual(len(sem), 1)  # 正反向共用 pair (P,Q) → 只保留一条
        self.assertEqual(sem[0]["source"], "Q")  # 反向（高 confidence）胜出，方向 S→O
        self.assertEqual(sem[0]["target"], "P")
        self.assertIs(sem[0]["directed"], True)
        self.assertEqual(sem[0]["reason"], "唯一B 替代 唯一A")

    def test_relation_lifecycle_cleanup(self):
        # 归档/回收材料的 relation 产物残留无害：材料不在 nodes（已被过滤）时不产任何边
        nodes = self._nodes({"M2": "b.pdf", "M3": "c.pdf"})  # M1 已回收 → 不在 nodes
        self._seed_entity("M2", ["旧流程Y"])
        self._seed_entity("M3", ["新流程X"])
        self._seed_relation("M1", [self._rel("旧流程Y", "替代", "新流程X")])  # M1 关系产物残留
        edges = graph._semantic_edges(nodes, graph._load_material_relations(), self.pairs)
        self.assertEqual([e for e in edges if e["relation"] == "semantic"], [])

    def test_recycled_material_entities_not_indexed(self):
        # 归档/回收材料的实体产物不进桥接索引：M2 回收后其实体不参与命中，
        # M1 的双端关系退化为单端命中（无方向），不误连已回收资源。
        nodes = self._nodes({"M1": "a.pdf", "M3": "c.pdf"})  # M2 已回收
        self._seed_entity("M1", ["旧流程Y", "新流程X"])
        self._seed_entity("M2", ["旧流程Y"])  # M2 实体产物残留（不进索引）
        self._seed_entity("M3", ["新流程X"])
        self._seed_relation("M1", [self._rel("旧流程Y", "替代", "新流程X")])
        edges = graph._semantic_edges(nodes, graph._load_material_relations(), self.pairs)
        sem = [e for e in edges if e["relation"] == "semantic"]
        # 旧流程Y 排除 M1 自身后无命中（M2 已回收）→ 仅新流程X 命中 M3 → 单端 M1↔M3
        self.assertEqual(len(sem), 1)
        self.assertEqual(sem[0]["target"], "M3")
        self.assertIs(sem[0]["directed"], False)

    def test_defensive_against_bad_derived_records(self):
        # 脏派生记录（非 dict 项、非法 confidence、缺字段）不得拖垮图谱组装
        nodes = self._nodes({"M1": "a.pdf", "P": "pp.pdf", "Q": "qq.pdf"})
        self._seed_entity("P", ["唯一A"])
        self._seed_entity("Q", ["唯一B"])
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, "M1", derived.KIND_RELATION_EXTRACTION, "ok",
            {"items": [
                "junk",
                {"subject": {}, "confidence": "unknown"},          # 缺谓词/客体 → 丢弃
                {"subject": {"name": "唯一A"}, "predicate": "替代",
                 "object": {"name": "唯一B"}, "confidence": 0.8},   # 合法 → 保留
            ], "source": "llm"}, "h", "g",
        )
        rels = graph._load_material_relations()
        edges = graph._semantic_edges(nodes, rels, self.pairs)
        sem = [e for e in edges if e["relation"] == "semantic"]
        self.assertEqual(len(sem), 1)
        self.assertEqual(sem[0]["reason"], "唯一A 替代 唯一B")

    def test_defensive_against_bad_entity_records(self):
        # 脏实体派生记录（content 非 dict、items 非 list 或含非字典项）不得打崩图谱组装
        nodes = self._nodes({"M1": "a.pdf", "P": "pp.pdf", "Q": "qq.pdf"})
        # P 的实体记录被污染：既有非字典项，也有合法项
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, "P", derived.KIND_ENTITY_EXTRACTION, "ok",
            {"items": ["x", 42, _entity("term", "唯一A")]}, "h", "g",
        )
        # Q 的实体记录 content 整体不是 dict
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, "Q", derived.KIND_ENTITY_EXTRACTION, "ok",
            ["not-a-dict"], "h", "g",
        )
        self._seed_relation("M1", [self._rel("唯一A", "替代", "唯一B")])

        rels = graph._load_material_relations()
        edges = graph._semantic_edges(nodes, rels, self.pairs)
        # 唯一A 只命中 P（Q 实体损坏被跳过）；单端命中 → M1↔P 一条无方向语义边
        sem = [e for e in edges if e["relation"] == "semantic"]
        self.assertEqual(len(sem), 1)
        self.assertIs(sem[0]["directed"], False)

    def test_semantic_conflict_is_audited_when_pair_is_deduplicated(self):
        nodes = self._nodes({"M1": "a.pdf", "P": "p.pdf", "Q": "q.pdf"})
        self._seed_entity("P", ["唯一A"])
        self._seed_entity("Q", ["唯一B"])
        self._seed_relation("M1", [self._rel("唯一A", "替代", "唯一B")])
        with self.assertLogs("mindos.graph", level="INFO") as logs:
            graph._add_edge([], self.pairs, "P", "Q", "source", "来源")
            graph._semantic_edges(nodes, graph._load_material_relations(), self.pairs)
        self.assertTrue(any("语义边因节点对冲突被丢弃" in line for line in logs.output))

    def test_limit_applies_after_bridge_filter(self):
        # 前几条高 confidence 但不可桥接的关系不应占用上限，第 6 条可桥接关系应被保留
        nodes = self._nodes({"M1": "a.pdf", "P": "pp.pdf", "Q": "qq.pdf"})
        self._seed_entity("P", ["唯一A"])
        self._seed_entity("Q", ["唯一B"])
        relations = []
        for i in range(5):  # 5 条不可桥接（幽灵实体），confidence 递减
            relations.append(self._rel(f"幽灵{i}", "替代", f"幽灵o{i}", confidence=0.95 - i * 0.1))
        relations.append(self._rel("唯一A", "替代", "唯一B", confidence=0.5))  # 第 6 条可桥接
        self._seed_relation("M1", relations)
        rels = graph._load_material_relations()

        edges = graph._semantic_edges(nodes, rels, self.pairs)
        sem = [e for e in edges if e["relation"] == "semantic"]
        self.assertEqual(len(sem), 1)  # 可桥接关系未被高 confidence 的不可桥接关系挤掉
        self.assertEqual(sem[0]["reason"], "唯一A 替代 唯一B")

    def test_add_edge_default_directed(self):
        edges, pairs = [], set()
        graph._add_edge(edges, pairs, "A", "B", "source", "来源")
        graph._add_edge(edges, pairs, "A", "B", "similar", "内容相似")  # 已被 source 占用 → 忽略
        self.assertEqual(len(edges), 1)
        self.assertIs(edges[0]["directed"], True)


class RelationGenerationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        derived.reset_relation_task_flags()
        self.store = derived_store.DerivedStore.instance()

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _entity_rec(self, items):
        return {"content": {"items": items}, "status": "ok", "generator": "g", "source": "llm"}

    def _seed_entity(self, items):
        rec = self._entity_rec(items)
        self.store.set_derived_record(
            "material", "m", derived.KIND_ENTITY_EXTRACTION, "ok",
            rec["content"], "h", "g",
        )
        return rec

    def test_marks_unavailable_when_entity_not_ready(self):
        # 实体缺失 → 关系为可重试的 unavailable（不写成完成态）
        with patch.object(derived, "_input_text", return_value="正文"):
            derived._generate_relations("m", "/tmp/a.pdf")
        rel = self.store.get_derived_record("material", "m", derived.KIND_RELATION_EXTRACTION)
        self.assertEqual(rel["status"], "unavailable")

    def test_empty_text_with_ok_entities_preserves_record(self):
        """环境故障防御：向量库读不到 chunk（_input_text 空）但实体产物 ok
        （说明历史上有文本）时，不得写 skipped 覆盖已有 ok 关系记录。"""
        self.store.set_derived_record(
            "material", "m", derived.KIND_ENTITY_EXTRACTION, "ok",
            {"items": [_entity("term", "A")], "source": "llm"}, "h", "g",
        )
        self.store.set_derived_record(
            "material", "m", derived.KIND_RELATION_EXTRACTION, "ok",
            {"items": [], "source": "llm"}, "h_ok", "g",
        )
        with patch.object(derived, "_input_text", return_value=""):
            derived._generate_relations("m", "/tmp/a.pdf")
        rel = self.store.get_derived_record("material", "m", derived.KIND_RELATION_EXTRACTION)
        self.assertEqual(rel["status"], "ok")            # 原记录未被覆盖
        self.assertEqual(rel["input_hash"], "h_ok")      # 原 hash 保持

    def test_empty_text_without_entities_writes_skipped(self):
        # 真空文本（无实体产物佐证历史可读）→ 正常写 skipped（原行为不变）
        with patch.object(derived, "_input_text", return_value=""):
            derived._generate_relations("m", "/tmp/a.pdf")
        rel = self.store.get_derived_record("material", "m", derived.KIND_RELATION_EXTRACTION)
        self.assertEqual(rel["status"], "skipped")

    def test_empty_text_entities_with_ok_summary_preserves_record(self):
        # 实体重投时同理：摘要 ok 佐证历史可读 → 不把实体覆盖成 skipped
        self.store.set_derived_record(
            "material", "m", derived.KIND_SUMMARY, "ok",
            {"text": "摘要"}, "h", "g",
        )
        self.store.set_derived_record(
            "material", "m", derived.KIND_ENTITY_EXTRACTION, "unavailable",
            {"items": []}, "h", "g",
        )
        with patch.object(derived, "_input_text", return_value=""):
            derived._generate_entities("m", "/tmp/a.pdf")
        ent = self.store.get_derived_record("material", "m", derived.KIND_ENTITY_EXTRACTION)
        self.assertEqual(ent["status"], "unavailable")   # 保持可重试状态，未被覆盖成 skipped

    def test_generates_relations_once_entity_ready(self):
        rec = self._seed_entity([_entity("term", "A"), _entity("term", "B")])
        with patch.object(derived, "_input_text", return_value="A替代B") as inp, \
                patch.object(derived, "_call_relation_model", side_effect=urllib.error.URLError("down")):
            derived._generate_relations("m", "/tmp/a.pdf")
        rel = self.store.get_derived_record("material", "m", derived.KIND_RELATION_EXTRACTION)
        self.assertEqual(rel["status"], "ok")
        self.assertIn("A", rel["content"]["items"][0]["subject"]["name"])

    def test_entity_ready_triggers_relation_chain(self):
        # 链式投递形态：_submit_relations 把关系任务经统一调度器（ollama_material_scheduler）
        # 提交到单并发队列，替换旧 _run_relation_task 进后台池。
        with patch.object(derived, "_ollama_scheduler") as scheduler:
            derived._submit_relations("m", "/tmp/a.pdf")
        scheduler.submit.assert_called_once()
        # submit(priority, task_fn, material_id=..., kind=...) —— 第二个位置参数是任务体
        self.assertEqual(scheduler.submit.call_args.args[1].__code__.co_name, "<lambda>")

    def test_race_recovers_from_unavailable_to_ok(self):
        """真实竞态端到端：

        1) 初始没有实体；
        2) 关系任务先执行 → 写 unavailable；
        3) 实体任务落库实体；
        4) 实体写入点链式调用 _submit_relations（此处用真实去重后的投递，捕获 _submit_relations）；
        5) 第二次关系任务执行 → 最终 ok。
        从而证明「上传后不打开分析页面，关系也能自动恢复」，而不会永久停留在 unavailable。
        """
        with patch.object(derived, "_input_text", return_value="A替代B"), \
                patch.object(derived, "_call_relation_model",
                             side_effect=urllib.error.URLError("down")):
            # 1) 初始无实体 2) 关系先跑 → unavailable
            derived._generate_relations("m", "/tmp/a.pdf")
        rel = self.store.get_derived_record("material", "m", derived.KIND_RELATION_EXTRACTION)
        self.assertEqual(rel["status"], "unavailable")

        # 3) 实体任务落库实体（模拟 _generate_summary_and_entities 内部产物写入）
        self.store.set_derived_record(
            "material", "m", derived.KIND_ENTITY_EXTRACTION, "ok",
            {"items": [_entity("term", "A"), _entity("term", "B")], "source": "llm"},
            "h", "g",
        )

        # 4) 实体写入点链式投递关系任务（单并发调度器 → 第二次执行）
        with patch.object(derived, "_input_text", return_value="A替代B"), \
                patch.object(derived, "_call_relation_model",
                             side_effect=urllib.error.URLError("down")), \
                patch.object(derived, "_ollama_scheduler") as scheduler:
            derived._submit_relations("m", "/tmp/a.pdf")
            scheduler.submit.assert_called_once()
            self.assertEqual(scheduler.submit.call_args.args[1].__code__.co_name, "<lambda>")
            # 5) 第二次关系任务（链式任务体，参数已闭包）执行 → 实体已就绪 → ok
            scheduler.submit.call_args.args[1]()

        rel = self.store.get_derived_record("material", "m", derived.KIND_RELATION_EXTRACTION)
        self.assertEqual(rel["status"], "ok")

    def test_submit_relations_uses_priority_and_kind(self):
        # 阶段 B §6.2：_submit_relations 唯一提交契约是「提交一次、带正确优先级/材料/种类」。
        # 并发、去重、重放由统一调度器接管，_ollama_scheduler.submit 始终返回 bool，
        # 不会因调度器已停止而抛异常或污染后续任务（旧 in-flight 锁语义已删除）。
        with patch.object(derived, "_ollama_scheduler") as scheduler:
            ret = derived._submit_relations("m", "/tmp/a.pdf", force=True)
            self.assertTrue(ret)
            scheduler.submit.assert_called_once()
            args, kwargs = scheduler.submit.call_args
            self.assertIs(args[0], derived.PRIORITY_MANUAL_REGENERATE)
            self.assertEqual(kwargs["material_id"], "m")
            self.assertEqual(kwargs["kind"], derived.KIND_RELATION_EXTRACTION.lower())
        with patch.object(derived, "_ollama_scheduler") as scheduler:
            derived._submit_relations("m", "/tmp/a.pdf", force=False)
            self.assertIs(
                scheduler.submit.call_args.args[0], derived.PRIORITY_RELATIONS
            )

    def test_skips_when_ok_and_hash_unchanged(self):
        rec = self._seed_entity([_entity("term", "A"), _entity("term", "B")])
        input_hash = derived._relation_input_hash("A替代B", rec)
        gen = derived._generator_name(derived.get_provider().get_local_snapshot())
        self.store.set_derived_record(
            "material", "m", derived.KIND_RELATION_EXTRACTION, "ok",
            {"items": []}, input_hash, gen,
        )
        with patch.object(derived, "_input_text", return_value="A替代B") as inp, \
                patch.object(derived, "_call_relation_model") as call:
            derived._generate_relations("m", "/tmp/a.pdf")
        call.assert_not_called()  # 幂等：不重复调用模型

    def test_relation_generation_tolerates_malformed_entity_record(self):
        self.store.set_derived_record(
            "material", "m", derived.KIND_ENTITY_EXTRACTION, "ok",
            {"items": ["bad", {"name": "A"}]}, "h", "g",
        )
        with patch.object(derived, "_input_text", return_value="A替代B"), \
                patch.object(derived, "_call_relation_model", side_effect=urllib.error.URLError("down")):
            derived._generate_relations("m", "/tmp/a.pdf")
        rel = self.store.get_derived_record("material", "m", derived.KIND_RELATION_EXTRACTION)
        self.assertEqual(rel["status"], "ok")


class TestBackfillScript(unittest.TestCase):
    """回填脚本 backfill_relations.py：可恢复、分批、等待与失败报告。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        derived.reset_relation_task_flags()
        self.store = derived_store.DerivedStore.instance()
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
        import backfill_relations as bf
        self.bf = bf
        # 测试隔离（P0-1/P1-4）：mock 实例锁与索引预检——测试进程绝不触碰
        # 生产 data 目录的锁文件，也不依赖真实 ChromaDB 的健康状态。
        self._lock_patcher = patch(
            "instance_lock.acquire", return_value=(unittest.mock.MagicMock(), None)
        )
        self._lock_patcher.start()
        self._chroma_patcher = patch(
            "vector_store.get_collection", return_value=unittest.mock.MagicMock()
        )
        self._chroma_patcher.start()
        self.addCleanup(self._lock_patcher.stop)
        self.addCleanup(self._chroma_patcher.stop)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _jobstore(self, records):
        store = unittest.mock.MagicMock()
        store.instance.return_value.list.return_value = records
        return store

    def _run_main(self, argv):
        with patch.object(sys, "argv", argv):
            return self.bf.main()

    def test_progress_roundtrip(self):
        bf = self.bf
        pf = self._tmp / "p.json"
        bf.save_progress(pf, {"m1", "m2"})   # 写"稳定完成项"
        self.assertEqual(bf.load_progress(pf), {"m1", "m2"})

    def test_progress_corrupt_returns_empty(self):
        bf = self.bf
        pf = self._tmp / "p.json"
        pf.write_text("{not-json", encoding="utf-8")   # 损坏 → 全部重跑而非崩溃
        self.assertEqual(bf.load_progress(pf), set())

    def test_resume_skips_done_and_counts_ok(self):
        # 已完成的材料不进 progress 时才会被投递；--wait 后 ok 计入进度并落盘
        bf = self.bf
        pf = self._tmp / "p.json"
        bf.save_progress(pf, {"m_done"})   # m_done 已完成 → 应被跳过
        records = [
            {"material_id": "m_done", "file_name": "done.pdf"},
            {"material_id": "m_new", "file_name": "new.pdf"},
        ]

        def _fake_refresh(mid, _sp):
            # 模拟后台任务落定：为提交的材料写 ok 派生产物
            self.store.set_derived_record(
                derived.OWNER_MATERIAL, mid, derived.KIND_RELATION_EXTRACTION, "ok",
                {"items": [], "source": "fallback"}, "h", "g",
            )

        with patch.object(ingestion, "JobStore", self._jobstore(records)), \
                patch.object(ingestion, "is_recycled", return_value=False), \
                patch.object(ingestion, "source_path_of", return_value="/x.pdf"), \
                patch.object(derived, "refresh_analysis", side_effect=_fake_refresh):
            code = self._run_main(["prog", "--resume", str(pf), "--wait",
                                   "--batch-size", "10", "--timeout", "5"])

        self.assertEqual(code, 0)                     # 无失败 → 返回 0
        self.assertEqual(bf.load_progress(pf), {"m_done", "m_new"})  # 稳定项已记录，可安全重跑

    def test_refresh_raises_is_reported_as_failure(self):
        # 提交阶段异常 → 记入失败明细（仅 materialId + 错误类型），不回写进度
        bf = self.bf
        pf = self._tmp / "p.json"
        records = [{"material_id": "m_x", "file_name": "x.pdf"}]

        class Boom(Exception):
            pass

        with patch.object(ingestion, "JobStore", self._jobstore(records)), \
                patch.object(ingestion, "is_recycled", return_value=False), \
                patch.object(ingestion, "source_path_of", return_value="/x.pdf"), \
                patch.object(derived, "refresh_analysis", side_effect=Boom("down")):
            code = self._run_main(["prog", "--resume", str(pf)])

        self.assertEqual(code, 1)                     # 存在失败 → 非零退出码
        self.assertEqual(bf.load_progress(pf), set())  # 失败项不进进度，下轮重试

    def test_wait_timeout_marks_failure(self):
        # --wait 下后台任务超时未落定 → 计为失败，不回写进度
        bf = self.bf
        pf = self._tmp / "p.json"
        records = [{"material_id": "m_t", "file_name": "t.pdf"}]

        with patch.object(ingestion, "JobStore", self._jobstore(records)), \
                patch.object(ingestion, "is_recycled", return_value=False), \
                patch.object(ingestion, "source_path_of", return_value="/x.pdf"), \
                patch.object(derived, "refresh_analysis") as rf:   # 不落任何记录 → 必然超时
            rf.return_value = None
            code = self._run_main(["prog", "--resume", str(pf), "--wait",
                                   "--timeout", "0.05"])

        self.assertEqual(code, 1)
        self.assertEqual(bf.load_progress(pf), set())

    def test_wait_rejects_preexisting_ok_until_this_run_updates_record(self):
        """回填重算时，提交前已有的 ok 不能被误认为本轮已完成。"""
        bf = self.bf
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, "m", derived.KIND_RELATION_EXTRACTION, "ok",
            {"items": []}, "old-hash", "g",
        )
        previous = bf._record_version(self.store.get_derived_record(
            derived.OWNER_MATERIAL, "m", derived.KIND_RELATION_EXTRACTION,
        ))
        with patch.object(bf.time, "monotonic", side_effect=[0, 0, 1]), \
                patch.object(bf.time, "sleep"):
            status = bf._wait_terminal(self.store, "m", 0.5, previous)
        self.assertIsNone(status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
