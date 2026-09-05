"""P0-1 数据根目录 OS 独占锁测试（索引可靠性方案）。

全部用例通过子进程 + 独立 CENTAURAI_DATABASE_DATA_ROOT 执行，
测试进程自身绝不触碰生产 data 目录的锁文件（方案 P1-4 测试隔离原则）。

覆盖方案 §7.5 验收：
- 同进程二次加锁失败且持有者信息可读（锁外区域）；
- release 后可重新获取；
- 跨进程：后端/另一维护进程持锁时拒绝执行，且拒绝前不打开 ChromaDB；
- 进程退出后 OS 自动释放（stale lock 免疫：文件残留但无进程持锁时可直接接管）；
- config 空闲卸载参数的环境变量覆盖（阶段 A 止血项）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _run_py(code: str, data_root: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "CENTAURAI_DATABASE_DATA_ROOT": str(data_root)}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", _assemble(code)],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# 在子进程中先 sys.path 注入 backend（不依赖 cwd）；dedent 后与 preamble 按行拼接
_PREAMBLE = "import sys; sys.path.insert(0, r'%s')" % str(BACKEND_DIR)


def _assemble(code: str) -> str:
    body = textwrap.dedent(code).strip("\n")
    return _PREAMBLE + "\n" + body


class TestInstanceLock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_second_acquire_rejected_and_holder_readable(self):
        """同进程二次加锁失败；持有者信息（锁外区域）可读且字段完整。"""
        proc = _run_py(
            """
            import json
            from instance_lock import acquire

            lock, _ = acquire(role="test-holder")
            assert lock is not None, "首次加锁必须成功"

            lock2, holder2 = acquire(role="test-second")
            out = {"second_ok": lock2 is not None, "holder": holder2}
            print("RESULT:" + json.dumps(out, ensure_ascii=False))

            lock.release()
            lock3, _ = acquire(role="test-after-release")
            print("REACQUIRE:" + json.dumps({"ok": lock3 is not None}))
            lock3.release()
            """,
            self.data_root,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(next(l for l in proc.stdout.splitlines() if l.startswith("RESULT:"))[7:])
        self.assertFalse(result["second_ok"], "同一进程第二次加锁必须失败")
        holder = result["holder"]
        self.assertIsNotNone(holder, "加锁失败时必须能读到持有者信息")
        self.assertEqual(holder["role"], "test-holder")
        for key in ("pid", "role", "started_at", "data_root"):
            self.assertIn(key, holder, f"持有者信息缺字段 {key}")
        reacquire = json.loads(next(l for l in proc.stdout.splitlines() if l.startswith("REACQUIRE:"))[10:])
        self.assertTrue(reacquire["ok"], "release 后必须可重新加锁")

    def test_cross_process_reject_then_autorelease(self):
        """跨进程：持锁进程运行期间另一进程被拒绝；进程退出后 OS 自动释放。"""
        holder_code = _assemble("""
            import os
            from instance_lock import acquire
            lock, _ = acquire(role="child-backend")
            print(f"LOCKED {os.getpid()}", flush=True)
            import time; time.sleep(5)  # 不主动 release，模拟崩溃后 OS 兜底回收
        """)
        env = {**os.environ, "CENTAURAI_DATABASE_DATA_ROOT": str(self.data_root)}
        child = subprocess.Popen(
            [sys.executable, "-c", holder_code],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # 等子进程确认拿到锁
            deadline = time.monotonic() + 30
            line = ""
            while time.monotonic() < deadline:
                line = child.stdout.readline() if child.stdout else ""
                if "LOCKED" in line:
                    break
                if child.poll() is not None:
                    self.fail(f"持锁子进程提前退出: {child.stderr.read()}")
                time.sleep(0.1)
            self.assertIn("LOCKED", line, "子进程未确认持锁")
            # Windows venv 的 python.exe 可能是 trampoline：真正执行代码（持锁）的
            # 是其子进程，故以子进程自报的 os.getpid() 为准，而不是 Popen().pid
            child_pid = int(line.split()[1])

            # 持锁期间：另一进程必须被拒绝，且能读到子进程的 PID
            proc = _run_py(
                """
                import json
                from instance_lock import acquire
                lock, holder = acquire(role="maintenance")
                print(json.dumps({"ok": lock is not None, "holder": holder}, ensure_ascii=False))
                """,
                self.data_root,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertFalse(data["ok"], "持锁进程运行期间另一进程必须被拒绝")
            self.assertEqual(data["holder"]["pid"], child_pid)
            self.assertEqual(data["holder"]["role"], "child-backend")
        finally:
            child.wait(timeout=60)

        # 子进程退出（未 release）后：新进程可直接接管 —— stale lock 免疫
        proc2 = _run_py(
            """
            import json
            from instance_lock import acquire
            lock, holder = acquire(role="after-exit")
            print(json.dumps({"ok": lock is not None, "holder": holder}, ensure_ascii=False))
            lock.release()
            """,
            self.data_root,
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        data2 = json.loads(proc2.stdout.strip().splitlines()[-1])
        self.assertTrue(data2["ok"], "进程退出后必须能重新加锁（OS 自动释放，文件残留无害）")
        self.assertIsNone(data2["holder"])

    def test_lock_file_created_under_data_root(self):
        """锁文件必须落在数据根目录下（随 CENTAURAI_DATABASE_DATA_ROOT 解析）。"""
        proc = _run_py(
            """
            from instance_lock import acquire, lock_path
            lock, _ = acquire(role="path-check")
            print(lock_path())
            lock.release()
            """,
            self.data_root,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = Path(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(path.parent, self.data_root.resolve())
        self.assertTrue(path.exists())


class TestLifespanLock(unittest.TestCase):
    """修复1验收：单实例锁随 FastAPI lifespan 获取/释放（覆盖 uvicorn server:app）。

    此前锁只在 main() 中获取，直接以 ASGI 方式启动（uvicorn server:app）会
    完全绕过单实例互斥；现在 lifespan startup 加锁、shutdown 释放，多 worker
    直接拒绝。健康自检打桩，聚焦锁行为本身。
    """

    _ENTER_LIFESPAN = """
        import asyncio, json
        import server
        server._run_startup_health_check = lambda: None

        async def main():
            try:
                async with server._lifespan(server.app):
                    print("INSIDE:不应到达")
            except RuntimeError as e:
                print("ERR:" + json.dumps({"msg": str(e)}, ensure_ascii=False))

        asyncio.run(main())
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lifespan_holds_lock_and_releases_on_shutdown(self):
        """lifespan startup 持锁（期间其他进程被拒）、shutdown 释放（可立即接管）。"""
        proc = _run_py(
            """
            import asyncio, json
            import server
            server._run_startup_health_check = lambda: None

            async def main():
                async with server._lifespan(server.app):
                    import instance_lock
                    lock, holder = instance_lock.acquire(role="intruder")
                    print("INSIDE:" + json.dumps(
                        {"intruder_ok": lock is not None, "holder": holder},
                        ensure_ascii=False))
                import instance_lock as il
                lock2, _ = il.acquire(role="after-shutdown")
                print("AFTER:" + json.dumps({"ok": lock2 is not None}))
                if lock2 is not None:
                    lock2.release()

            asyncio.run(main())
            """,
            self.data_root,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inside = json.loads(
            next(l for l in proc.stdout.splitlines() if l.startswith("INSIDE:"))[7:]
        )
        self.assertFalse(inside["intruder_ok"],
                         "lifespan 启动后必须持锁，同数据根的其他进程被拒绝")
        self.assertIsNotNone(inside["holder"])
        self.assertEqual(inside["holder"]["role"], "backend")
        after = json.loads(
            next(l for l in proc.stdout.splitlines() if l.startswith("AFTER:"))[6:]
        )
        self.assertTrue(after["ok"], "lifespan shutdown 后锁必须释放，后续进程可立即接管")

    def test_lifespan_rejects_multi_worker(self):
        """WEB_CONCURRENCY>1（gunicorn/uvicorn --workers）→ 拒绝启动。"""
        proc = _run_py(self._ENTER_LIFESPAN, self.data_root,
                       extra_env={"WEB_CONCURRENCY": "4"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        err = json.loads(
            next(l for l in proc.stdout.splitlines() if l.startswith("ERR:"))[4:]
        )
        self.assertIn("多 worker", err["msg"])

    def test_lifespan_rejects_when_another_process_holds_lock(self):
        """其他进程持锁时 ASGI 启动被拒，且错误信息含持锁者提示。"""
        holder_code = _assemble("""
            import os
            from instance_lock import acquire
            lock, _ = acquire(role="other-backend")
            print(f"LOCKED {os.getpid()}", flush=True)
            import time; time.sleep(5)
        """)
        env = {**os.environ, "CENTAURAI_DATABASE_DATA_ROOT": str(self.data_root)}
        child = subprocess.Popen(
            [sys.executable, "-c", holder_code],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 30
            line = ""
            while time.monotonic() < deadline:
                line = child.stdout.readline() if child.stdout else ""
                if "LOCKED" in line:
                    break
                if child.poll() is not None:
                    self.fail(f"持锁子进程提前退出: {child.stderr.read()}")
                time.sleep(0.1)
            self.assertIn("LOCKED", line)

            proc = _run_py(self._ENTER_LIFESPAN, self.data_root)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            err = json.loads(
                next(l for l in proc.stdout.splitlines() if l.startswith("ERR:"))[4:]
            )
            self.assertIn("数据目录已被其他进程占用", err["msg"])
            self.assertIn("other-backend", err["msg"], "拒绝原因必须包含持锁者信息")
        finally:
            child.wait(timeout=60)


class TestIdleUnloadEnvOverride(unittest.TestCase):
    """阶段 A 止血项：VDB_IDLE_UNLOAD_MINUTES / _CHECK_SECONDS 环境变量覆盖。"""

    def _read_config(self, extra_env: dict[str, str]) -> tuple[int, int]:
        proc = _run_py(
            """
            import config
            print(config.IDLE_UNLOAD_MINUTES, config.IDLE_UNLOAD_CHECK_SECONDS)
            """,
            Path(tempfile.mkdtemp()),
            extra_env=extra_env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        a, b = proc.stdout.strip().splitlines()[-1].split()
        return int(a), int(b)

    def test_defaults(self):
        minutes, seconds = self._read_config({})
        self.assertEqual((minutes, seconds), (30, 60))

    def test_env_override(self):
        minutes, seconds = self._read_config({"VDB_IDLE_UNLOAD_MINUTES": "0"})
        self.assertEqual(minutes, 0, "VDB_IDLE_UNLOAD_MINUTES=0 必须生效（关闭空闲卸载）")
        self.assertEqual(seconds, 60)

    def test_invalid_falls_back(self):
        minutes, seconds = self._read_config({"VDB_IDLE_UNLOAD_MINUTES": "abc"})
        self.assertEqual(minutes, 30, "非法值必须回落默认 30")

    def test_check_seconds_floor(self):
        _, seconds = self._read_config({"VDB_IDLE_UNLOAD_CHECK_SECONDS": "1"})
        self.assertEqual(seconds, 10, "巡检间隔下限 10 秒，防止忙轮询")


if __name__ == "__main__":
    unittest.main()
