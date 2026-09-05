"""视频媒体处理 — ffmpeg / ffprobe 封装（纯 subprocess，无第三方依赖）。

职责：从任意视频抽出可被现有检索管线消费的两类信号——
  1) 音轨 → 16kHz/mono/pcm wav（喂给 faster-whisper 转写）
  2) 关键帧 → jpg + 精确时间戳（喂给 Chinese-CLIP 视觉嵌入 / 帧 OCR）

设计：损坏/不可读/无对应流的输入以 MediaError 或空结果显式表达，由调用方
（watcher.index_file 的 video 分支）graceful 处理，绝不让异常冒泡炸掉索引管线。
"""
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import wave
from pathlib import Path
from typing import Optional

from config import (
    SCENE_THRESHOLD,
    MAX_FRAMES_PER_VIDEO,
    FRAME_INTERVAL_SEC,
    FRAME_JPEG_QUALITY,
    MIN_SCENE_FRAMES,
    FFPROBE_TIMEOUT,
    FFMPEG_AUDIO_TIMEOUT,
    FFMPEG_FRAME_TIMEOUT,
)

logger = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

# scene 抽帧 metadata sidecar 解析
_PTS_RE = re.compile(r"pts_time:([0-9.]+)")
_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


class MediaError(Exception):
    """损坏 / 不可读 / 不支持的输入，或 ffmpeg 失败（含超时）。"""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _pyav_available() -> bool:
    """PyAV 是音频转写的可选解码回退，不承担视频抽帧能力。"""
    try:
        import av  # noqa: F401
    except ImportError:
        return False
    return True


def audio_decode_available() -> bool:
    """是否具备把音频解码为 Whisper 所需 WAV 的能力。

    优先使用系统 ffmpeg/ffprobe；未安装时，faster-whisper 的 PyAV 依赖仍可
    解码 MP3/M4A/WAV，不能因为缺少视频工具而拒绝整个音频资料。
    """
    return ffmpeg_available() or _pyav_available()


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """硬超时执行；超时抛 MediaError。

    start_new_session 让子进程自成会话/进程组，超时时用 os.killpg 连带杀掉整个组
    （万一 ffmpeg 派生了子进程也一并清掉，不留孤儿）；subprocess.run 的 timeout 只 kill
    直接子进程，故这里用 Popen 自己处理。
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        raise MediaError(f"ffmpeg/ffprobe 不可用: {e}") from e
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise MediaError(f"ffmpeg 超时（{timeout}s）: {' '.join(cmd[:4])} ...") from e
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def probe(path: str, timeout: float = FFPROBE_TIMEOUT) -> dict:
    """单次 ffprobe → {duration, width, height, has_audio, has_video}。

    损坏/不可读（ffprobe 非零退出，如 'moov atom not found'）抛 MediaError。
    """
    if not ffmpeg_available():
        return _probe_with_pyav(path)

    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration:stream=index,codec_type,width,height",
        "-of", "json", path,
    ]
    proc = _run(cmd, timeout)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise MediaError(f"ffprobe 失败: {err or '未知错误'}")
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as e:
        raise MediaError(f"ffprobe 输出解析失败: {e}") from e

    streams = data.get("streams") or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    width = height = None
    for s in streams:
        if s.get("codec_type") == "video":
            width, height = s.get("width"), s.get("height")
            break
    duration = None
    raw_dur = (data.get("format") or {}).get("duration")
    try:
        duration = float(raw_dur) if raw_dur not in (None, "N/A") else None
    except (TypeError, ValueError):
        duration = None

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": has_audio,
        "has_video": has_video,
    }


def _probe_with_pyav(path: str) -> dict:
    """无 ffprobe 时用 PyAV 读取媒体流信息，仅供音频转写回退。"""
    try:
        import av

        with av.open(path) as container:
            streams = list(container.streams)
            audio_stream = next((stream for stream in streams if stream.type == "audio"), None)
            video_stream = next((stream for stream in streams if stream.type == "video"), None)
            duration = None
            if container.duration is not None:
                duration = float(container.duration / av.time_base)
            elif audio_stream is not None and audio_stream.duration is not None:
                duration = float(audio_stream.duration * audio_stream.time_base)
            elif video_stream is not None and video_stream.duration is not None:
                duration = float(video_stream.duration * video_stream.time_base)
            return {
                "duration": duration,
                "width": getattr(video_stream, "width", None),
                "height": getattr(video_stream, "height", None),
                "has_audio": audio_stream is not None,
                "has_video": video_stream is not None,
            }
    except Exception as exc:
        raise MediaError(f"PyAV 读取媒体失败: {exc}") from exc


def extract_audio_16k_mono(
    src: str, out_wav: str, info: Optional[dict] = None,
    timeout: float = FFMPEG_AUDIO_TIMEOUT,
) -> Optional[str]:
    """抽 16kHz/mono/pcm_s16le wav（whisper 友好）。

    无音轨 → 返回 None（调用方跳过转写）。ffmpeg 失败或文件未产出 → MediaError。
    """
    if not ffmpeg_available():
        return _extract_audio_16k_mono_pyav(src, out_wav, info)

    if info is None:
        info = probe(src)
    if not info.get("has_audio"):
        return None

    Path(out_wav).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", src,
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-f", "wav", out_wav,
    ]
    proc = _run(cmd, timeout)
    if proc.returncode != 0 or not Path(out_wav).is_file() or Path(out_wav).stat().st_size == 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise MediaError(f"抽音轨失败: {err or '未产出 wav'}")
    return out_wav


def _extract_audio_16k_mono_pyav(
    src: str, out_wav: str, info: Optional[dict] = None,
) -> Optional[str]:
    """无 ffmpeg 时，用 PyAV 重采样为 16kHz/单声道/16bit WAV。

    PyAV 是 faster-whisper 的既有运行依赖；此回退只覆盖音频解码，视频抽帧仍
    必须依赖 ffmpeg，避免在两条媒体链路中承诺不同的能力。
    """
    if not _pyav_available():
        raise MediaError("ffmpeg/ffprobe 与 PyAV 均不可用")
    if info is None:
        info = _probe_with_pyav(src)
    if not info.get("has_audio"):
        return None
    try:
        import av

        output = Path(out_wav)
        output.parent.mkdir(parents=True, exist_ok=True)
        with av.open(src) as container, wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            resampler = av.audio.resampler.AudioResampler(
                format="s16", layout="mono", rate=16000
            )
            for frame in container.decode(audio=0):
                for resampled in resampler.resample(frame):
                    wav.writeframes(resampled.to_ndarray().tobytes())
            for resampled in resampler.resample(None):
                wav.writeframes(resampled.to_ndarray().tobytes())
        if not output.is_file() or output.stat().st_size <= 44:
            raise MediaError("PyAV 未产出可用 WAV")
        return str(output)
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError(f"PyAV 音频解码失败: {exc}") from exc


def _collect_frames(out_dir: str, pattern: str) -> list[Path]:
    return sorted(Path(out_dir).glob(pattern))


def extract_scene_frames(
    src: str, out_dir: str,
    threshold: float = SCENE_THRESHOLD,
    max_frames: int = MAX_FRAMES_PER_VIDEO,
    quality: int = FRAME_JPEG_QUALITY,
    timeout: float = FFMPEG_FRAME_TIMEOUT,
) -> list[dict]:
    """场景切帧 → [{path, timestamp, score}]，timestamp = 源 pts_time（精确）。

    时间戳从 metadata=print 写出的 sidecar 解析（权威），不抓 showinfo stderr。
    select 过滤后剩的帧仍带原始 PTS，故 pts_time 即源视频时间。
    """
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    sidecar = str(Path(out_dir) / "_scenes.txt")
    out_pat = str(Path(out_dir) / "scene_%04d.jpg")
    vf = f"select='gt(scene,{threshold})',metadata=print:file={sidecar}"
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", src,
        "-vf", vf,
        "-fps_mode", "vfr",
        "-frames:v", str(max_frames),
        "-q:v", str(quality),
        # ffmpeg8 的 mjpeg 编码器对部分 YUV range 默认拒绝输出（"Non full-range YUV is
        # non-standard"），unofficial 放开后正常出 jpg；否则整段 0 帧。
        "-strict", "unofficial",
        out_pat,
    ]
    proc = _run(cmd, timeout)
    files = _collect_frames(out_dir, "scene_*.jpg")
    # 非零退出但已产出部分帧 → 视为可用（截断尾）；零退出且无帧 → 低动态，返回空让调用方回退
    if not files:
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()[:200]
            logger.info(f"场景抽帧无输出（{err or '无场景切换'}）")
        return []

    pts_list: list[float] = []
    score_list: list[float] = []
    try:
        for line in Path(sidecar).read_text("utf-8", "replace").splitlines():
            m = _PTS_RE.search(line)
            if m:
                pts_list.append(float(m.group(1)))
            s = _SCORE_RE.search(line)
            if s:
                score_list.append(float(s.group(1)))
    except OSError:
        pass

    frames = []
    for i, f in enumerate(files):
        ts = pts_list[i] if i < len(pts_list) else None
        sc = score_list[i] if i < len(score_list) else None
        frames.append({"path": str(f), "timestamp": ts, "score": sc})
    return frames


def extract_timed_frames(
    src: str, out_dir: str,
    every_sec: float = FRAME_INTERVAL_SEC,
    max_frames: int = MAX_FRAMES_PER_VIDEO,
    quality: int = FRAME_JPEG_QUALITY,
    timeout: float = FFMPEG_FRAME_TIMEOUT,
) -> list[dict]:
    """定时抽帧 → [{path, timestamp}]，timestamp = index*every_sec（计算）。

    fps 滤镜会重写 PTS，sidecar 时间戳不可靠，故必须按输出帧序号算时间。
    """
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    out_pat = str(Path(out_dir) / "timed_%04d.jpg")
    fps = 1.0 / every_sec if every_sec > 0 else 1.0
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", src,
        "-vf", f"fps={fps}",
        "-frames:v", str(max_frames),
        "-q:v", str(quality),
        "-strict", "unofficial",   # 同场景抽帧：放开 mjpeg 的 YUV range 限制
        "-start_number", "0",
        out_pat,
    ]
    proc = _run(cmd, timeout)
    files = _collect_frames(out_dir, "timed_*.jpg")
    if not files:
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()[:200]
            logger.info(f"定时抽帧无输出（{err or '无帧'}）")
        return []
    frames = []
    for f in files:
        m = re.search(r"timed_(\d+)\.jpg$", f.name)
        idx = int(m.group(1)) if m else 0
        frames.append({"path": str(f), "timestamp": idx * every_sec, "score": None})
    return frames


def _discard_frames(out_dir: str, prefix: str) -> None:
    """删掉被丢弃那一组的帧文件，避免磁盘留下未入库的孤儿 jpg。"""
    for p in Path(out_dir).glob(f"{prefix}*.jpg"):
        try:
            p.unlink()
        except OSError:
            pass


def _timed_interval(info: dict) -> float:
    """定时抽帧间隔：长视频用默认 N 秒；短视频(<10×N)按时长/10 缩小，保证仍能抽到若干帧。

    否则 fps=1/30 对 10s 短片会得 0 帧（test-videos.co.uk 这类短样片就踩了这个坑）。
    """
    dur = info.get("duration") or 0.0
    if dur <= 0:
        return FRAME_INTERVAL_SEC
    return max(1.0, min(FRAME_INTERVAL_SEC, dur / 10.0))


def extract_frames(src: str, out_dir: str, info: dict) -> list[dict]:
    """编排：有视频流 → 场景抽帧；帧数 < MIN_SCENE_FRAMES（低动态）→ 回退定时抽帧。

    无视频流（纯音频容器）→ 返回 []。两组抽帧都会落盘，最终丢弃的那组文件会被清掉。
    """
    if not info.get("has_video"):
        return []
    frames = extract_scene_frames(src, out_dir)
    if len(frames) < MIN_SCENE_FRAMES:
        logger.info(f"场景抽帧仅 {len(frames)} 帧，回退定时抽帧: {src}")
        timed = extract_timed_frames(src, out_dir, every_sec=_timed_interval(info))
        # 回退结果更全则用回退；否则保留已抽的场景帧。丢弃组的落盘文件清掉。
        if len(timed) >= len(frames):
            _discard_frames(out_dir, "scene_")
            frames = timed
        else:
            _discard_frames(out_dir, "timed_")
    return frames
