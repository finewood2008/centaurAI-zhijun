"""P0-4 索引任务失败恢复与可观测状态专项测试。

覆盖方案 §P0-4 + §7.1/§7.2 的可观测面：
- 状态机 queued -> processing -> validating -> done（validating 为写入后、终态确认之间）。
- 失败落盘稳定错误码（error_code），不导出完整异常原文。
- 任务终态记录 old_index_preserved（旧索引是否仍可读）。
- 失败时旧索引保留、可检索（§7.1）。
"""
import unittest
from unittest.mock import MagicMock, patch

import watcher
from watcher import (
    ERRCODE_EMPTY,
    ERRCODE_UNKNOWN,
    JOB_STATE_DONE,
    JOB_STATE_FAILED,
    JOB_STATE_PROCESSING,
    JOB_STATE_VALIDATING,
    _ERRCODE_MSG,
    _index_error_code,
)
from mindos.stores import job_store
from parser import EmptyFileError


def _reset_jobs():
    with watcher._JOBS_LOCK:
        watcher._JOBS.clear()


class ErrorCodeMappingTests(unittest.TestCase):
    def test_unknown_exception_maps_to_unknown(self):
        self.assertEqual(_index_error_code(RuntimeError("boom")), ERRCODE_UNKNOWN)

    def test_empty_error_maps_to_empty(self):
        self.assertEqual(_index_error_code(EmptyFileError("空")), ERRCODE_EMPTY)


class ValidatingStateTests(unittest.TestCase):
    def setUp(self):
        _reset_jobs()
        self.pt = patch("vector_store.get_source_hash", return_value=None)
        self.pt.start()

    def tearDown(self):
        self.pt.stop()
        _reset_jobs()

    def test_success_goes_through_validating_then_done(self):
        """成功路径：processing -> validating -> done，走完状态机。"""
        with (
            patch.object(watcher, "index_file", return_value=True),
            patch.object(watcher, "_wait_file_stable"),
        ):
            watcher._run_index_job("/x/a.md", submit_wiki=False)

        job = watcher.get_job("/x/a.md")
        self.assertEqual(job["state"], JOB_STATE_DONE)
        self.assertEqual(job.get("finished_at") is not None, True)

    def test_validating_marked_after_write_before_final(self):
        """validating 在写入成功、done 落盘前短暂出现。"""
        import sys
        from unittest.mock import Mock

        states = []
        fake_wiki = Mock()
        fake_wiki.submit_source.side_effect = lambda *a, **kw: states.append(
            watcher.get_job("/x/b.md")["state"]
        )

        with (
            patch.object(watcher, "index_file", return_value=True),
            patch.object(watcher, "_wait_file_stable"),
            patch.dict(sys.modules, {"wiki_store": fake_wiki}),
        ):
            watcher._run_index_job("/x/b.md")

        self.assertTrue(any(s == JOB_STATE_VALIDATING for s in states))
        self.assertEqual(watcher.get_job("/x/b.md")["state"], JOB_STATE_DONE)

    def test_failure_with_exception_sets_failed_with_stable_error(self):
        """异常失败：stable error code，不含异常原文；旧索引保留可读。"""
        with (
            patch.object(watcher, "index_file", side_effect=RuntimeError("内部token: secret")),
            patch.object(watcher, "get_source_hash", return_value="h1"),
            patch.object(watcher.logger, "error"),
        ):
            watcher._run_index_job("/x/c.md")

        job = watcher.get_job("/x/c.md")
        self.assertEqual(job["state"], JOB_STATE_FAILED)
        # 落盘 error 为稳定文案，不含 "secret"
        self.assertNotIn("secret", job.get("error", ""))
        self.assertEqual(job["error"], _ERRCODE_MSG[ERRCODE_UNKNOWN])
        self.assertEqual(job["old_index_preserved"], True)

    def test_fail_false_return_records_old_index_preserved_absent(self):
        """index_file 返回 False 且旧索引不存在：old_index_preserved=False。"""
        with (
            patch.object(watcher, "index_file", return_value=False),
            patch.object(watcher, "get_source_hash", return_value=None),
        ):
            watcher._run_index_job("/x/d.md")

        job = watcher.get_job("/x/d.md")
        self.assertEqual(job["state"], JOB_STATE_FAILED)
        self.assertEqual(job["old_index_preserved"], False)


class PersistedOutcomeTests(unittest.TestCase):
    """终态落盘：old_index_preserved 持久化并可恢复。"""

    def setUp(self):
        job_store.reset_for_tests()
        _reset_jobs()

    def tearDown(self):
        _reset_jobs()

    def test_save_and_read_index_outcome_persists_preserved(self):
        store = job_store.JobStore.instance()
        store.save_index_outcome("/x/e.md", "failed", "稳定文案",
                                 old_index_preserved=True)
        rec = store.index_outcome("/x/e.md")
        self.assertEqual(rec["state"], "failed")
        self.assertEqual(rec["error"], "稳定文案")
        self.assertTrue(rec["old_index_preserved"])

    def test_restored_job_includes_preserved_flag(self):
        from mindos.services import ingestion

        store = job_store.JobStore.instance()
        store.save_index_outcome("/x/f.md", "failed", "稳定文案",
                                 old_index_preserved=True)
        restored = ingestion._restored_job("/x/f.md")
        self.assertEqual(restored["state"], "failed")
        self.assertTrue(restored.get("old_index_preserved"))


if __name__ == "__main__":
    unittest.main()