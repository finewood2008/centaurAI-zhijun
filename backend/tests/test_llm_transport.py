"""模型服务 HTTP 传输单元测试。"""
from __future__ import annotations

import http.server
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos import llm_transport
from mindos.stores.runtime_settings_store import reset_for_tests


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/echo":
            data = (self.headers.get("Host", "") or "").encode("utf-8")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:  # noqa: D102
        pass


class TransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.store = reset_for_tests(str(Path(cls._tmp.name) / "rt.db"))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def _url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_allowed_request_connects(self) -> None:
        resp = llm_transport.allowed_urlopen(
            self._url("/"), channel="material", store=self.store, timeout=5
        )
        self.assertEqual(resp.read(), b"ok")

    def test_post_with_body(self) -> None:
        resp = llm_transport.allowed_urlopen(
            self._url("/"),
            channel="material",
            store=self.store,
            timeout=5,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
