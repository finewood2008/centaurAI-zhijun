"""MP3 音频转写在未安装系统 ffmpeg 时的回归测试。"""
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import video
import watcher


class PyAVAudioFallbackTests(unittest.TestCase):
    def test_audio_decode_available_accepts_pyav_without_ffmpeg(self):
        with patch.object(video, "ffmpeg_available", return_value=False), patch.object(
            video, "_pyav_available", return_value=True
        ):
            self.assertTrue(video.audio_decode_available())

    def test_extract_audio_uses_pyav_when_ffmpeg_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "input.wav"
            output = Path(temp_dir) / "output.wav"
            with wave.open(str(source), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(b"\x00\x00" * 800)

            with patch.object(video, "ffmpeg_available", return_value=False):
                result = video.extract_audio_16k_mono(str(source), str(output))

            self.assertEqual(result, str(output))
            with wave.open(str(output), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertEqual(wav.getframerate(), 16000)
                self.assertGreater(wav.getnframes(), 0)

    def test_audio_indexing_does_not_require_system_ffmpeg_when_pyav_is_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "sample.mp3"
            source.write_bytes(b"placeholder")
            wav = Path(temp_dir) / "audio_16k.wav"
            wav.write_bytes(b"placeholder")
            metadata = {"content_hash": "abc123", "file_name": "sample.mp3"}
            with patch.object(video, "audio_decode_available", return_value=True), patch.object(
                watcher, "whisper_available", return_value=True
            ), patch.object(video, "probe", return_value={"has_audio": True, "duration": 1.0}), patch.object(
                video, "extract_audio_16k_mono", return_value=str(wav)
            ) as extract_audio, patch.object(
                watcher, "transcribe_audio", return_value=[{"start": 0.0, "end": 1.0, "text": "测试音频"}]
            ), patch.object(watcher, "embed_batch_texts", return_value=[[0.1, 0.2]]), patch.object(
                watcher, "add_file_chunks", return_value=True
            ), patch.object(watcher, "_submit_material_summary"), patch.object(
                watcher, "_submit_material_analysis"
            ), patch.object(watcher, "VIDEO_WORK_DIR", temp_dir):
                self.assertTrue(watcher._index_audio(str(source), str(source), metadata))
            extract_audio.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
