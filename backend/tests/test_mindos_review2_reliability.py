"""第二轮 review 修复验收测试（索引可靠性）。

覆盖：
- 修复1：schema 迁移不再立即 commit 空集合——begin 后由后台线程等
  scan_existing 的任务全部终态再校验提交；超时/失败 abort 保留旧集合；
- 修复2：集合切换第二步 rename 失败必须回滚（obsolete→正式名），
  正式集合绝不消失；commit_rebuild 捕获切换异常返回 not ok；
- 修复3：/api/reindex 超时或任一任务失败 → abort 保留旧索引，
  不再把半成品集合切上线；
- 修复5：lifespan 统一启动后台服务（uvicorn server:app 与 python server.py
  等价），幂等且 shutdown 停止 watcher；
- 修复6：完整性巡检入口——verify_source_index 主动枚举已索引源，
  integrity_failed / read_error 的重新入队重建。

全部 mock ChromaDB / watcher / 模型，不触碰真实数据目录。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vector_store as vs
import generation_store
import rebuild_progress
import watcher
import server


# ======================= 修复2：切换回滚（Fake Chroma） =======================

class FakeCollection:
    """单个 Chroma collection 内存模拟（含 rename/delete、$and/$or 过滤、对齐返回）。

    get() 必须按 where 过滤并对齐返回 metadatas/documents/embeddings——
    add_file_chunks 的新代校验（_verify_new_generation）依赖完整对齐结果。
    """

    def __init__(self, name: str, client=None):
        self.name = name
        self._client = client
        self.records: dict[str, dict] = {}

    def count(self) -> int:
        return len(self.records)

    def modify(self, name=None):
        if name and self._client:
            self._client.rename_collection(self.name, name)

    def add(self, ids, embeddings, documents, metadatas):
        for i in range(len(ids)):
            if ids[i] in self.records:
                raise ValueError(f"duplicate id: {ids[i]}")
            self.records[ids[i]] = {
                "embedding": list(embeddings[i]),
                "document": documents[i],
                "metadata": dict(metadatas[i]),
            }

    @staticmethod
    def _match(meta: dict, where) -> bool:
        if not where:
            return True
        if "$and" in where:
            return all(FakeCollection._match(meta, w) for w in where["$and"])
        if "$or" in where:
            return any(FakeCollection._match(meta, w) for w in where["$or"])
        return all(meta.get(k) == v for k, v in where.items())

    def get(self, ids=None, where=None, limit=None, include=None):
        rows = [
            (cid, r) for cid, r in self.records.items()
            if (ids is None or cid in ids) and self._match(r["metadata"], where)
        ]
        if limit is not None:
            rows = rows[:limit]
        return {
            "ids": [cid for cid, _ in rows],
            "documents": [r["document"] for _, r in rows],
            "metadatas": [r["metadata"] for _, r in rows],
            "embeddings": [r["embedding"] for _, r in rows],
        }

    def delete(self, ids=None, where=None):
        targets = [
            cid for cid, r in self.records.items()
            if (ids is None or cid in ids) and self._match(r["metadata"], where)
        ]
        for cid in targets:
            self.records.pop(cid, None)


class FakeClient:
    """按名管理集合；fail_rename_to 命中「改名目标」即抛错（精确模拟某一步失败）。

    故障为一次性（瞬态）：第一次命中抛错后自动清除——回滚改名（同样以正式名
    为目标）必须放行，才能验证「第二步失败 → obsolete 改回正式名」的回滚路径。
    """

    def __init__(self, fail_rename_to: str | None = None):
        self.collections: dict[str, FakeCollection] = {}
        self.fail_rename_to = fail_rename_to

    def list_collections(self):
        return list(self.collections)

    def get_or_create_collection(self, name, metadata=None):
        if name not in self.collections:
            self.collections[name] = FakeCollection(name, self)
        return self.collections[name]

    def get_collection(self, name):
        return self.collections[name]

    def delete_collection(self, name):
        self.collections.pop(name, None)

    def rename_collection(self, old: str, new: str):
        if self.fail_rename_to is not None and new == self.fail_rename_to:
            self.fail_rename_to = None  # 瞬态故障：仅首次命中失败
            raise RuntimeError(f"simulated transient rename failure {old} -> {new}")
        col = self.collections.pop(old, None)
        if col is None:
            raise KeyError(old)
        col.name = new
        self.collections[new] = col


class SwitchRollbackTestBase(unittest.TestCase):
    """公共环境：独立注册表 DB + FakeClient 双集合（旧数据已写入 active）。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        generation_store.reset_for_tests(self._tmp / "gen.db")
        self._reset_vs_globals()
        self.addCleanup(self._reset_vs_globals)
        self.addCleanup(lambda: generation_store.reset_for_tests(None))
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))

    def _reset_vs_globals(self):
        vs._client = None
        vs._collection = None
        vs._image_collection = None
        vs._REBUILD_TARGETS.clear()
        for k in list(vs._REGISTERED_COLLECTIONS.keys()):
            vs._REGISTERED_COLLECTIONS.pop(k, None)

    def _setup_client(self, fail_rename_to: str | None = None) -> FakeClient:
        client = FakeClient(fail_rename_to=fail_rename_to)
        self._patcher = patch.object(vs, "_get_client", return_value=client)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        # 预建 active 双集合（模拟正常运行态）
        vs.get_or_create_collection(vs.CHROMA_COLLECTION)
        vs.get_or_create_collection(vs.IMAGE_COLLECTION)
        old = client.collections[vs.CHROMA_COLLECTION]
        old.add(["a::0"], [[0.1, 0.2]], ["旧数据"], [{"source_path": "a"}])
        return client


@unittest.skip("C1 已改为目录级 generation 路由，不再支持 collection rename 切换")
class TestCollectionSwitchRollback(SwitchRollbackTestBase):
    """修复2：双 rename 第二步失败 → 回滚，正式集合不消失。"""

    def test_second_rename_failure_rolls_back_to_active(self):
        client = self._setup_client(fail_rename_to=vs.CHROMA_COLLECTION)
        assert vs.begin_rebuild()["ok"]
        with self.assertRaises(vs._CollectionSwitchError):
            vs._apply_collection_switch("text", vs.CHROMA_COLLECTION)
        # 回滚成功：正式集合仍在且是旧数据；obsolete 已被改回正式名
        self.assertIn(vs.CHROMA_COLLECTION, client.collections)
        self.assertNotIn(f"{vs.CHROMA_COLLECTION}__obsolete", client.collections)
        self.assertEqual(
            len(client.collections[vs.CHROMA_COLLECTION].records), 1,
            "回滚后正式集合必须保有旧数据",
        )

    def test_first_rename_failure_keeps_active_untouched(self):
        client = self._setup_client(
            fail_rename_to=f"{vs.CHROMA_COLLECTION}__obsolete"
        )
        assert vs.begin_rebuild()["ok"]
        with self.assertRaises(vs._CollectionSwitchError):
            vs._apply_collection_switch("text", vs.CHROMA_COLLECTION)
        # 第一步失败：正式集合未被改动，数据原样
        self.assertIn(vs.CHROMA_COLLECTION, client.collections)
        self.assertEqual(len(client.collections[vs.CHROMA_COLLECTION].records), 1)

    def test_commit_rebuild_captures_switch_failure(self):
        """commit 层：切换失败不抛异常、返回 not ok（调用方可 abort 清理）。"""
        self._setup_client(fail_rename_to=vs.CHROMA_COLLECTION)
        assert vs.begin_rebuild()["ok"]
        result = vs.commit_rebuild()
        self.assertFalse(result["ok"])
        self.assertTrue(
            str(result.get("error", "")).startswith("switch_failed"),
            f"error 应以 switch_failed 开头: {result.get('error')}",
        )
        self.assertEqual(vs.rebuild_status(), vs.REBUILD_STATE_FULL,
                         "切换失败后状态保持 rebuilding，供 abort_rebuild 清理")

    def test_commit_rebuild_success_still_works(self):
        """回滚逻辑不破坏正常提交路径。"""
        client = self._setup_client()
        assert vs.begin_rebuild()["ok"]
        # 重建期间写入新数据
        vs.add_file_chunks(
            "a", "text", ["新数据"], [[0.3, 0.4]],
            {"file_name": "a", "content_hash": "h2"},
        )
        result = vs.commit_rebuild()
        self.assertTrue(result["ok"], result)
        self.assertIn(vs.CHROMA_COLLECTION, client.collections)
        self.assertEqual(len(client.collections[vs.CHROMA_COLLECTION].records), 1)
        self.assertEqual(vs.rebuild_status(), vs.REBUILD_STATE_IDLE)


# ======================= 修复1：迁移延迟提交 =======================

class TestMigrationDeferredCommit(unittest.TestCase):
    """schema 迁移：全量扫描（后台任务排空）后才 commit；超时/失败 abort。"""

    def setUp(self):
        self._commit = patch.object(server, "commit_rebuild")
        self._abort = patch.object(server, "abort_rebuild")
        self.commit = self._commit.start()
        self.abort = self._abort.start()
        self.addCleanup(self._commit.stop)
        self.addCleanup(self._abort.stop)

    def _run(self, job_state="done", time_values=None, commit_result=None, scan_result=None):
        self.commit.return_value = commit_result or {"ok": True}
        scan_result = scan_result or {
            "candidates": [r"/x/a.md"], "already_pending": [],
        }
        time_patch = (
            patch("time.time", side_effect=time_values)
            if time_values is not None
            else patch("time.time", return_value=0)
        )
        with patch.object(watcher, "get_job", return_value={"state": job_state}), \
             patch("time.sleep"), \
             time_patch:
            server._bg_schema_migration_commit(scan_result)

    def test_commits_only_after_all_manifest_items_done(self):
        """本轮强制扫描清单均 done，才允许提交。"""
        self._run()
        self.commit.assert_called_once()
        self.abort.assert_not_called()

    def test_timeout_aborts_keeping_old_collection(self):
        """超时（任务一直未完成）→ abort，旧集合保留。"""
        counter = iter(range(0, 100000))  # time.time 递增，必然越过 deadline
        self._run(job_state="processing", time_values=lambda: next(counter))
        self.abort.assert_called_once()
        self.commit.assert_not_called()

    def test_failed_manifest_item_aborts(self):
        self._run(job_state="failed")
        self.commit.assert_not_called()
        self.abort.assert_called_once()

    def test_pending_old_job_aborts(self):
        self._run(scan_result={"candidates": [r"/x/a.md"], "already_pending": [r"/x/a.md"]})
        self.commit.assert_not_called()
        self.abort.assert_called_once()

    def test_commit_failure_aborts(self):
        self._run(commit_result={"ok": False, "error": "x"})
        self.commit.assert_called_once()
        self.abort.assert_called_once()

    def test_start_services_does_not_commit_immediately(self):
        """启动路径：needs_migration 时只 begin，绝不立即 commit（修复1 核心）。"""
        def _reset_services():
            server._SERVICES_STARTED = False
            server._WATCHER_OBSERVER = None
            watcher.reset_rebuild_barrier_for_tests()
        _reset_services()
        self.addCleanup(_reset_services)
        with patch.object(server, "needs_migration", return_value=True), \
             patch.object(server, "begin_rebuild", return_value={"ok": True}) as begin, \
             patch.object(server, "_load_lan_config"), \
             patch.object(server, "scan_existing", return_value={"candidates": [], "already_pending": []}) as scan, \
             patch.object(server, "start_watcher", return_value=MagicMock()), \
             patch.object(server, "_bg_warmup"), \
             patch.object(server, "MEMORY_IMPORT_AUTO_SYNC", False), \
             patch.object(server.tokenmanager_sync, "run_forever"), \
             patch.object(server, "_idle_unload_loop"), \
             patch.object(server.wiki_store, "start_maintenance_loop"), \
             patch.object(server.context_snapshot, "start_snapshot_loop"), \
             patch.object(server, "INTEGRITY_PATROL_ENABLED", False), \
             patch.object(server, "_bg_schema_migration_commit") as migrate:
            server._start_background_services()
            # 迁移线程异步启动，轮询等待其进入（不 commit）
            import time as _poll
            deadline = _poll.time() + 2
            while _poll.time() < deadline and not migrate.called:
                _poll.sleep(0.01)
        begin.assert_called_once()
        self.assertTrue(scan.call_args.kwargs["force"])
        self.assertTrue(scan.call_args.kwargs["rebuild_session"])
        self.commit.assert_not_called()
        self.assertEqual(migrate.call_args.args[0], {"candidates": [], "already_pending": []})
        self.assertTrue(migrate.call_args.args[1])


# ======================= 修复3：/api/reindex 严格 abort =======================

class TestReindexStrictAbort(unittest.TestCase):
    """/api/reindex：超时 / 任一任务失败 → abort 保留旧索引。"""

    def setUp(self):
        vs.reset_index_health()
        self._reset_server_services()
        self.addCleanup(self._reset_server_services)
        patchers = [
            patch.object(server, "begin_rebuild", return_value={"ok": True}),
            patch.object(server, "commit_rebuild", return_value={"ok": True}),
            patch.object(server, "abort_rebuild"),
            patch.object(server, "get_stats", return_value={"documents": 0}),
            patch.object(server, "rebuild_status", return_value="idle"),
            patch.object(
                watcher, "scan_existing",
                return_value={"total": 1, "skipped": 0,
                              "submitted": [r"/x/a.md"], "already_pending": []},
            ),
            patch.object(watcher, "get_job", return_value={"state": "done"}),
            patch("time.sleep"),
        ]
        # cleanup 必须停 patcher 本身（start() 返回的是 mock，stop() 无效果，
        # 补丁会泄漏到同进程后续测试——scan_existing/get_job 等）
        self.mocks = [p.start() for p in patchers]
        self.addCleanup(lambda: [p.stop() for p in patchers])
        self.begin, self.commit, self.abort = self.mocks[0], self.mocks[1], self.mocks[2]
        self.get_job = self.mocks[6]

    def _reset_server_services(self):
        server._SERVICES_STARTED = False
        server._WATCHER_OBSERVER = None
        watcher.reset_rebuild_barrier_for_tests()

    def test_reindex_commits_when_all_done(self):
        result = server.reindex()
        self.assertNotIn("error", result)
        self.commit.assert_called_once()
        self.abort.assert_not_called()

    def test_reindex_timeout_aborts(self):
        """超时（任务一直 queued）→ abort，绝不 commit 半成品。"""
        self.get_job.return_value = {"state": "queued"}
        counter = iter(range(0, 100000))
        with patch("time.time", side_effect=lambda: next(counter)):
            result = server.reindex()
        self.abort.assert_called_once()
        self.commit.assert_not_called()
        self.assertTrue(str(result["error"]).startswith("rebuild_timeout"),
                        f"error 应以 rebuild_timeout 开头: {result['error']}")

    def test_reindex_validating_job_must_finish_before_commit(self):
        """validating 不是终态，不能在该窗口切换 rebuild 集合。"""
        self.get_job.return_value = {"state": "validating"}
        counter = iter(range(0, 100000))
        with patch("time.time", side_effect=lambda: next(counter)):
            result = server.reindex()
        self.abort.assert_called_once()
        self.commit.assert_not_called()
        self.assertTrue(str(result["error"]).startswith("rebuild_timeout"))

    def test_reindex_failed_job_aborts_with_detail(self):
        """任一材料失败 → abort 并返回失败明细（含材料级完整性校验失败）。"""
        self.get_job.return_value = {"state": "failed", "error": "新代校验失败"}
        result = server.reindex()
        self.abort.assert_called_once()
        self.commit.assert_not_called()
        self.assertEqual(result["error"], "rebuild_failed:1_materials")
        self.assertEqual(
            result["failed"],
            [{"path": "/x/a.md", "state": "failed", "error": "新代校验失败"}],
        )

    def test_reindex_conflicting_old_job_aborts(self):
        self.mocks[5].return_value = {
            "total": 1, "skipped": 0, "candidates": [r"/x/a.md"],
            "submitted": [], "already_pending": [r"/x/a.md"],
        }
        result = server.reindex()
        self.abort.assert_called_once()
        self.commit.assert_not_called()
        self.assertEqual(result["error"], "rebuild_conflict:1_pending")

    def test_reindex_commit_failure_aborts(self):
        self.commit.return_value = {"ok": False, "error": "switch_failed:x"}
        result = server.reindex()
        self.abort.assert_called_once()
        self.assertEqual(result["error"], "switch_failed:x")


# ======================= 修复5：lifespan 统一后台服务 =======================

class TestLifespanUnifiedServices(unittest.TestCase):
    """uvicorn server:app 启动路径同样启动 watcher/后台服务；幂等 + 停止。"""

    def setUp(self):
        self._reset()
        self.addCleanup(self._reset)

    def _reset(self):
        server._SERVICES_STARTED = False
        server._WATCHER_OBSERVER = None

    def _patch_services(self):
        return [
            patch.object(server, "_load_lan_config"),
            patch.object(server, "needs_migration", return_value=False),
            patch.object(server, "start_watcher", return_value=self.fake_observer),
            patch.object(server, "_bg_warmup"),
            patch.object(server, "MEMORY_IMPORT_AUTO_SYNC", False),
            patch.object(server.tokenmanager_sync, "run_forever"),
            patch.object(server, "_idle_unload_loop"),
            patch.object(server.wiki_store, "start_maintenance_loop"),
            patch.object(server.context_snapshot, "start_snapshot_loop"),
            patch.object(server, "INTEGRITY_PATROL_ENABLED", False),
        ]

    def test_start_services_idempotent_and_stops_watcher(self):
        self.fake_observer = MagicMock()
        # 注意区分 patcher 与 start() 返回值：带 new 值的 patch（如
        # MEMORY_IMPORT_AUTO_SYNC=False）start() 返回 new 本身而非 MagicMock。
        patchers = self._patch_services()
        mocks = [p.start() for p in patchers]
        # 线程池 shutdown 也打桩——不能把 watcher/_INDEX_POOL 等真实池关掉，
        # 否则同进程后续测试的 submit 会 RuntimeError
        pool_patchers = [
            patch.object(watcher, "shutdown_pool"),
            patch.object(server.wiki_store, "shutdown_pool"),
            patch("mindos.derived.shutdown_pool"),
            patch("vector_store.active_operations", return_value=0),
            patch("vector_store.release_chroma", return_value=True),
            patch.object(server.memory_store, "release_memory_collection"),
        ]
        pool_mocks = [p.start() for p in pool_patchers]
        self.addCleanup(lambda: [p.stop() for p in patchers])
        self.addCleanup(lambda: [p.stop() for p in pool_patchers])
        start_watcher = mocks[2]

        server._start_background_services()
        server._start_background_services()  # 幂等：不重复启动

        start_watcher.assert_called_once()
        self.assertIs(server._WATCHER_OBSERVER, self.fake_observer)
        self.assertTrue(server._SERVICES_STARTED)

        server._stop_background_services()
        self.fake_observer.stop.assert_called_once()
        self.assertIsNone(server._WATCHER_OBSERVER)
        pool_mocks[4].assert_called_once()
        pool_mocks[5].assert_called_once()

    def test_lifespan_runs_services_for_asgi_startup(self):
        """纯 ASGI 启动（uvicorn server:app）：lifespan 内启动后台服务。"""
        self.fake_observer = MagicMock()
        with patch.object(server, "_detect_worker_count", return_value=1), \
             patch.object(server, "_acquire_instance_lock_once", return_value=(True, None)), \
             patch.object(server, "_run_startup_health_check"), \
             patch.object(server, "_start_background_services") as start_svc, \
             patch.object(server, "_stop_background_services") as stop_svc:
            async def run():
                async with server._lifespan(server.app):
                    start_svc.assert_called_once()
                    stop_svc.assert_not_called()
            asyncio.run(run())
        stop_svc.assert_called_once()


# ======================= 修复6：完整性巡检入口 =======================

class TestIntegrityPatrol(unittest.TestCase):
    """_verify_and_requeue_incomplete：损坏源主动重建，健康源不动。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))

    def _touch(self, name: str) -> str:
        p = self._tmp / name
        p.write_text("x", encoding="utf-8")
        return str(p)

    def test_requeues_integrity_failed_and_read_error(self):
        ok_src = self._touch("ok.md")
        bad_src = self._touch("bad.md")
        err_src = self._touch("err.md")
        gone_src = str(self._tmp / "gone.md")  # 不存在：跳过重建

        def verify(src, **kwargs):
            return {
                ok_src: vs.VERIFY_OK,
                bad_src: vs.VERIFY_INTEGRITY_FAILED,
                err_src: vs.VERIFY_READ_ERROR,
                gone_src: vs.VERIFY_INTEGRITY_FAILED,
            }[src]

        with patch.object(vs, "list_all_documents",
                          return_value=[{"id": p} for p in (ok_src, bad_src, err_src, gone_src)]), \
             patch.object(vs, "verify_source_index", side_effect=verify), \
             patch.object(server, "submit_index", return_value=True) as submit:
            result = server._verify_and_requeue_incomplete()

        self.assertEqual(result["checked"], 4)
        self.assertEqual(result["ok"], 1)
        self.assertEqual(sorted(result["requeued"]), sorted([bad_src, err_src]))
        self.assertEqual(len(result["incomplete"]), 3)  # bad + err + gone
        submitted_paths = [c.args[0] for c in submit.call_args_list]
        self.assertEqual(sorted(submitted_paths), sorted([bad_src, err_src]))
        for call in submit.call_args_list:
            self.assertTrue(call.kwargs.get("force"),
                            "损坏源重建必须 force=True（内容未变也需重建）")

    def test_enumeration_failure_returns_empty_stats(self):
        with patch.object(vs, "list_all_documents", side_effect=RuntimeError("db locked")):
            result = server._verify_and_requeue_incomplete()
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["requeued"], [])


# ======================= scan_existing 提交清单（修复3 前置） =======================

class TestScanExistingManifest(unittest.TestCase):
    """scan_existing 返回本轮提交清单，供 /api/reindex 等待终态。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        for name in ("a.md", "b.md"):
            (self._tmp / name).write_text("内容", encoding="utf-8")

    def _scan(self, submit_returns, source_hash=None):
        with patch.object(watcher, "WATCH_FOLDER", str(self._tmp)), \
             patch.object(watcher, "_index_fingerprint", return_value="fp"), \
             patch.object(watcher, "get_source_hash", return_value=source_hash), \
             patch.object(watcher, "submit_index", return_value=submit_returns) as submit:
            result = watcher.scan_existing()
        return result, submit

    def test_submitted_manifest(self):
        result, submit = self._scan(True, source_hash=None)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(result["submitted"]), 2)
        self.assertEqual(result["already_pending"], [])
        self.assertEqual(submit.call_count, 2)

    def test_already_pending_tracked(self):
        """已在队列的任务被去重跳过 → 计入 already_pending（等待时同样要等它）。"""
        result, submit = self._scan(False, source_hash=None)
        self.assertEqual(result["submitted"], [])
        self.assertEqual(len(result["already_pending"]), 2)

    def test_unchanged_files_skipped(self):
        result, submit = self._scan(True, source_hash="fp")
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["submitted"], [])
        submit.assert_not_called()

    def test_force_scan_rebuilds_all_files(self):
        with patch.object(watcher, "WATCH_FOLDER", str(self._tmp)), \
             patch.object(watcher, "get_source_hash") as source_hash, \
             patch.object(watcher, "submit_index", return_value=True) as submit:
            result = watcher.scan_existing(force=True)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(result["candidates"]), 2)
        source_hash.assert_not_called()
        self.assertEqual(submit.call_count, 2)
        for call in submit.call_args_list:
            self.assertTrue(call.kwargs["force"])


class TestRebuildSubmissionBarrier(unittest.TestCase):
    """重建期间的 Watcher 事件只能在切换后回放，不能混入 __rebuild。"""

    def setUp(self):
        watcher.reset_rebuild_barrier_for_tests()
        self.addCleanup(watcher.reset_rebuild_barrier_for_tests)

    def test_external_submission_is_deferred_then_replayed(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        path = root / "later.md"
        path.write_text("later", encoding="utf-8")
        with patch.object(watcher._INDEX_POOL, "submit") as pool_submit:
            self.assertTrue(watcher.begin_rebuild_barrier("session-1")["ok"])
            self.assertTrue(watcher.submit_index(str(path)))
            pool_submit.assert_not_called()
            finished = watcher.finish_rebuild_barrier("session-1")
        self.assertEqual(finished["replayed"], [str(path.absolute())])
        pool_submit.assert_called_once()


class TestRebuildProgressPersistence(unittest.TestCase):
    """中断恢复进度必须保留每个材料终态，不能被同 session 的 manifest 更新清空。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        rebuild_progress.reset_for_tests(self._tmp / "rebuild_progress.db")
        self.addCleanup(lambda: rebuild_progress.reset_for_tests(None))
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))

    def test_resume_start_preserves_terminal_state(self):
        session = "resume-session"
        rebuild_progress.start(session, "api-reindex", {"/x/a.md": "fp-a"})
        rebuild_progress.set_path_state(session, "/x/a.md", "done")

        # 重启续跑以同一 session 重新登记当前 manifest，旧的 done 不能被清空。
        rebuild_progress.start(session, "api-reindex", {"/x/a.md": "fp-a", "/x/b.md": "fp-b"})
        active = rebuild_progress.active()
        self.assertEqual(active["states"], {"/x/a.md": "done"})
        self.assertEqual(active["manifest"], {"/x/a.md": "fp-a", "/x/b.md": "fp-b"})

    def test_manifest_submission_is_allowed_during_barrier(self):
        with patch.object(watcher._INDEX_POOL, "submit") as pool_submit:
            watcher.begin_rebuild_barrier("session-2")
            self.assertTrue(
                watcher.submit_index(r"C:\\tmp\\manifest.md", force=True, rebuild_session="session-2")
            )
        pool_submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
