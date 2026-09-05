"""MindOS P14-09 关联推荐的可信 Top-3 闭环单元测试。

覆盖 mindos.related：
- RECOMMENDED_LIMIT=3 产品上限；内部召回池更大，择优后截断；
- 同一对象多来源命中合并为一项，reasons 并列全部依据，不占多个名额；
- 每项返回 scoreBand（高/中）与 sourceType，不暴露原始模型分；
- 不足 3 个时如实返回 total 与 note（原因），绝不硬塞低相关对象；
- 端点级：排除自身、归档关联消失、统一 items 契约。

依赖项目 .venv，可独立于 server 运行：
    .venv\\Scripts\\python.exe -m unittest test_mindos_p14_09_related -v
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos import related


def _req(device_id: str | None = None):
    """构造票据模式下的最小 Request：global(调试) 或指定 device_id 作用域。"""
    return SimpleNamespace(
        state=SimpleNamespace(
            mindos_device_context=SimpleNamespace(device_id=device_id)
        )
    )


def _merged_item(mid: str, source_type: str, score: float, reason: str, reasons: list[str] | None = None) -> dict:
    return {
        "id": mid,
        "sourceType": source_type,
        "title": f"{mid}.md",
        "snippet": "内容片段",
        "score": score,
        "reason": reason,
        "reasons": reasons or [reason],
    }


class ScoreBandTests(unittest.TestCase):
    """scoreBand：只暴露高/中，不暴露原始模型分。"""

    def test_high_and_medium_mapping(self):
        self.assertEqual(related._score_band(0.9), "high")
        self.assertEqual(related._score_band(0.7), "high")   # 达到 _BAND_HIGH
        self.assertEqual(related._score_band(0.69), "medium")
        self.assertEqual(related._score_band(0.5), "medium")
        self.assertEqual(related._score_band(0.15), "medium")  # 达到 _MIN_SCORE 但属一般


class MergeRelatedTests(unittest.TestCase):
    """合并去重：同一对象多来源命中合并为一项，reasons 并列，不占多名额。"""

    def test_same_object_merged_with_all_reasons(self):
        similar = [{"id": "a", "sourceType": "material", "title": "A", "snippet": "s", "score": 0.9, "reason": "内容相似"}]
        shared = [
            {"id": "a", "sourceType": "material", "title": "A", "snippet": "s", "score": 1.0, "reason": "共享标签"},
            {"id": "b", "sourceType": "knowledge", "title": "B", "snippet": "", "score": 0.6, "reason": "共享标签"},
        ]
        merged = related._merge_related([similar, shared])
        self.assertEqual(len(merged), 2)  # a 只占一项
        a = merged[0]
        self.assertEqual(a["id"], "a")
        self.assertEqual(set(a["reasons"]), {"内容相似", "共享标签"})
        # 主 reason 取分数最高的来源（共享标签 1.0 > 内容相似 0.9）
        self.assertEqual(a["reason"], "共享标签")

    def test_reasons_deduplicated(self):
        same = [
            {"id": "a", "sourceType": "material", "title": "A", "snippet": "s", "score": 0.8, "reason": "内容相似"},
            {"id": "a", "sourceType": "material", "title": "A", "snippet": "s", "score": 0.85, "reason": "内容相似"},
        ]
        merged = related._merge_related([same])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["reasons"], ["内容相似"])  # 去重，不重复


class RecommendResponseTests(unittest.TestCase):
    """响应组装：最多 3 项；不足时如实说明原因，绝不凑数。"""

    def test_three_items_no_note(self):
        merged = [_merged_item(f"m{i}", "material", 0.9 - i * 0.1, "内容相似") for i in range(3)]
        resp = related._recommend_response(merged)
        self.assertEqual(len(resp["items"]), 3)
        self.assertEqual(resp["total"], 3)
        self.assertEqual(resp["note"], "")
        self.assertEqual(resp["recommendedLimit"], 3)

    def test_five_items_truncated_to_three(self):
        merged = [_merged_item(f"m{i}", "material", 0.9 - i * 0.1, "内容相似") for i in range(5)]
        resp = related._recommend_response(merged)
        self.assertEqual(len(resp["items"]), 3)
        self.assertEqual(resp["total"], 5)  # total 反映实际达标对象数
        self.assertEqual(resp["note"], "")

    def test_two_items_note_shortfall(self):
        merged = [_merged_item(f"m{i}", "material", 0.6, "共享标签") for i in range(2)]
        resp = related._recommend_response(merged)
        self.assertEqual(len(resp["items"]), 2)
        self.assertEqual(resp["total"], 2)
        self.assertIn("仅 2 项", resp["note"])

    def test_zero_items_note_none(self):
        resp = related._recommend_response([])
        self.assertEqual(resp["items"], [])
        self.assertEqual(resp["total"], 0)
        self.assertEqual(resp["note"], "暂无达到阈值的关联")

    def test_items_never_expose_raw_score(self):
        merged = [_merged_item("m0", "material", 0.9, "内容相似"), _merged_item("m1", "knowledge", 0.5, "共享标签")]
        resp = related._recommend_response(merged)
        payload = str(resp)
        # "scoreBand" 含子串 score，但独立 score 字段（"score":）不应存在
        self.assertNotIn('"score":', payload)
        self.assertNotIn('"score" }', payload)
        self.assertEqual(resp["items"][0]["scoreBand"], "high")
        self.assertEqual(resp["items"][1]["scoreBand"], "medium")


class MaterialRelatedEndpointTests(unittest.TestCase):
    """material_related：统一 items 契约、排除自身、归档过滤、多来源 reasons 合并。"""

    def _records(self):
        return [
            {"material_id": "mat_self", "file_name": "self.pdf", "file_type": "document", "source_path": "src://self.pdf"},
            {"material_id": "mat_a", "file_name": "a.md", "file_type": "document", "source_path": "src://a.md"},
            {"material_id": "mat_archived", "file_name": "old.md", "file_type": "document", "source_path": "src://old.md"},
        ]

    def _ann(self, source: str) -> dict:
        tags = {
            "src://self.pdf": ["项目"],
            "src://a.md": ["项目", "预算"],  # 与 self 共享“项目”
            "src://old.md": ["项目"],
        }
        return {"tags": tags.get(source, [])}

    def test_excludes_self_and_archived_and_merges_reasons(self):
        records = self._records()
        chunks = [
            {"source_path": "src://a.md", "vector_score": 0.9, "text": "预算说明"},
            {"source_path": "src://old.md", "vector_score": 0.85, "text": "旧内容"},  # 归档，应消失
        ]
        with patch.object(related.ingestion, "source_path_of", return_value="src://self.pdf"), patch.object(
            related.ingestion, "material_tags", return_value=["项目"]
        ), patch.object(related.ingestion, "detail_of", return_value={}), patch.object(
            related.ingestion, "summary_text_of", return_value=""
        ), patch.object(related, "get_source_embedding", return_value=[0.1, 0.2]), patch.object(
            related, "vector_search", return_value=chunks
        ), patch.object(
            related.ingestion, "material_for_source",
            side_effect=lambda s, device_scope="global": next((r for r in records if r["source_path"] == s), None),
        ), patch.object(
            related.ingestion.JobStore, "instance", return_value=MagicMock(list=lambda device_scope=None: records),
        ), patch.object(
            related, "_ann_get", side_effect=self._ann,
        ), patch.object(
            related.ingestion, "recycled_material_ids", return_value={"mat_archived"},
        ), patch.object(related.knowledge, "search_cards", return_value=[]), patch.object(
            related.knowledge, "knowledge_list", return_value={"items": []}
        ):
            resp = related.material_related("mat_self", _req())

        self.assertEqual(resp["recommendedLimit"], 3)
        self.assertEqual(len(resp["items"]), 1)
        self.assertEqual(resp["total"], 1)
        self.assertIn("仅 1 项", resp["note"])

        item = resp["items"][0]
        self.assertEqual(item["id"], "mat_a")           # 自身与归档均被排除
        self.assertEqual(item["sourceType"], "material")
        self.assertEqual(set(item["reasons"]), {"内容相似", "共享标签"})
        self.assertIn("预算说明", item["snippet"])

    def test_zero_candidates_note(self):
        with patch.object(related.ingestion, "source_path_of", return_value="src://self.pdf"), patch.object(
            related.ingestion, "material_tags", return_value=[]
        ), patch.object(related.ingestion, "detail_of", return_value={}), patch.object(
            related.ingestion, "summary_text_of", return_value=""
        ), patch.object(related, "get_source_embedding", return_value=[]), patch.object(
            related, "vector_search", return_value=[]
        ), patch.object(
            related.ingestion.JobStore, "instance", return_value=MagicMock(list=lambda device_scope=None: []),
        ), patch.object(related, "_ann_get", return_value={"tags": []}), patch.object(
            related.ingestion, "recycled_material_ids", return_value=set(),
        ), patch.object(related.knowledge, "search_cards", return_value=[]), patch.object(
            related.knowledge, "knowledge_list", return_value={"items": []}
        ):
            resp = related.material_related("mat_self", _req())

        self.assertEqual(resp["items"], [])
        self.assertEqual(resp["total"], 0)
        self.assertEqual(resp["note"], "暂无达到阈值的关联")


class KnowledgeRelatedEndpointTests(unittest.TestCase):
    """knowledge_related：排除自身、sourceType=knowledge、统一 items 契约。"""

    def _cards(self):
        return [
            {"knowledgeId": "k_self", "title": "自身卡片", "content": "自身正文内容", "tags": ["项目"]},
            {"knowledgeId": "k_a", "title": "卡片A", "content": "预算正文", "tags": ["项目"]},
            {"knowledgeId": "k_c", "title": "卡片C", "content": "预算正文", "tags": ["项目"]},
        ]

    def test_excludes_self_and_returns_knowledge_items(self):
        cards = self._cards()

        def _find(kid):
            return next(c for c in cards if c["knowledgeId"] == kid)

        def _public(page):
            return {"tags": page["tags"], "content": page["content"], "title": page["title"]}

        with patch.object(related.knowledge, "_find", side_effect=_find), patch.object(
            related.knowledge, "_public", side_effect=_public
        ), patch.object(
            related.knowledge.wiki_store, "_parse_frontmatter", side_effect=lambda c: ("", c),
        ), patch.object(related, "embed_query", return_value=[0.1, 0.2]), patch.object(
            related, "vector_search", return_value=[]
        ), patch.object(related.ingestion, "material_for_source", return_value=None), patch.object(
            related.ingestion.JobStore, "instance", return_value=MagicMock(list=lambda device_scope=None: []),
        ), patch.object(related, "_ann_get", return_value={"tags": []}), patch.object(
            related.ingestion, "recycled_material_ids", return_value=set(),
        ), patch.object(related.knowledge, "search_cards", return_value=[]), patch.object(
            related.knowledge, "knowledge_list", return_value={"items": cards}
        ):
            resp = related.knowledge_related("k_self", _req())

        ids = [i["id"] for i in resp["items"]]
        self.assertNotIn("k_self", ids)  # 自身不出现
        self.assertEqual(set(ids), {"k_a", "k_c"})
        self.assertTrue(all(i["sourceType"] == "knowledge" for i in resp["items"]))
        self.assertEqual(resp["total"], 2)
        self.assertIn("仅 2 项", resp["note"])
        self.assertTrue(all(i["reasons"] for i in resp["items"]))  # 每项有可解释依据


class DeviceScopeIsolationRelatedTests(unittest.TestCase):
    """阶段 2：关联召回按请求设备作用域隔离，跨设备对象不得回显。"""

    def test_material_related_excludes_cross_device_materials(self):
        records = [
            {"material_id": "mat_a_dev", "file_name": "a.txt", "file_type": "document",
             "source_path": "src://a.txt"},
            {"material_id": "mat_b_dev", "file_name": "b.txt", "file_type": "document",
             "source_path": "src://b.txt"},  # 属于 dev_b，作用域外
        ]
        chunks = [
            {"source_path": "src://b.txt", "vector_score": 0.95, "text": "跨设备内容"},
        ]
        with patch.object(related.ingestion, "source_path_of", return_value="src://self.txt"), patch.object(
            related.ingestion, "material_tags", return_value=["项目"]
        ), patch.object(related.ingestion, "detail_of", return_value={}), patch.object(
            related.ingestion, "summary_text_of", return_value=""
        ), patch.object(related, "get_source_embedding", return_value=[0.1, 0.2]), patch.object(
            related, "vector_search", return_value=chunks
        ), patch.object(
            related.ingestion, "material_for_source",
            # 跨设备 source_path（src://b.txt 属 dev_b）在 dev_a 作用域内应返回 None，
            # 只把本作用域材料（mat_a_dev）映射为候选，杜绝跨设备回显。
            side_effect=lambda s, device_scope="global":
                next((r for r in records if r["source_path"] == s
                      and device_scope == "device:dev_a" and r["material_id"] == "mat_a_dev"), None),
        ), patch.object(
            related.ingestion.JobStore, "instance",
            return_value=MagicMock(
                # dev_a 作用域的存储层只返回本作用域材料（a/dev），b 属 dev_b 不应出现
                list=lambda device_scope=None: [r for r in records if r["material_id"] == "mat_a_dev"],
            ),
        ), patch.object(related, "_ann_get", side_effect=lambda s: {"tags": ["项目"]}), patch.object(
            related.ingestion, "recycled_material_ids", return_value=set(),
        ), patch.object(related.knowledge, "search_cards", return_value=[]), patch.object(
            related.knowledge, "knowledge_list", return_value={"items": []}
        ):
            resp = related.material_related("mat_a_dev", _req("dev_a"))  # 设备 A 作用域

        # dev_a 作用域只召回 a.txt；跨设备 b.txt 向量命中但被作用域过滤
        self.assertEqual([i["id"] for i in resp["items"]], [])
        self.assertEqual(resp["total"], 0)

    def test_knowledge_related_returns_404_for_out_of_scope_card(self):
        from fastapi import HTTPException

        with patch.object(related, "card_ledger_store") as ledger:
            ledger.get.return_value = None  # 卡片不在 dev_a 作用域
            with patch.object(related.knowledge, "_find", return_value={}):
                with self.assertRaises(HTTPException) as ctx:
                    related.knowledge_related("k_cross_device", _req("dev_a"))
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
