"""MindOS P14-08 图片语义理解与多模态检索单元测试。

覆盖 mindos.search._visual_material_results 与 unified_search 返回契约：
- CLIP 以文搜图（复用入库时建立的图片视觉索引）→ visualMaterials 分组；
- 文本 BGE 分与 CLIP 分独立返回、不合并排序（视觉分不进 materials / total）；
- 未映射源 / 归档材料 / 非图片类型 / 低于阈值的情节一律不宣称视觉命中；
- CLIP 不可用 / 查询嵌入失败 / 检索异常 → 空 visualMaterials 且
  capabilities.visualSearch=false（显式降级，不把失败吞掉伪称“未命中”）；
- 同一材料按最高视觉分去重；不返回 source path / 原始视觉 embedding。

依赖项目 .venv，可独立于 server 运行：
    .venv\\Scripts\\python.exe -m unittest test_mindos_p14_08_visual_search -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos import search
from fastapi import Request


def _clip_vector() -> list[float]:
    return [0.1, 0.2, 0.3, 0.4]


def _image_hit(source: str, shot: float) -> dict:
    return {"source_path": source, "vector_score": shot, "modality": "image"}


def _record(material_id: str, source: str, file_type: str = "image", name: str | None = None) -> dict:
    return {
        "material_id": material_id,
        "file_name": name or f"{material_id}.jpg",
        "file_type": file_type,
        "source_path": source,
    }


class VisualMaterialResultsTests(unittest.TestCase):
    """_visual_material_results：CLIP 命中 → material_for_source 映射 → 过滤 → 去重。"""

    def _run(
        self,
        query: str = "一只在草地上的狗",
        limit: int = 12,
        imgs: list[dict] | None = None,
        records: dict[str, dict] | None = None,
        snippets: dict[str, str] | None = None,
        archived: tuple = (),
        clip_ok: bool = True,
        clip_embed: list[float] | None = None,
        search_images_error: bool = False,
    ) -> tuple[list[dict], bool]:
        imgs = [{"source_path": "src://a.jpg", "vector_score": 0.83, "modality": "image"}] if imgs is None else imgs
        records = {"src://a.jpg": _record("mindos_img_a", "src://a.jpg")} if records is None else records
        snippets = {**{s: "" for s in records}, **(snippets or {})}

        def _snippet_of(source: str, limit: int = 100):
            text = snippets.get(source, "")
            return [{"text": text}] if text else []

        with patch.object(search, "CLIP_ENABLED", True), patch.object(
            search, "clip_available", return_value=clip_ok
        ), patch.object(
            search, "embed_query_clip",
            return_value=_clip_vector() if clip_embed is None else clip_embed,
        ), patch.object(
            search, "search_images",
            side_effect=Exception("boom") if search_images_error else lambda *a, **kw: imgs,
        ), patch.object(search, "IMAGE_SIM_THRESHOLD", 0.28), patch.object(
            search.ingestion, "recycled_material_ids", return_value=set(archived),
        ), patch.object(
            search.ingestion, "material_for_source", side_effect=lambda s, device_scope="global": records.get(s) if device_scope == "global" else None,
        ), patch.object(search.ingestion, "status_of", return_value={"status": "available"}
        ), patch("mindos.stage_d_admin.legacy_read_enabled", return_value=True
        ), patch.object(
            search, "get_source_chunks", side_effect=_snippet_of,
        ):
            return search._visual_material_results(query, limit)

    def test_visual_hit_returns_match_mode_and_preview(self):
        items, ok = self._run()
        self.assertTrue(ok)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["materialId"], "mindos_img_a")
        self.assertEqual(item["fileType"], "image")
        self.assertEqual(item["score"], 0.83)
        self.assertEqual(item["matchMode"], "visual")
        self.assertEqual(item["previewUrl"], "/api/mindos/materials/mindos_img_a/file")

    def test_payload_never_leaks_source_path_or_embedding(self):
        items, _ = self._run()
        payload = str(items)
        self.assertNotIn("src://", payload)
        self.assertNotIn("0.1, 0.2", payload)

    def test_unmapped_source_dropped(self):
        items, ok = self._run(
            imgs=[_image_hit("src://ghost.jpg", 0.9), _image_hit("src://a.jpg", 0.83)],
            records={"src://a.jpg": _record("mindos_img_a", "src://a.jpg")},
        )
        self.assertTrue(ok)
        self.assertEqual([i["materialId"] for i in items], ["mindos_img_a"])

    def test_archived_dropped(self):
        items, _ = self._run(archived=("mindos_img_a",))
        self.assertEqual(items, [])

    def test_non_image_material_dropped(self):
        items, _ = self._run(
            imgs=[_image_hit("src://doc.jpg", 0.9)],
            records={"src://doc.jpg": _record("mindos_doc_1", "src://doc.jpg", file_type="document")},
        )
        self.assertEqual(items, [])

    def test_below_threshold_dropped(self):
        items, _ = self._run(imgs=[_image_hit("src://a.jpg", 0.1)])
        self.assertEqual(items, [])

    def test_dedup_keeps_highest_visual_score(self):
        items, _ = self._run(
            imgs=[
                _image_hit("src://a.jpg", 0.6),
                _image_hit("src://a.jpg", 0.95),
                _image_hit("src://b.jpg", 0.7),
            ],
            records={
                "src://a.jpg": _record("mindos_img_a", "src://a.jpg"),
                "src://b.jpg": _record("mindos_img_b", "src://b.jpg"),
            },
        )
        self.assertEqual(len(items), 2)
        by_id = {i["materialId"]: i["score"] for i in items}
        self.assertEqual(by_id["mindos_img_a"], 0.95)  # 同材料取最高视觉分

    def test_snippet_comes_from_text_collection(self):
        items, _ = self._run(
            snippets={"src://a.jpg": "一只金毛犬在草地上奔跑"},
        )
        self.assertIn("金毛犬", items[0]["snippet"])

    def test_clip_disabled_returns_empty_and_false(self):
        items, ok = self._run(clip_ok=False)
        self.assertEqual(items, [])
        self.assertFalse(ok)

    def test_query_embed_failure_returns_empty_and_false(self):
        items, ok = self._run(clip_embed=[])  # embed_query_clip 不可用时返回空
        self.assertEqual(items, [])
        self.assertFalse(ok)

    def test_search_images_exception_returns_empty_and_false(self):
        items, ok = self._run(search_images_error=True)
        self.assertEqual(items, [])
        self.assertFalse(ok)

    def test_late_valid_image_recalled_beyond_first_batch(self):
        """P1 回归：前 40 条均为无效记录时，排在第 41 位的有效 MindOS 图片仍必须被召回。

        search_images 模拟真实 Chroma 行为：n_results=N 只返回前 N 条。前 40 条
        全是未映射的旧项目图片（无法映射到 MindOS 材料），第 41 条才是有效 MindOS
        图片——固定只召回 40 条会漏掉它，分批扩大召回必须仍返回该图片。
        """
        junk = [_image_hit(f"src://legacy_{i}.jpg", 0.99 - i * 0.001) for i in range(40)]
        valid = _image_hit("src://mindos_valid.jpg", 0.8)
        all_imgs = junk + [valid]
        records = {"src://mindos_valid.jpg": _record("mindos_img_valid", "src://mindos_valid.jpg")}

        def _truncated_search(query_embedding, n_results=10, **kwargs):
            return all_imgs[:n_results]

        with patch.object(search, "CLIP_ENABLED", True), patch.object(
            search, "clip_available", return_value=True
        ), patch.object(search, "embed_query_clip", return_value=_clip_vector()), patch.object(
            search, "search_images", side_effect=_truncated_search,
        ), patch.object(search, "IMAGE_SIM_THRESHOLD", 0.28), patch.object(
            search.ingestion, "recycled_material_ids", return_value=set(),
        ), patch.object(
            search.ingestion, "material_for_source", side_effect=lambda s, device_scope="global": records.get(s) if device_scope == "global" else None,
        ), patch.object(search.ingestion, "status_of", return_value={"status": "available"}), patch("mindos.stage_d_admin.legacy_read_enabled", return_value=True), patch.object(search, "get_source_chunks", return_value=[]):
            items, ok = search._visual_material_results("一只在草地上的狗", limit=5)

        self.assertTrue(ok)
        self.assertEqual([i["materialId"] for i in items], ["mindos_img_valid"])


class UnifiedSearchContractTests(unittest.TestCase):
    """unified_search 返回契约：独立 visualMaterials 分组 + capabilities。"""

    def _call(self, visual_items, visual_ok, materials=(), cards=(), unavailable=()):
        with patch.object(search.knowledge, "search_cards", return_value=list(cards)), patch.object(
            search, "_material_results", return_value=list(materials)
        ), patch.object(search, "_unavailable_material_results", return_value=list(unavailable)
        ), patch.object(
            search, "_visual_material_results", return_value=(list(visual_items), visual_ok)
        ):
            return search.unified_search(Request({"type": "http"}), q="测试查询", limit=12)

    def test_response_has_visual_group_and_capabilities(self):
        resp = self._call(
            visual_items=[{
                "materialId": "mindos_img_a", "title": "a.jpg", "fileType": "image",
                "snippet": "", "score": 0.83, "matchMode": "visual",
                "previewUrl": "/api/mindos/materials/mindos_img_a/file",
            }],
            visual_ok=True,
            materials=[{
                "materialId": "mindos_doc_1", "title": "doc.pdf", "fileType": "document",
                "snippet": "x", "score": 0.9,
            }],
        )
        self.assertEqual(resp["capabilities"], {"visualSearch": True})
        self.assertEqual(len(resp["visualMaterials"]), 1)
        self.assertEqual(resp["visualMaterials"][0]["matchMode"], "visual")

    def test_total_is_text_scope_only(self):
        """视觉命中不计入 total：CLIP 分与 BGE 分不同空间，不与文本命中混算。"""
        resp = self._call(
            visual_items=[{"materialId": "mindos_img_a", "score": 0.83, "matchMode": "visual"}],
            visual_ok=True,
            materials=[{"materialId": "mindos_doc_1", "score": 0.9}],
            cards=[{"knowledgeId": "k1", "score": 0.8}],
        )
        self.assertEqual(resp["total"], 2)  # 1 知识成品 + 1 原材料；不含视觉
        self.assertEqual(len(resp["visualMaterials"]), 1)

    def test_visual_search_false_on_degradation(self):
        resp = self._call(visual_items=[], visual_ok=False)
        self.assertFalse(resp["capabilities"]["visualSearch"])
        self.assertEqual(resp["visualMaterials"], [])

    def test_response_keeps_unavailable_materials_out_of_search_total(self):
        resp = self._call(
            visual_items=[], visual_ok=True,
            unavailable=[{
                "materialId": "mindos_paused", "title": "计划草稿.docx",
                "status": "queued", "reason": "服务中断，任务已暂停", "actions": ["resume"],
            }],
        )
        self.assertEqual(resp["total"], 0)
        self.assertEqual(resp["unavailableTotal"], 1)
        self.assertEqual(resp["unavailableMaterials"][0]["actions"], ["resume"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
