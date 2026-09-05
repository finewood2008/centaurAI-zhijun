"""Safe source-file delivery tests for app and authenticated LAN proxies."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.responses import FileResponse

import server


class FileAccessTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "watch"
        self.root.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_serves_supported_file_with_requested_disposition(self):
        source = self.root / "quarterly report.pdf"
        source.write_bytes(b"report")

        with patch.object(server, "WATCH_FOLDER", str(self.root)):
            response = server.get_file(str(source), "attachment")

        self.assertIsInstance(response, FileResponse)
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn("quarterly%20report.pdf", response.headers["content-disposition"])

    def test_rejects_path_outside_watch_folder(self):
        outside = Path(self.tempdir.name) / "secret.pdf"
        outside.write_bytes(b"secret")

        with patch.object(server, "WATCH_FOLDER", str(self.root)):
            with self.assertRaises(HTTPException) as raised:
                server.get_file(str(outside), "inline")

        self.assertEqual(raised.exception.status_code, 403)

    def test_rejects_unsupported_file_and_invalid_disposition(self):
        unsupported = self.root / "payload.exe"
        unsupported.write_bytes(b"nope")
        supported = self.root / "notes.md"
        supported.write_text("hello", encoding="utf-8")

        with patch.object(server, "WATCH_FOLDER", str(self.root)):
            with self.assertRaises(HTTPException) as unsupported_error:
                server.get_file(str(unsupported), "inline")
            with self.assertRaises(HTTPException) as disposition_error:
                server.get_file(str(supported), "preview")

        self.assertEqual(unsupported_error.exception.status_code, 404)
        self.assertEqual(disposition_error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
