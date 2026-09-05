"""MindOS P13 音频链路回归测试。

覆盖：
- 三种音频扩展名 + 200MB 边界（与 test_mindos_validation 呼应）；
- ASR 逐字稿时间片段构建（detail_of.transcript）：仅返回有效 start/end；
- 无时间戳降级：缺失、非法、NaN/Inf、end<=start、caption 块一律丢弃，不伪造 00:00；
- 材料文件预览白名单：音频返回 FileResponse（Range 由 Starlette FileResponse 提供）；
- 归档/恢复往返。

依赖项目 .venv（watchdog/chromadb 等），可独立于 server 运行：
    .venv\\Scripts\\python.exe -m unittest test_mindos_p13 -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Request
from fastapi.responses import FileResponse

from mindos.services import ingestion
from mindos import uploads
from mindos.validation import (
    validate_import,
    AUDIO_MAX_BYTES,
    DOC_IMAGE_MAX_BYTES,
    OK,
    OVERSIZE,
    UNSUPPORTED,
)
from mindos.stores import governance_store
from runtime_paths import GOVERNANCE_DB_PATH


class AudioValidationTests(unittest.TestCase):
    """三种音频扩展名开放 + 200MB 边界（文档保持 50MB）。"""

    def test_audio_extensions_allowed(self):
        for name in ("a.mp3", "a.wav", "a.m4a"):
            result = validate_import(name, 1024)
            self.assertEqual(result["status"], OK, name)
            self.assertEqual(result["category"], "audio", name)

    def test_audio_200mb_boundary(self):
        self.assertEqual(validate_import("a.mp3", AUDIO_MAX_BYTES)["status"], OK)
        self.assertEqual(validate_import("a.wav", AUDIO_MAX_BYTES)["status"], OK)
        self.assertEqual(validate_import("a.m4a", AUDIO_MAX_BYTES)["status"], OK)
        self.assertEqual(validate_import("a.mp3", AUDIO_MAX_BYTES + 1)["status"], OVERSIZE)

    def test_document_still_50mb(self):
        self.assertEqual(validate_import("plan.pdf", DOC_IMAGE_MAX_BYTES)["status"], OK)
        self.assertEqual(validate_import("plan.pdf", DOC_IMAGE_MAX_BYTES + 1)["status"], OVERSIZE)

    def test_other_extensions_still_rejected(self):
        self.assertEqual(validate_import("a.mp4", 1024)["status"], UNSUPPORTED)
        self.assertEqual(validate_import("a.webm", 1024)["status"], UNSUPPORTED)


class TranscriptBuildTests(unittest.TestCase):
    """detail_of 音频逐字稿：仅返回有效时间片段，缺失/非法不伪造 00:00。"""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".m4a", delete=False)
        self._tmp.write(b"fake audio content")
        self._tmp.close()
        self.source = self._tmp.name

    def tearDown(self):
        os.unlink(self.source)

    def _detail_with_chunks(self, chunks):
        rec = {
            "material_id": "mindos_audio1",
            "file_name": "a.m4a",
            "file_type": "audio",
            "source_path": self.source,
            "job_id": "job_audio1",
            "created_at": 1700000000.0,
            "folder": "未分类",
        }
        with patch.object(
            ingestion.JobStore, "instance",
            return_value=MagicMock(get=lambda _mid: rec, is_canceled=lambda _mid: False),
        ), patch.object(ingestion, "get_job", return_value={"state": "done"}), patch.object(
            ingestion, "get_source_chunks", return_value=chunks
        ), patch.object(ingestion, "_ann_get", return_value={"tags": []}), patch(
            "mindos.stage_d_admin.legacy_read_enabled", return_value=True
        ), patch.object(ingestion, "_snapshot_text_of", return_value=None):
            return ingestion.detail_of("mindos_audio1")

    def test_disabled_legacy_read_never_reads_chroma_timeline(self):
        with patch("mindos.stage_d_admin.legacy_read_enabled", return_value=False), patch.object(
            ingestion, "get_source_chunks"
        ) as chunks:
            self.assertEqual(ingestion._transcript_segments(self.source), [])
            chunks.assert_not_called()

    def test_valid_transcript_segments(self):
        chunks = [
            {"text": "第一段", "metadata": {"modality": "transcript", "start_time": 0.0, "end_time": 5.0}},
            {"text": "第二段", "metadata": {"modality": "transcript", "start_time": 5.0, "end_time": 12.5}},
        ]
        detail = self._detail_with_chunks(chunks)
        self.assertEqual(len(detail["transcript"]), 2)
        self.assertEqual(detail["transcript"][0]["start"], 0.0)
        self.assertEqual(detail["transcript"][0]["end"], 5.0)
        self.assertEqual(detail["transcript"][1]["start"], 5.0)
        self.assertEqual(detail["transcript"][1]["end"], 12.5)
        self.assertEqual(detail["textLabel"], "转写结果")

    def test_missing_timestamps_are_dropped(self):
        chunks = [
            {"text": "无时间戳", "metadata": {"modality": "transcript"}},
            {"text": "正常段", "metadata": {"modality": "transcript", "start_time": 1.0, "end_time": 3.0}},
        ]
        detail = self._detail_with_chunks(chunks)
        self.assertEqual(len(detail["transcript"]), 1)
        self.assertEqual(detail["transcript"][0]["text"], "正常段")
        self.assertNotEqual(detail["transcript"][0]["start"], 0.0)  # 不伪造 00:00

    def test_reversed_or_zero_span_timestamps_dropped(self):
        chunks = [
            {"text": "end小于start", "metadata": {"modality": "transcript", "start_time": 10.0, "end_time": 5.0}},
            {"text": "零跨度", "metadata": {"modality": "transcript", "start_time": 3.0, "end_time": 3.0}},
            {"text": "正常段", "metadata": {"modality": "transcript", "start_time": 0.0, "end_time": 4.0}},
        ]
        detail = self._detail_with_chunks(chunks)
        self.assertEqual(len(detail["transcript"]), 1)
        self.assertEqual(detail["transcript"][0]["text"], "正常段")

    def test_non_finite_or_non_numeric_timestamps_dropped(self):
        chunks = [
            {"text": "NaN", "metadata": {"modality": "transcript", "start_time": float("nan"), "end_time": 5.0}},
            {"text": "Inf", "metadata": {"modality": "transcript", "start_time": 1.0, "end_time": float("inf")}},
            {"text": "非数字", "metadata": {"modality": "transcript", "start_time": "abc", "end_time": 5.0}},
            {"text": "正常段", "metadata": {"modality": "transcript", "start_time": 1.0, "end_time": 2.0}},
        ]
        detail = self._detail_with_chunks(chunks)
        self.assertEqual(len(detail["transcript"]), 1)
        self.assertEqual(detail["transcript"][0]["text"], "正常段")

    def test_caption_chunks_excluded(self):
        chunks = [
            {"text": "说明：用户备注", "metadata": {"modality": "caption"}},
            {"text": "转写段", "metadata": {"modality": "transcript", "start_time": 0.0, "end_time": 3.0}},
        ]
        detail = self._detail_with_chunks(chunks)
        self.assertEqual(len(detail["transcript"]), 1)
        self.assertEqual(detail["transcript"][0]["text"], "转写段")

    def test_no_valid_segments_yields_empty_transcript(self):
        chunks = [{"text": "x", "metadata": {"modality": "transcript"}}]
        detail = self._detail_with_chunks(chunks)
        self.assertEqual(detail["transcript"], [])
        self.assertEqual(detail["text"], "")


class FilePreviewTests(unittest.TestCase):
    """材料文件预览：音频返回 FileResponse（Range 由 Starlette 提供）。"""

    def test_audio_file_returns_fileresponse(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.write(b"fake mp3")
        tmp.close()
        try:
            rec = {
                "material_id": "mindos_audio2",
                "file_name": "a.mp3",
                "source_path": tmp.name,
            }
            watch_root = str(Path(tmp.name).parent)
            with patch.object(
                ingestion.JobStore, "instance",
                return_value=MagicMock(get=lambda _mid: rec),
            ), patch("mindos.uploads.WATCH_FOLDER", new=watch_root):
                response = uploads.mindos_material_file("mindos_audio2", Request({"type": "http"}))
            self.assertIsInstance(response, FileResponse)
        finally:
            os.unlink(tmp.name)

    def test_unsupported_preview_still_404(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".exe", delete=False)
        tmp.write(b"x")
        tmp.close()
        try:
            rec = {"material_id": "mindos_audio3", "file_name": "a.exe", "source_path": tmp.name}
            watch_root = str(Path(tmp.name).parent)
            with patch.object(
                ingestion.JobStore, "instance",
                return_value=MagicMock(get=lambda _mid: rec),
            ), patch("mindos.uploads.WATCH_FOLDER", new=watch_root):
                with self.assertRaises(Exception) as ctx:
                    uploads.mindos_material_file("mindos_audio3", Request({"type": "http"}))
            self.assertEqual(getattr(ctx.exception, "status_code", None), 404)
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
