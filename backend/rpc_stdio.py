"""桌面端 P2P stdio JSON-RPC 通道。

渲染进程 → Electron 主进程(ipcRenderer.invoke) → 后端子进程(stdin/stdout) → FastAPI app。

设计要点：
- 通过 `httpx.ASGITransport` 直接派发到 FastAPI ASGI app，复用全部现有路由/中间件/权限，
  等价于一次本机 HTTP 请求，但不占用 / 不依赖固定 TCP 端口（真正点对点）。
- 协议：stdin/stdout 各一行一个 UTF-8 JSON 帧（每条换行结尾）。
    请求  {"id", "method"(GET/POST/...), "uri"("/api/...?query"), "headers": {..},
            "body": <原始请求体字符串，可空>, "form": {"k":"v",..}, "file": {name,filename,type,base64}}
    响应  {"id", "status"(http 码), "headers"{content-type}, "body"(响应体字符串, 可空)}
      或  {"id", "error": {"message": "..."}}  （通道层异常，非 HTTP 层错误）
- 普通日志一律写到 stderr（uvicorn 默认即 stderr），不污染 stdout 协议流；即使有个别
  进程内 print 误写 stdout，Electron 端会丢弃无法解析为 JSON 的行，具备韧性。
- 进程内只做请求/响应同步往返。服务端 → 渲染端的主动推送（进度/事件）由既有轮询承担，
  不在本通道内实现，避免复杂化首版。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import sys
import threading
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# 单请求超时上限：qa/视频/Wiki 等重操作可能耗时较长，给足余量。
_DEFAULT_TIMEOUT = httpx.Timeout(600.0)


def _emit(obj: dict) -> None:
    """把一帧 JSON 写入 stdout（协议通道）。"""
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):  # stdout 已关闭/被替换 → 通道失效，静默退出
        pass


def _decode_request(raw: str) -> Optional[dict]:
    """解析一帧请求；非法行返回 None（上层忽略）。"""
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _build_request(req: dict) -> httpx.Request | None:
    """把 JSON-RPC 帧构造成 httpx.Request。缺 id 或 uri 的帧直接丢弃。"""
    req_id = req.get("id")
    uri = req.get("uri")
    if req_id is None or not isinstance(uri, str):
        return None
    method = str(req.get("method") or "GET").upper()
    headers = {k: str(v) for k, v in (req.get("headers") or {}).items()}
    body: bytes | None = None
    data: dict[str, Any] | None = None
    files = None

    # 可选 multipart：一个主文件 + 其它表单字段（与 body 互斥）
    file_info = req.get("file")
    if isinstance(file_info, dict) and file_info.get("base64"):
        try:
            file_bytes = base64.b64decode(file_info["base64"], validate=True)
        except (ValueError, TypeError):
            file_bytes = b""
        files = [
            (
                file_info.get("name") or "file",
                (
                    file_info.get("filename") or "upload.bin",
                    io.BytesIO(file_bytes),
                    file_info.get("type") or "application/octet-stream",
                ),
            )
        ]
        form = req.get("form") or {}
        data = {str(k): str(v) for k, v in form.items()}
    else:
        raw_body = req.get("body")
        if isinstance(raw_body, bytes):
            body = raw_body
        elif isinstance(raw_body, str) and raw_body != "":
            body = raw_body.encode("utf-8")

    # httpx 需要绝对 URL（cookie 处理）；P2P 通道内 host 无意义，统一用固定虚拟 origin，
    # ASGITransport 按 path 路由到 app，不影响任何业务。
    url = uri
    if not url.startswith(("http://", "https://")):
        url = "http://centaurai-p2p.local" + url

    return httpx.Request(
        method, url, headers=headers, content=body, data=data, files=files
    )


async def _dispatch(app, request: httpx.Request) -> dict:
    """经 ASGITransport 把请求打到 app，返回 (status, headers, body) 帧数据。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), timeout=_DEFAULT_TIMEOUT
    ) as client:
        try:
            resp = await client.send(request)
        except Exception as exc:  # 通道层异常，与 HTTP 层错误区分
            logger.exception("stdio-rpc 派发失败")
            return {
                "error": {
                    "message": f"{type(exc).__name__}: {exc}",
                }
            }

    ctype = resp.headers.get("content-type", "")
    # 二进制响应（图片/视频/文件下载）用 base64 返回；否则按文本返回
    is_binary = not (ctype.startswith("text/") or "json" in ctype or ctype == "")
    decoded = base64.b64encode(resp.content).decode("ascii") if is_binary else resp.text

    return {
        "status": resp.status_code,
        "headers": {"content-type": ctype},
        "body": decoded,
        # 二进制响应时 body 为 base64，Electron 端据此还原
        "bodyBase64": is_binary,
    }


def _server_loop(app) -> None:
    """在独立线程里跑事件循环：逐行读 stdin 并派发，直到 EOF(通道关闭)。"""
    import asyncio

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        asyncio.run(_read_and_dispatch(app))

    # stdin 阻塞读放在该线程内；EOF(± Electron 主进程退出/管道关闭) 即退出
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass
    _run()


async def _read_and_dispatch(app) -> None:
    for line in sys.stdin:
        req = _decode_request(line)
        if req is None:
            continue
        http_request = _build_request(req)
        if http_request is None:
            continue
        result = await _dispatch(app, http_request)
        # 带上原 id 供主进程关联
        result = {"id": req.get("id"), **result}
        _emit(result)


def start_stdio_rpc(app) -> threading.Thread:
    """启动 stdio RPC 后台线程。返回线程句柄（daemon，随进程退出）。"""
    thread = threading.Thread(target=_server_loop, args=(app,), daemon=True, name="stdio-rpc")
    thread.start()
    logger.info("stdio-rpc 点对点通道已启动（stdin/stdout）")
    return thread


def is_stdio_rpc_requested(argv: Optional[list[str]] = None) -> bool:
    """根据命令行参数判断是否启用 stdio 点对点通道。"""
    args = list(argv) if argv is not None else sys.argv[1:]
    return "--stdio-rpc" in args