"""MindOS 导入校验边界测试（P1 / P13 开放音频 / 本期开放 Excel 与 PPT）。

覆盖：50MB 边界（恰好允许 / 超出拒绝）、200MB 音频边界、扩展名大小写、
音频开放、Excel（.xlsx/.xlsm/.xls）与 PPT（.pptx）开放、
视频/压缩包/旧版二进制格式（.ppt/.doc）拒绝。仅依赖 mindos.validation，可独立于 server 运行。
"""
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos.validation import (
    validate_import,
    DOC_IMAGE_MAX_BYTES,
    AUDIO_MAX_BYTES,
    OK,
    OVERSIZE,
    UNSUPPORTED,
)


class ImportValidationTests(unittest.TestCase):
    def assert_status(self, filename, size, expected):
        result = validate_import(filename, size)
        self.assertEqual(result["status"], expected, f"{filename}@{size} -> {result}")

    # ---- 50MB 边界（文档/图片）----
    def test_exactly_50mb_allowed(self):
        self.assert_status("plan.pdf", DOC_IMAGE_MAX_BYTES, OK)
        self.assert_status("scan.png", DOC_IMAGE_MAX_BYTES, OK)

    def test_over_50mb_rejected(self):
        self.assert_status("plan.pdf", DOC_IMAGE_MAX_BYTES + 1, OVERSIZE)
        self.assert_status("scan.png", DOC_IMAGE_MAX_BYTES + 1, OVERSIZE)

    # ---- 200MB 边界（音频）----
    def test_audio_within_200mb_allowed(self):
        self.assert_status("a.mp3", AUDIO_MAX_BYTES, OK)
        self.assert_status("a.wav", AUDIO_MAX_BYTES, OK)
        self.assert_status("a.m4a", 1024, OK)

    def test_audio_over_200mb_rejected(self):
        self.assert_status("a.mp3", AUDIO_MAX_BYTES + 1, OVERSIZE)

    # ---- 大小写扩展名 ----
    def test_extension_case_insensitive(self):
        self.assert_status("PLAN.PDF", 1024, OK)
        self.assert_status("Scan.PNG", 1024, OK)
        self.assert_status("Interview.M4A", 1024, OK)
        self.assert_status("Movie.MP4", 1024, UNSUPPORTED)

    # ---- 支持的文档/图片/音频 ----
    def test_supported_documents_images_audio(self):
        for name in ("a.pdf", "a.docx", "a.txt", "a.md", "a.png", "a.jpg", "a.jpeg"):
            self.assert_status(name, 1024, OK)
        # 本期开放：Excel（.xlsx/.xlsm/.xls）与 PPT（.pptx），归 document 类
        for name in ("a.xlsx", "a.xlsm", "a.xls", "a.pptx"):
            self.assert_status(name, 1024, OK)
        for name in ("a.mp3", "a.wav", "a.m4a"):
            self.assert_status(name, 1024, OK)

    # ---- 不支持类型 ----
    def test_unsupported_types(self):
        # 注意：.ppt 为旧版二进制格式，解析器不支持，本期不开放
        for name in ("a.mp4", "a.zip", "a.ppt", "a.doc", "a.gif", "a.webp"):
            self.assert_status(name, 1024, UNSUPPORTED)

    # ---- 无扩展名 ----
    def test_no_extension_rejected(self):
        self.assert_status("README", 1024, UNSUPPORTED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
