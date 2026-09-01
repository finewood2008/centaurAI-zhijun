"""P11 治理待办测试。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException

import wiki_store
from mindos import governance
from mindos.stores import governance_store

_real_parse_fm = wiki_store._parse_frontmatter


def _new_store(self) -> Path:
    """为每个测试使用独立临时 DB，返回路径。"""
    tmp = tempfile.mkdtemp(prefix="govtest_")
    path = Path(tmp) / "governance.db"
    governance_store.reset_for_tests(path)
    return path


class TestGovernanceStore(unittest.TestCase):

    def setUp(self):
        self.db = _new_store(self)
        self.store = governance_store.instance()

    def test_create_and_list(self):
        n = self.store.create([{
            "kind": "duplicate", "title": "t", "reason": "r", "snippet": "s",
            "source_knowledge_id": "ka", "target_knowledge_id": "kb",
            "score": 0.8, "fingerprint": "duplicate:ka:kb",
        }])
        self.assertEqual(n, 1)
        items = self.store.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "duplicate")
        self.assertEqual(items[0]["status"], "pending")

    def test_create_idempotent(self):
        item = {"kind": "duplicate", "title": "t", "reason": "r", "snippet": "s",
                "source_knowledge_id": "ka", "target_knowledge_id": "kb",
                "score": 0.8, "fingerprint": "duplicate:ka:kb"}
        self.assertEqual(self.store.create([item]), 1)
        self.assertEqual(self.store.create([item]), 0)  # 幂等

    def test_create_reopens_resolved_fingerprint(self):
        """已处理候选保留审计历史，但不阻止同一问题被后续扫描重新提审。"""
        for status in ("ignored", "merged", "archived"):
            item = {"kind": "duplicate", "title": "t", "reason": "r", "snippet": "s",
                    "source_knowledge_id": "ka", "target_knowledge_id": "kb",
                    "score": 0.8, "fingerprint": f"duplicate:ka:kb:{status}"}
            self.assertEqual(self.store.create([item]), 1)
            original = next(row for row in self.store.list(status="pending") if row["status"] == "pending")
            self.assertEqual(self.store.resolve(original["id"], status)["status"], status)
            self.assertEqual(self.store.create([item]), 1)
            rows = [row for row in self.store.list() if row["status"] in (status, "pending")]
            self.assertEqual(len([row for row in rows if row["status"] == status]), 1)
        self.assertEqual(len(self.store.list(status="pending")), 3)

    def test_resolve_ignored(self):
        self.store.create([{"kind": "outdated", "title": "t", "reason": "r", "snippet": "",
                            "material_id": "mindos_x", "score": 0.9,
                            "fingerprint": "outdated:kc:mindos_x"}])
        item = self.store.list()[0]
        resolved = self.store.resolve(item["id"], "ignored", "no change")
        self.assertEqual(resolved["status"], "ignored")
        self.assertEqual(resolved["note"], "no change")

    def test_archive_material(self):
        self.store.archive_material("mindos_x")
        self.assertIn("mindos_x", self.store.archived_material_ids())
        self.store.unarchive_material("mindos_x")
        self.assertNotIn("mindos_x", self.store.archived_material_ids())

    def test_recover_processing(self):
        """仅恢复租约过期的 processing 中间态，不触碰仍在执行的仲裁。"""
        self.store.create([{"kind": "relation", "title": "t", "reason": "r", "snippet": "",
                            "material_id": "mindos_x", "score": 0.6,
                            "fingerprint": "relation:kc:mindos_x"}])
        item = self.store.list()[0]
        # 模拟进行中的仲裁：记录进入 processing，租约未过期
        self.store.resolve(item["id"], "processing")
        self.assertEqual(self.store.get(item["id"])["status"], "processing")
        # 未超过安全超时 → 不恢复（不重置正在执行的仲裁）
        recovered = self.store.recover_processing(timeout_seconds=3600)
        self.assertEqual(recovered, 0)
        self.assertEqual(self.store.get(item["id"])["status"], "processing")
        # 租约过期（进程崩溃遗留）→ 恢复为 pending
        recovered = self.store.recover_processing(timeout_seconds=0)
        self.assertEqual(recovered, 1)
        self.assertEqual(self.store.get(item["id"])["status"], "pending")

    def test_recover_default_timeout_does_not_reclaim_recent(self):
        """默认超时（配置化，明显大于最大预期操作）不回收近期仍在执行的仲裁。"""
        self.store.create([{"kind": "relation", "title": "t", "reason": "r", "snippet": "",
                            "material_id": "mindos_x", "score": 0.6,
                            "fingerprint": "relation:kc:mindos_x"}])
        item = self.store.list()[0]
        self.store.resolve(item["id"], "processing")
        recovered = self.store.recover_processing()  # 默认 RECOVER_TIMEOUT_SECONDS
        self.assertEqual(recovered, 0)  # 不重复回收 → 不会重复合并/归档
        self.assertEqual(self.store.get(item["id"])["status"], "processing")

    def test_rollback_does_not_overwrite_new_claim(self):
        """并发竞态：旧 claim 的回滚/完成不会覆盖新一次抢占（claim token 匹配）。"""
        self.store.create([{"kind": "relation", "title": "t", "reason": "r", "snippet": "",
                            "material_id": "mindos_x", "score": 0.6,
                            "fingerprint": "relation:kc:mindos_x"}])
        item = self.store.list()[0]
        # 请求 A 抢占
        self.store.resolve(item["id"], "processing", claim_token="token_a")
        self.assertEqual(self.store.get(item["id"])["status"], "processing")
        # 租约过期 → 扫描恢复为 pending（清除 A 的 token）
        self.store.recover_processing(timeout_seconds=0)
        self.assertEqual(self.store.get(item["id"])["status"], "pending")
        # 请求 B 重新抢占
        self.store.resolve(item["id"], "processing", claim_token="token_b")
        self.assertEqual(self.store.get(item["id"])["status"], "processing")
        # A 失败后用旧 token 回滚 → 不生效，B 的抢占不被覆盖
        rolled = self.store.resolve(item["id"], "pending", from_status="processing", claim_token="token_a")
        self.assertIsNone(rolled)
        self.assertEqual(self.store.get(item["id"])["status"], "processing")
        # B 正常完成
        done = self.store.resolve(item["id"], "archived", from_status="processing", claim_token="token_b")
        self.assertEqual(done["status"], "archived")

    def test_legacy_null_processing_migrated(self):
        """旧库中无租约时间戳的 processing 记录在初始化迁移时恢复为 pending，避免永久卡住。"""
        db_path = _new_store(self)
        store = governance_store.instance()
        store.create([{"kind": "relation", "title": "t", "reason": "r", "snippet": "",
                       "material_id": "mindos_x", "score": 0.6,
                       "fingerprint": "relation:kc:mindos_x"}])
        item = store.list()[0]
        # 模拟旧库状态：processing 且租约字段为 NULL
        conn = store._connect()
        with conn:
            conn.execute(
                "UPDATE governance_items SET status='processing', processing_started_at=NULL WHERE id=?",
                (item["id"],),
            )
        conn.close()
        # 重新初始化触发迁移
        governance_store.reset_for_tests(db_path)
        store2 = governance_store.instance()
        self.assertEqual(store2.get(item["id"])["status"], "pending")


class TestScan(unittest.TestCase):

    def setUp(self):
        self.db = _new_store(self)
        self.store = governance_store.instance()

    def test_scan_duplicate(self):
        page_a = {"path": "/wiki/a.md", "title": "A", "content": "---\ntitle: \"A\"\nmindos_card: true\n---\n# A\nbody a"}
        page_b = {"path": "/wiki/b.md", "title": "B", "content": "---\ntitle: \"B\"\nmindos_card: true\n---\n# B\nbody b"}
        with patch.object(governance.knowledge, "knowledge_list",
                          return_value={"items": [{"knowledgeId": "ka", "title": "A", "content": "x"},
                                                  {"knowledgeId": "kb", "title": "B", "content": "y"}]}), \
             patch.object(governance.knowledge, "_find", side_effect=lambda kid: {"ka": page_a, "kb": page_b}[kid]), \
             patch.object(governance.knowledge, "search_cards",
                          return_value=[{"knowledgeId": "kb", "title": "B", "snippet": "s", "score": 0.9}]):
            result = governance.scan()
        self.assertGreaterEqual(result["created"], 1)
        items = self.store.list(kind="duplicate")
        self.assertEqual(len(items), 1)
        self.assertIn("ka", items[0]["sourceKnowledgeId"])

    def test_scan_outdated(self):
        page = {"path": "/wiki/c.md", "title": "C",
                "content": "---\ntitle: \"C\"\nmindos_card: true\n---\n# C\nbody"}
        with patch.object(governance.knowledge, "knowledge_list",
                          return_value={"items": [{"knowledgeId": "kc", "title": "C", "content": "x"}]}), \
             patch.object(governance.knowledge, "_find", return_value=page), \
             patch.object(governance.knowledge, "_source_ids", return_value=["mindos_missing"]), \
             patch.object(governance.knowledge, "search_cards", return_value=[]), \
             patch.object(governance.knowledge, "_tags", return_value=[]), \
             patch.object(governance.related, "_similar_materials", return_value=[]), \
             patch.object(governance.related, "_shared_tag_materials", return_value=[]), \
             patch.object(governance.ingestion.JobStore, "instance",
                          return_value=MagicMock(list=lambda: [])):
            result = governance.scan()
        self.assertGreaterEqual(result["created"], 1)
        items = self.store.list(kind="outdated")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["materialId"], "mindos_missing")

    def test_scan_relation(self):
        page = {"path": "/wiki/d.md", "title": "D",
                "content": "---\ntitle: \"D\"\nmindos_card: true\n---\n# D\nbody"}
        with patch.object(governance.knowledge, "knowledge_list",
                          return_value={"items": [{"knowledgeId": "kd", "title": "D", "content": "x"}]}), \
             patch.object(governance.knowledge, "_find", return_value=page), \
             patch.object(governance.knowledge, "_source_ids", return_value=[]), \
             patch.object(governance.knowledge, "search_cards", return_value=[]), \
             patch.object(governance.knowledge, "_tags", return_value=["t"]), \
             patch.object(governance.related, "_similar_materials",
                          return_value=[{"materialId": "mindos_m", "score": 0.7, "reason": "内容相似"}]):
            result = governance.scan()
        items = self.store.list(kind="relation")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["materialId"], "mindos_m")

    def test_scan_relation_excludes_confirmed_source(self):
        """已确认的来源关系不再生成"待确认关联"候选。"""
        page = {"path": "/wiki/d.md", "title": "D",
                "content": "---\ntitle: \"D\"\nmindos_card: true\n---\n# D\nbody"}
        with patch.object(governance.knowledge, "knowledge_list",
                          return_value={"items": [{"knowledgeId": "kd", "title": "D", "content": "x"}]}), \
             patch.object(governance.knowledge, "_find", return_value=page), \
             patch.object(governance.knowledge, "_source_ids", return_value=["mindos_confirmed"]), \
             patch.object(governance.knowledge, "search_cards", return_value=[]), \
             patch.object(governance.knowledge, "_tags", return_value=["t"]), \
             patch.object(governance.related, "_similar_materials",
                          return_value=[{"materialId": "mindos_confirmed", "score": 0.9, "reason": "内容相似"},
                                        {"materialId": "mindos_other", "score": 0.6, "reason": "内容相似"}]):
            result = governance.scan()
        items = self.store.list(kind="relation")
        ids = {it["materialId"] for it in items}
        self.assertNotIn("mindos_confirmed", ids)  # 已确认来源被排除
        self.assertIn("mindos_other", ids)

    def test_scan_relation_excludes_archived(self):
        """已归档原材料不再生成"待确认关联"候选。"""
        self.store.archive_material("mindos_archived")
        page = {"path": "/wiki/d.md", "title": "D",
                "content": "---\ntitle: \"D\"\nmindos_card: true\n---\n# D\nbody"}
        with patch.object(governance.knowledge, "knowledge_list",
                          return_value={"items": [{"knowledgeId": "kd", "title": "D", "content": "x"}]}), \
             patch.object(governance.knowledge, "_find", return_value=page), \
             patch.object(governance.knowledge, "_source_ids", return_value=[]), \
             patch.object(governance.knowledge, "search_cards", return_value=[]), \
             patch.object(governance.knowledge, "_tags", return_value=["t"]), \
             patch.object(governance.related, "_similar_materials",
                          return_value=[{"materialId": "mindos_archived", "score": 0.9, "reason": "内容相似"},
                                        {"materialId": "mindos_other", "score": 0.6, "reason": "内容相似"}]):
            governance.scan()
        items = self.store.list(kind="relation")
        ids = {it["materialId"] for it in items}
        self.assertNotIn("mindos_archived", ids)
        self.assertIn("mindos_other", ids)

    def test_related_layer_excludes_archived(self):
        """related 召回层也排除已归档原材料（P9/P10 相关内容、图谱同样生效）。"""
        self.store.archive_material("mindos_a")
        with patch.object(governance.related.ingestion.JobStore, "instance",
                          return_value=MagicMock(list=lambda: [
                              {"material_id": "mindos_a", "file_name": "A.pdf", "file_type": "document",
                               "source_path": "/w/a.pdf"},
                              {"material_id": "mindos_b", "file_name": "B.pdf", "file_type": "document",
                               "source_path": "/w/b.pdf"},
                          ])), \
             patch.object(governance.related, "_ann_get", return_value={"tags": ["x"]}):
            results = governance.related._shared_tag_materials(["x"], "")
        ids = {r["id"] for r in results}  # P14-09：related 统一 items 结构使用 id
        self.assertNotIn("mindos_a", ids)  # 已归档材料被排除
        self.assertIn("mindos_b", ids)

    def test_scan_outdated_uses_real_status(self):
        """过时判定依据真实处理状态：非 available（含 failed/processing/uploaded）视为过时。"""
        page = {"path": "/wiki/c.md", "title": "C",
                "content": "---\ntitle: \"C\"\nmindos_card: true\n---\n# C\nbody"}
        with patch.object(governance.knowledge, "knowledge_list",
                          return_value={"items": [{"knowledgeId": "kc", "title": "C", "content": "x"}]}), \
             patch.object(governance.knowledge, "_find", return_value=page), \
             patch.object(governance.knowledge, "_source_ids", return_value=["mindos_failed"]), \
             patch.object(governance.knowledge, "search_cards", return_value=[]), \
             patch.object(governance.knowledge, "_tags", return_value=[]), \
             patch.object(governance.related, "_similar_materials", return_value=[]), \
             patch.object(governance.related, "_shared_tag_materials", return_value=[]), \
             patch.object(governance.ingestion, "status_of",
                          return_value={"materialId": "mindos_failed", "status": "failed"}):
            governance.scan()
        items = self.store.list(kind="outdated")
        self.assertEqual(len(items), 1)  # failed 状态 → 过时

    def test_scan_available_material_not_outdated(self):
        """available 状态的材料不生成过时候选。"""
        page = {"path": "/wiki/c.md", "title": "C",
                "content": "---\ntitle: \"C\"\nmindos_card: true\n---\n# C\nbody"}
        with patch.object(governance.knowledge, "knowledge_list",
                          return_value={"items": [{"knowledgeId": "kc", "title": "C", "content": "x"}]}), \
             patch.object(governance.knowledge, "_find", return_value=page), \
             patch.object(governance.knowledge, "_source_ids", return_value=["mindos_ok"]), \
             patch.object(governance.knowledge, "search_cards", return_value=[]), \
             patch.object(governance.knowledge, "_tags", return_value=[]), \
             patch.object(governance.related, "_similar_materials", return_value=[]), \
             patch.object(governance.related, "_shared_tag_materials", return_value=[]), \
             patch.object(governance.ingestion, "status_of",
                          return_value={"materialId": "mindos_ok", "status": "available"}):
            governance.scan()
        items = self.store.list(kind="outdated")
        self.assertEqual(len(items), 0)  # available → 不过时

    def test_scan_does_not_recover_processing(self):
        """运行中的扫描不恢复 processing 中间态（避免回收仍在执行的仲裁）。"""
        self.store.create([{"kind": "relation", "title": "t", "reason": "r", "snippet": "",
                            "material_id": "mindos_x", "score": 0.6,
                            "fingerprint": "relation:kc:mindos_x"}])
        item = self.store.list()[0]
        self.store.resolve(item["id"], "processing", claim_token="token_x")
        with patch.object(governance.knowledge, "knowledge_list", return_value={"items": []}):
            governance.scan()
        self.assertEqual(self.store.get(item["id"])["status"], "processing")  # 未被恢复


class TestResolve(unittest.TestCase):

    def setUp(self):
        self.db = _new_store(self)
        self.store = governance_store.instance()

    def test_ignore_does_not_write(self):
        self.store.create([{"kind": "duplicate", "title": "t", "reason": "r", "snippet": "s",
                            "source_knowledge_id": "ka", "target_knowledge_id": "kb",
                            "score": 0.8, "fingerprint": "duplicate:ka:kb"}])
        item = self.store.list()[0]
        with patch.object(governance.knowledge, "wiki_store") as mock_wiki, \
             patch.object(governance.knowledge, "_find") as mock_find:
            resolved = governance.resolve_item(item["id"], governance.ResolveRequest(action="ignore"))
        self.assertEqual(resolved["status"], "ignored")
        mock_wiki.write_page.assert_not_called()
        mock_find.assert_not_called()

    def test_stale_claim_does_not_execute_entity(self):
        """旧 claim 已过期/被覆盖后，实体操作前的二次校验失败（不重复合并/归档）。"""
        self.store.create([{"kind": "duplicate", "title": "t", "reason": "r", "snippet": "s",
                            "source_knowledge_id": "ka", "target_knowledge_id": "kb",
                            "score": 0.8, "fingerprint": "duplicate:ka:kb"}])
        item = self.store.list()[0]
        # 请求 A 抢占
        self.store.resolve(item["id"], "processing", claim_token="token_a")
        # 服务重启恢复（遗留 processing）→ 请求 B 重新抢占
        self.store.recover_processing(timeout_seconds=0)
        self.store.resolve(item["id"], "processing", claim_token="token_b")
        # A 的 claim 已失效 → 二次校验失败；B 的 claim 有效
        self.assertFalse(governance._claim_valid(item["id"], "token_a"))
        self.assertTrue(governance._claim_valid(item["id"], "token_b"))

    def test_resolve_skips_entity_when_claim_invalid(self):
        """二次校验失败时，resolve_item 不执行任何实体写入。"""
        self.store.create([{"kind": "duplicate", "title": "t", "reason": "r", "snippet": "s",
                            "source_knowledge_id": "ka", "target_knowledge_id": "kb",
                            "score": 0.8, "fingerprint": "duplicate:ka:kb"}])
        item = self.store.list()[0]
        with patch.object(governance, "_claim_valid", return_value=False), \
             patch.object(governance.knowledge, "_find") as mock_find, \
             patch.object(governance.knowledge, "wiki_store") as mock_wiki:
            with self.assertRaises(HTTPException) as ctx:
                governance.resolve_item(item["id"], governance.ResolveRequest(action="merge", keepKnowledgeId="ka"))
            self.assertEqual(ctx.exception.status_code, 409)
        mock_find.assert_not_called()  # 未读取卡片
        mock_wiki.write_page.assert_not_called()  # 未写入任何实体

    def test_merge_writes_only_knowledge(self):
        source = {"path": "/wiki/a.md", "title": "A",
                  "content": '---\ntitle: "A"\nmindos_card: true\n---\n# A\nsource body'}
        target = {"path": "/wiki/b.md", "title": "B",
                  "content": '---\ntitle: "B"\nmindos_card: true\n---\n# B\ntarget body'}
        self.store.create([{"kind": "duplicate", "title": "t", "reason": "r", "snippet": "s",
                            "source_knowledge_id": "ka", "target_knowledge_id": "kb",
                            "score": 0.8, "fingerprint": "duplicate:ka:kb"}])
        item = self.store.list()[0]
        written = []
        with patch.object(governance.knowledge, "_find", side_effect=lambda kid: {"ka": source, "kb": target}[kid]), \
             patch.object(governance.knowledge, "wiki_store") as mock_wiki:
            mock_wiki._parse_frontmatter.side_effect = _real_parse_fm
            mock_wiki.write_page.side_effect = lambda path, content, **kw: written.append((path, content))
            # 用户明确选择保留 ka（source）
            resolved = governance.resolve_item(
                item["id"], governance.ResolveRequest(action="merge", keepKnowledgeId="ka"))
        self.assertEqual(resolved["status"], "merged")
        # 只写卡片路径，绝无原材料路径
        self.assertEqual({p for p, _ in written}, {"/wiki/a.md", "/wiki/b.md"})
        # target（kb）被归档
        kb_written = [c for p, c in written if p == "/wiki/b.md"]
        self.assertTrue(kb_written and "mindos_archived" in kb_written[0])

    def test_merge_retry_does_not_append_target_body_twice(self):
        source = {"path": "/wiki/a.md", "title": "A",
                  "content": '---\ntitle: "A"\nmindos_card: true\n---\n# A\nsource body'}
        target = {"path": "/wiki/b.md", "title": "B",
                  "content": '---\ntitle: "B"\nmindos_card: true\n---\n# B\ntarget body'}
        fail_target_once = {"value": True}

        def write_page(path, content, **_kwargs):
            if path == source["path"]:
                source["content"] = content
                return source
            if fail_target_once["value"]:
                fail_target_once["value"] = False
                raise OSError("disk busy")
            target["content"] = content
            return target

        with patch.object(governance.knowledge, "_find", side_effect=lambda kid: {"ka": source, "kb": target}[kid]), \
             patch.object(governance.knowledge, "_sync_card_index"), \
             patch.object(governance.knowledge, "wiki_store") as mock_wiki:
            mock_wiki._parse_frontmatter.side_effect = _real_parse_fm
            mock_wiki.write_page.side_effect = write_page
            with self.assertRaises(OSError):
                governance._merge_knowledge("ka", "kb")
            governance._merge_knowledge("ka", "kb")

        self.assertEqual(source["content"].count("target body"), 1)
        self.assertIn('mindos_merged_source_ids: ["kb"]', source["content"])
        self.assertIn('mindos_merged_into: "ka"', target["content"])

    def test_merge_requires_keep(self):
        """合并必须由用户明确选择主卡片，禁止隐式依赖 ID 排序。"""
        self.store.create([{"kind": "duplicate", "title": "t", "reason": "r", "snippet": "s",
                            "source_knowledge_id": "ka", "target_knowledge_id": "kb",
                            "score": 0.8, "fingerprint": "duplicate:ka:kb"}])
        item = self.store.list()[0]
        with self.assertRaises(HTTPException) as ctx:
            governance.resolve_item(item["id"], governance.ResolveRequest(action="merge"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_merge_rejects_non_duplicate(self):
        self.store.create([{"kind": "outdated", "title": "t", "reason": "r", "snippet": "",
                            "material_id": "mindos_x", "score": 0.9,
                            "fingerprint": "outdated:kc:mindos_x"}])
        item = self.store.list()[0]
        with self.assertRaises(HTTPException) as ctx:
            governance.resolve_item(item["id"], governance.ResolveRequest(action="merge"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_resolve_rejects_processed(self):
        """已处理的待办不能再次仲裁（同一待办重复合并不会再次写入）。"""
        self.store.create([{"kind": "duplicate", "title": "t", "reason": "r", "snippet": "s",
                            "source_knowledge_id": "ka", "target_knowledge_id": "kb",
                            "score": 0.8, "fingerprint": "duplicate:ka:kb"}])
        item = self.store.list()[0]
        self.store.resolve(item["id"], "merged", "第一次合并")
        with self.assertRaises(HTTPException) as ctx:
            governance.resolve_item(item["id"], governance.ResolveRequest(action="merge", keepKnowledgeId="ka"))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_outdated_archive_action_rejected(self):
        """知识卡片归档已移除，治理仲裁不再接受 archive 动作。"""
        card_page = {"path": "/wiki/c.md", "title": "C",
                     "content": '---\ntitle: "C"\nmindos_card: true\n---\n# C\nbody'}
        self.store.create([{"kind": "outdated", "title": "t", "reason": "r", "snippet": "",
                            "source_knowledge_id": "kc", "material_id": "mindos_x",
                            "score": 0.9, "fingerprint": "outdated:kc:mindos_x"}])
        item = self.store.list()[0]
        with self.assertRaises(HTTPException) as ctx:
            governance.resolve_item(item["id"], governance.ResolveRequest(action="archive"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_relation_archive_action_rejected(self):
        self.store.create([{"kind": "relation", "title": "t", "reason": "r", "snippet": "",
                            "source_knowledge_id": "kc", "material_id": "mindos_x",
                            "score": 0.6, "fingerprint": "relation:kc:mindos_x"}])
        item = self.store.list()[0]
        with self.assertRaises(HTTPException) as ctx:
            governance.resolve_item(item["id"], governance.ResolveRequest(action="archive"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_duplicate_archive_action_rejected(self):
        source = {"path": "/wiki/a.md", "title": "A",
                  "content": '---\ntitle: "A"\nmindos_card: true\n---\n# A\nbody'}
        target = {"path": "/wiki/b.md", "title": "B",
                  "content": '---\ntitle: "B"\nmindos_card: true\n---\n# B\nbody'}
        self.store.create([{"kind": "duplicate", "title": "t", "reason": "r", "snippet": "s",
                            "source_knowledge_id": "ka", "target_knowledge_id": "kb",
                            "score": 0.8, "fingerprint": "duplicate:ka:kb"}])
        item = self.store.list()[0]
        with self.assertRaises(HTTPException) as ctx:
            governance.resolve_item(
                item["id"], governance.ResolveRequest(action="archive", keepKnowledgeId="ka"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_duplicate_archive_requires_keep(self):
        """疑似重复归档必须明确选择保留卡片，避免默认归档错误卡片。"""
        self.store.create([{"kind": "duplicate", "title": "t", "reason": "r", "snippet": "s",
                            "source_knowledge_id": "ka", "target_knowledge_id": "kb",
                            "score": 0.8, "fingerprint": "duplicate:ka:kb"}])
        item = self.store.list()[0]
        with patch.object(governance.knowledge, "_find") as mock_find, \
             patch.object(governance.knowledge, "wiki_store") as mock_wiki:
            with self.assertRaises(HTTPException) as ctx:
                governance.resolve_item(item["id"], governance.ResolveRequest(action="archive"))
        self.assertEqual(ctx.exception.status_code, 400)
        mock_find.assert_not_called()
        mock_wiki.write_page.assert_not_called()


class TestKnowledgeArchiveFilter(unittest.TestCase):

    def test_is_active_filters_archived(self):
        from mindos import knowledge
        archived = {"path": "/wiki/a.md",
                    "content": '---\ntitle: "A"\nmindos_card: true\nmindos_archived: true\n---\n# A\nbody'}
        active = {"path": "/wiki/b.md",
                  "content": '---\ntitle: "B"\nmindos_card: true\n---\n# B\nbody'}
        self.assertFalse(knowledge._is_active_mindos_card(archived))
        self.assertTrue(knowledge._is_active_mindos_card(active))


if __name__ == "__main__":
    unittest.main()
