import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

import parser
import server
import watcher


class EmptyUploadTests(unittest.TestCase):
    def test_empty_upload_is_rejected_without_leaving_a_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            watch_root = Path(temporary) / "watch"
            upload = UploadFile(filename="empty.pdf", file=io.BytesIO(b""))

            with patch.object(server, "WATCH_FOLDER", str(watch_root)):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(server._save_upload(upload, "mobile_uploads"))

            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(raised.exception.detail, "上传文件为空")
            self.assertEqual([path for path in Path(temporary).rglob("*") if path.is_file()], [])

    def test_non_empty_upload_is_atomically_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            watch_root = Path(temporary) / "watch"
            destination_dir = watch_root / "mobile_uploads"
            test_case = self

            class ObservingUpload:
                filename = "notes.txt"

                def __init__(self):
                    self.chunks = [b"content", b""]

                async def read(self, _size):
                    self.assert_final_path_hidden()
                    return self.chunks.pop(0)

                def assert_final_path_hidden(self):
                    published = list(destination_dir.glob("*.txt")) if destination_dir.exists() else []
                    test_case.assertEqual(published, [])

            upload = ObservingUpload()

            with patch.object(server, "WATCH_FOLDER", str(watch_root)):
                dest, safe_name = asyncio.run(server._save_upload(upload, "mobile_uploads"))

            self.assertEqual(safe_name, "notes.txt")
            self.assertEqual(dest.read_bytes(), b"content")
            self.assertEqual(list((watch_root.parent / ".upload_staging").glob("*.uploading")), [])


class EmptyParserTests(unittest.TestCase):
    def test_empty_supported_file_raises_stable_error_before_parser_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "empty.pdf"
            source.touch()

            with patch.object(parser, "_parse_pdf") as parse_pdf:
                with self.assertRaises(parser.EmptyFileError) as raised:
                    parser.parse_file(str(source))

            parse_pdf.assert_not_called()
            self.assertIn("文件为空，无法索引", str(raised.exception))


class EmptyWatcherTests(unittest.TestCase):
    def setUp(self):
        with watcher._JOBS_LOCK:
            self.original_jobs = dict(watcher._JOBS)

    def tearDown(self):
        with watcher._JOBS_LOCK:
            watcher._JOBS.clear()
            watcher._JOBS.update(self.original_jobs)

    def test_scan_existing_skips_empty_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            watch_root = Path(temporary) / "watch"
            watch_root.mkdir()
            empty = watch_root / "empty.pdf"
            empty.touch()
            populated = watch_root / "notes.txt"
            populated.write_text("content", encoding="utf-8")

            with (
                patch.object(watcher, "WATCH_FOLDER", str(watch_root)),
                patch.object(watcher, "_index_fingerprint", return_value=""),
                patch.object(watcher, "submit_index") as submit_index,
                patch.object(watcher.annotations, "get_rag_override", return_value=None),
            ):
                watcher.scan_existing()

            submit_index.assert_called_once_with(str(populated))
            job = watcher.get_job(str(empty))
            self.assertEqual(job["state"], "failed")
            self.assertIn("文件为空，无法索引", job["error"])

    def test_background_job_marks_empty_file_failed_without_error_log(self):
        source = "/tmp/empty.pdf"
        with (
            patch.object(watcher, "_wait_file_stable"),
            patch.object(watcher, "index_file", side_effect=parser.EmptyFileError("文件为空，无法索引")),
            patch.object(watcher.annotations, "get_rag_override", return_value=None),
            patch.object(watcher.logger, "warning") as warning,
            patch.object(watcher.logger, "error") as error,
        ):
            watcher._run_index_job(source)

        job = watcher.get_job(source)
        self.assertEqual(job["state"], "failed")
        # P0-4：EmptyFileError 映射为稳定错误码文案，不再暴露异常原文
        self.assertEqual(job["error"], "文件为空或无可提取内容")
        self.assertEqual(job["error_code"], "empty")
        warning.assert_called_once()
        error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
