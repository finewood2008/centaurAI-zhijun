"""P1-1 Watcher 稳定判断 + 事件延迟合并 专项测试。

覆盖方案 §P1-1 可编码部分：
- 稳定判断：同时采样大小 + 修改时间 + 内容指纹前缀，连续多次一致才判稳定；
  （原实现只看大小）内容覆写/半截写入会被识别。
- 延迟合并：去重窗口不丢最终事件——窗口内只保留一个待处理标记，
  窗口结束后重新检查并提交最后版本；删除事件取消待处理标记。
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import watcher


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    # 保证 mtime 变化可被采样识别（尤其同一内容重写时）
    os.utime(path, (time.time() + 1, time.time() + 1))


class StabilitySignatureTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "a.txt"

    def test_signature_none_for_missing_or_empty_file(self):
        self.assertIsNone(watcher._file_stability_signature(str(self.path)))
        _write(self.path, b"")
        self.assertIsNone(watcher._file_stability_signature(str(self.path)))

    def test_signature_includes_size_mtime_and_content_prefix(self):
        _write(self.path, b"hello world")
        sig = watcher._file_stability_signature(str(self.path))
        self.assertIsNotNone(sig)
        size, mtime, prefix = sig
        self.assertEqual(size, 11)
        self.assertEqual(
            prefix,
            __import__("hashlib").sha1(b"hello world").hexdigest(),
        )
        # mtime 是正整数浮点
        self.assertGreater(mtime, 0)

    def test_signature_changes_when_content_rewritten_same_size(self):
        # 关键场景：大小+mtime 不足以区分时，内容前缀指纹要能识别覆写。
        data = b"A" * 4096
        _write(self.path, data)
        sig1 = watcher._file_stability_signature(str(self.path))
        # 同大小、但内容不同（覆写原字节）：字符串尾字不同
        data2 = b"B" * 4096
        _write(self.path, data2)
        sig2 = watcher._file_stability_signature(str(self.path))
        self.assertNotEqual(sig1, sig2)
        self.assertEqual(sig1[0], sig2[0])  # 大小一致


class ControlledShutdownTests(unittest.TestCase):
    def test_shutdown_waits_for_running_index_work_and_cancels_only_queued(self):
        pool = MagicMock()
        previous = watcher._INDEX_POOL_STOPPING
        try:
            watcher._INDEX_POOL_STOPPING = False
            with patch.object(watcher, "_INDEX_POOL", pool):
                watcher.shutdown_pool()
            self.assertTrue(watcher._INDEX_POOL_STOPPING)
            pool.shutdown.assert_called_once_with(wait=True, cancel_futures=True)
        finally:
            watcher._INDEX_POOL_STOPPING = previous


class WaitFileStableTests(unittest.TestCase):
    """稳定判断：多信号连续一致才通过；变化则重置等待。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "grow.bin"

    def test_returns_quickly_when_already_stable(self):
        _write(self.path, b"x" * 512)
        watcher._STABLE_SAMPLES_BAK = watcher._STABLE_SAMPLES
        watcher._STABLE_SAMPLES = 2
        try:
            with patch.object(watcher, "time") as t:
                watcher._wait_file_stable(str(self.path), timeout=30, interval=0.5)
            # 连续 2 次一致即通过，远未用满 timeout=30
            self.assertLessEqual(t.sleep.call_count, 3)
        finally:
            watcher._STABLE_SAMPLES = watcher._STABLE_SAMPLES_BAK

    def test_resets_when_content_keeps_changing(self):
        # 文件持续被改写：每次采样指纹都不同 → 稳定计数永远归零 → 跑满 timeout
        watcher._STABLE_SAMPLES_BAK = watcher._STABLE_SAMPLES
        watcher._STABLE_SAMPLES = 3
        counter = {"n": 0}

        def flaky(_path):
            counter["n"] += 1
            return (100 + counter["n"], 1.0, f"h{counter['n']}")

        try:
            with patch.object(watcher, "_file_stability_signature", side_effect=flaky), \
                 patch.object(watcher, "time") as t:
                watcher._wait_file_stable(str(self.path), timeout=2, interval=0.5)
            # timeout=2/interval=0.5 → 4 次采样均不稳定，跑满
            self.assertGreaterEqual(t.sleep.call_count, 4)
        finally:
            watcher._STABLE_SAMPLES = watcher._STABLE_SAMPLES_BAK


class DebounceEventTests(unittest.TestCase):
    """延迟合并去重：窗口内只保留一个待处理标记，不丢最终事件。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "doc.txt"
        _write(self.path, b"hello")
        watcher._PENDING_SWEEPER_STARTED = True  # 禁止测试中真的起线程
        watcher._pending_index.clear()
        self.handler = watcher.DocumentHandler()

    def _handle(self):
        self.handler._handle(str(self.path), "modified")

    def test_first_event_arms_single_marker(self):
        self._handle()
        with watcher._PENDING_LOCK:
            self.assertIn(str(self.path), watcher._pending_index)

    def test_second_event_within_window_does_not_reset_deadline(self):
        self._handle()
        with watcher._PENDING_LOCK:
            due0 = watcher._pending_index[str(self.path)]
        # 窗口内再次触发：不丢事件，但仅保留一个待处理标记（截止不推后）
        self._handle()
        with watcher._PENDING_LOCK:
            self.assertEqual(watcher._pending_index[str(self.path)], due0)
        # 只有一条待处理
        self.assertEqual(len(watcher._pending_index), 1)

    def test_submit_pending_event_skips_deleted_and_submits_existing(self):
        with patch.object(watcher, "submit_index") as sub:
            watcher._submit_pending_event(str(self.path))
            sub.assert_called_once_with(str(self.path))
        with patch.object(watcher, "submit_index") as sub:
            watcher._submit_pending_event(str(self.path) + ".nonexistent")
            sub.assert_not_called()

    def test_deletion_cancels_pending_marker(self):
        self._handle()
        with watcher._PENDING_LOCK:
            self.assertIn(str(self.path), watcher._pending_index)
        with patch.object(watcher, "delete_file") as deleter:
            self.handler._handle_deletion(str(self.path))
            deleter.assert_called_once()
        with watcher._PENDING_LOCK:
            self.assertNotIn(str(self.path), watcher._pending_index)


if __name__ == "__main__":
    unittest.main()
