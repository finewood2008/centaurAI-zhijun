"""资源监控采样（P2 §8）。

能力探测而不是平台假设：
- CPU / 内存：跨平台系统 API（posix 读 /proc，Windows 用 ctypes GetSystemTimes /
  GlobalMemoryStatusEx），不依赖 psutil；
- NVIDIA GPU：`nvidia-smi` 短超时探测；缺失 / 超时 / 解析失败返回 `available=false` 与简短原因，
  不伪造 NVIDIA 指标；
- Ollama：经已配置本地 Ollama HTTP 探测，不扫描磁盘目录；
- 采样缓存：最短间隔 2 秒，多个 Web 客户端共享，避免页面轮询放大为子进程风暴。

每一项均以 `{available, stale, errorCode?, value?}` 表达，使前端可区分「数值为 0」、
「尚未采样」「能力不可用」「暂时失败」。GPU 探测失败不影响 CPU/内存/Ollama/索引服务。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

_CACHE_MIN_INTERVAL_SECONDS = 2.0
_GPU_TIMEOUT_SECONDS = 1.5
_CACHE_LOCK = threading.Lock()
_CACHE = {}  # {sampled_at_epoch, snapshot}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cap(text: str, limit: int = 200) -> str:
    """截断错误/说明文本，避免把路径或长异常全文带出。"""
    text = (text or "").strip().replace("\n", " ")
    return text[:limit]


# =====================================================================
# CPU
# =====================================================================

_CPU_PREV = {"idle": None, "busy": None, "at": None}


def sample_cpu() -> dict:
    """整体 CPU 占用率（0-100）。首采返回 available=true 但 value=None、stale=true。"""
    try:
        if os.name == "nt":
            idle, busy = _cpu_windows()
        else:
            idle, busy = _cpu_posix()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "value": None, "errorCode": _cap(str(type(exc).__name__))}
    now = time.time()
    prev = _CPU_PREV
    prev_idle, prev_busy, prev_at = prev["idle"], prev["busy"], prev["at"]
    _CPU_PREV["idle"], _CPU_PREV["busy"], _CPU_PREV["at"] = idle, busy, now
    if prev_at is None or prev_busy is None:
        return {"available": True, "value": None, "stale": True}
    d_idle = idle - prev_idle
    d_busy = busy - prev_busy
    d_total = d_idle + d_busy
    if d_total <= 0:
        return {"available": True, "value": 0.0}
    return {"available": True, "value": round(100.0 * d_busy / d_total, 1)}


def _cpu_posix() -> tuple[int, int]:
    """读 /proc/stat 首行 cpu 合计与 idle。返回 (idle, busy)。"""
    with open("/proc/stat", "r", encoding="utf-8") as f:
        fields = f.readline().split()
    if not fields or fields[0] != "cpu":
        raise RuntimeError("unable_to_read /proc/stat")
    nums = [int(x) for x in fields[1:]]
    # user nice system idle iowait irq softirq steal
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    busy = sum(nums) - idle
    return idle, busy


def _cpu_windows() -> tuple[int, int]:
    """用 GetSystemTimes 计算 (idle, busy) 累计滴答（FILETIME 即 100ns 单位）。"""
    import ctypes

    class _FT(ctypes.Structure):
        _fields_ = [("hi", ctypes.c_ulong), ("lo", ctypes.c_ulong)]

    def to_int(ft: _FT) -> int:
        return (ft.hi << 32) | ft.lo

    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    idle, kernel, user = _FT(), _FT(), _FT()
    if not k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise RuntimeError("GetSystemTimes_failed")
    tt_idle = to_int(idle)
    tt_kernel = to_int(kernel)
    tt_user = to_int(user)
    # kernel 已含 idle；busy = user + (kernel - idle)。
    return tt_idle, tt_user + tt_kernel - tt_idle


# =====================================================================
# 内存
# =====================================================================


def sample_memory() -> dict:
    """系统内存：total/available（字节）。"""
    try:
        total, available = _memory_total_available()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "errorCode": _cap(str(type(exc).__name__))}
    return {
        "available": True,
        "totalBytes": total,
        "availableBytes": available,
        "usedPercent": None if total <= 0 else round(100.0 * (total - available) / total, 1),
    }


def _memory_total_available() -> tuple[int, int]:
    if os.name == "nt":
        import ctypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        ms = _MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):  # type: ignore[attr-defined]
            raise RuntimeError("GlobalMemoryStatusEx_failed")
        return ms.ullTotalPhys, ms.ullAvailPhys
    with open("/proc/meminfo", "r", encoding="utf-8") as f:
        data = {}
        for line in f:
            parts = line.split(":")
            if len(parts) == 2:
                data[parts[0].strip()] = parts[1].strip()
    total = int(data.get("MemTotal", "0").split()[0]) * 1024
    avail = data.get("MemAvailable")
    if avail is not None:
        avail = int(avail.split()[0]) * 1024
    else:
        free = int(data.get("MemFree", "0").split()[0]) * 1024
        buffers = int(data.get("Buffers", "0").split()[0]) * 1024
        cached = int(data.get("Cached", "0").split()[0]) * 1024
        avail = free + buffers + cached
    return total, avail


# =====================================================================
# NVIDIA GPU
# =====================================================================


def sample_gpu() -> dict:
    """nvidia-smi 探测；缺失 / 超时 / 解析失败返回 available=false，不伪造指标。"""
    exe = _find_nvidia_smi()
    if exe is None:
        return {
            "available": False,
            "errorCode": "nvidia_smi_not_found",
            "errorMessageSafe": "未检测到 NVIDIA 工具",
        }
    try:
        proc = subprocess.run(
            [exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total", "--format=csv,json,noheader,nounits"],
            capture_output=True, text=True, timeout=_GPU_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"available": False, "errorCode": "timeout", "errorMessageSafe": "nvidia-smi 超时"}
    except OSError as exc:
        return {"available": False, "errorCode": _cap(str(type(exc).__name__))}
    if proc.returncode != 0:
        return {"available": False, "errorCode": "nvidia_smi_error", "errorMessageSafe": _cap(proc.stderr)}
    try:
        payload = json.loads(proc.stdout or "[]")
    except ValueError:
        return {"available": False, "errorCode": "parse_error", "errorMessageSafe": "nvidia-smi 输出解析失败"}
    gpu = payload[0] if isinstance(payload, list) and payload else (payload if isinstance(payload, dict) else None)
    if gpu is None:
        return {"available": False, "errorCode": "no_gpu", "errorMessageSafe": "未查到 GPU"}
    try:
        gpu_mem_total = int(gpu.get("memory.total", 0) or 0)
        gpu_mem_used = int(gpu.get("memory.used", 0) or 0)
        utilization = int(gpu.get("utilization.gpu", 0) or 0)
    except (ValueError, TypeError):
        utilization, gpu_mem_total, gpu_mem_used = 0, 0, 0
    return {
        "available": True,
        "name": _cap(str(gpu.get("name", ""))),
        "utilizationPercent": utilization,
        "memoryUsedBytes": gpu_mem_used * 1024 * 1024,
        "memoryTotalBytes": gpu_mem_total * 1024 * 1024,
    }


def _find_nvidia_smi() -> str | None:
    path = shutil.which("nvidia-smi")
    return path or None


# =====================================================================
# Ollama
# =====================================================================


def sample_ollama(local) -> dict:
    """经已配置本地 Ollama 探测连通性、版本与运行模型数。失败返回 available=false。"""
    try:
        from . import ollama_client

        v = ollama_client.version(local)
        running = len(ollama_client.ps(local).get("models", []))
        installed = len(ollama_client.tags(local).get("models", []))
        return {
            "available": True,
            "reachable": True,
            "version": v.get("version"),
            "runningCount": running,
            "installedCount": installed,
        }
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None):
            code = f"http_{getattr(exc, 'code')}"
        elif isinstance(exc, TimeoutError):
            code = "timeout"
        elif isinstance(exc, OSError):
            code = "connection"
        else:
            code = "parse_error"
        return {"available": False, "reachable": False, "errorCode": code}


# =====================================================================
# 聚合采样缓存
# =====================================================================


def get_snapshot(local=None) -> dict:
    """返回聚合采样快照；距上次采样 < 2s 时直接复用缓存（§8 采样缓存）。

    `local` 为可选的 Material/local Ollama 快照；传入时才探测 Ollama 部分。
    """
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get("snapshot")
        if cached and now - cached["sampledAtEpoch"] < _CACHE_MIN_INTERVAL_SECONDS:
            return cached["value"]
    cpu = sample_cpu()
    memory = sample_memory()
    gpu = sample_gpu()
    ollama = sample_ollama(local) if local is not None else {
        "available": False, "reachable": False, "errorCode": "not_configured"
    }
    value = {
        "sampledAt": _now_iso(),
        "cpu": cpu,
        "memory": memory,
        "gpu": gpu,
        "ollama": ollama,
    }
    with _CACHE_LOCK:
        _CACHE["snapshot"] = {"sampledAtEpoch": now, "value": value}
    return value


def clear_cache() -> None:
    """测试隔离：清空采样缓存。"""
    global _CPU_PREV
    with _CACHE_LOCK:
        _CACHE.clear()
    _CPU_PREV = {"idle": None, "busy": None, "at": None}
