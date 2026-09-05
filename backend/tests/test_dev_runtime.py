"""Disposable HTTP children only; never start the application or touch its data."""
import importlib.util
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev_runtime.py"
spec = importlib.util.spec_from_file_location("dev_runtime", SCRIPT)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zhijun-runtime-test-")
        self.root = Path(self.temporary.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.app = runtime.Runtime(self.root, {"backend": (self.port, "/", "start-backend.sh")})
        self.write_server()
        self.external = []

    def tearDown(self):
        try:
            self.app.stop(timeout=4)
        finally:
            for process in self.external:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=4)
            self.temporary.cleanup()

    def write_server(self):
        command = [sys.executable, "-m", "http.server", str(self.port), "--bind", "127.0.0.1"]
        (self.root / "start-backend.sh").write_text("#!/bin/bash\nexec " + shlex.join(command) + "\n")

    def unowned(self, cwd=None):
        process = subprocess.Popen([sys.executable, "-m", "http.server", str(self.port), "--bind", "127.0.0.1"],
            cwd=cwd or self.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.external.append(process)
        until = time.monotonic() + 4
        while not runtime.healthy(self.port, "/") and time.monotonic() < until:
            time.sleep(.05)
        self.assertTrue(runtime.healthy(self.port, "/"))
        return process

    def test_start_twice_reuses_pid_status_and_stop(self):
        first = self.app.start(timeout=5)["backend"]
        self.assertTrue(first["healthy"])
        self.assertTrue(first["managed"])
        second = self.app.start(timeout=5)["backend"]
        self.assertEqual(first["pid"], second["pid"])
        self.assertTrue((self.app.folder / "backend.log").is_file())
        self.assertEqual(self.app.stop(timeout=4)["backend"]["state"], "stopped")
        self.assertEqual(self.app.read(), {})

    def test_existing_project_service_is_reused_but_never_stopped(self):
        process = self.unowned()
        item = self.app.start(timeout=4)["backend"]
        self.assertTrue(item["healthy"])
        self.assertFalse(item["managed"])
        self.app.stop()
        self.assertIsNone(process.poll())

    def test_foreign_listener_is_not_adopted_or_stopped(self):
        process = self.unowned(cwd="/private/tmp")
        with self.assertRaisesRegex(RuntimeError, "端口已有服务"):
            self.app.start(timeout=2)
        self.app.stop()
        self.assertIsNone(process.poll())

    def test_stale_pid_cannot_stop_an_unrelated_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL)
        self.external.append(process)
        with self.app.lock():
            self.app.save({"backend": {"pid": process.pid, "project": str(self.root), "token": "not-an-owned-supervisor"}})
        self.app.stop()
        self.assertIsNone(process.poll())

    def test_failed_start_has_log_and_can_recover(self):
        (self.root / "start-backend.sh").write_text("#!/bin/bash\nprintf 'synthetic startup failure\\n'\nexit 2\n")
        with self.assertRaisesRegex(RuntimeError, "尚未就绪"):
            self.app.start(timeout=2)
        self.assertIn("synthetic startup failure", (self.app.folder / "backend.log").read_text())
        self.write_server()
        self.assertTrue(self.app.start(timeout=5)["backend"]["healthy"])

    def test_concurrent_start_creates_one_supervisor(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.app.start(timeout=5), range(2)))
        self.assertEqual(results[0]["backend"]["pid"], results[1]["backend"]["pid"])


if __name__ == "__main__":
    unittest.main()
