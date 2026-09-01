"""Persistent personal context snapshot.

This is the durable "what does my AI node know about me right now?" layer.
It complements query-time RAG by maintaining a readable Markdown snapshot in
the Wiki vault plus a JSON cache for mobile clients.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from config import WATCH_FOLDER
import memory_store
from runtime_paths import CONTEXT_SNAPSHOT_JSON_PATH
import wiki_store
from vector_store import get_source_chunks, get_stats, list_documents

logger = logging.getLogger(__name__)

SNAPSHOT_REL_PATH = "Resources/Personal-Context-Snapshot.md"
SNAPSHOT_JSON_PATH = CONTEXT_SNAPSHOT_JSON_PATH
SNAPSHOT_INTERVAL_SECONDS = 6 * 60 * 60
MOBILE_CAPTURE_DIRS = ("mobile_recordings", "mobile_uploads", "mobile_clips")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _trim(text: str, limit: int) -> str:
    clean = " ".join((text or "").split())
    return clean[: max(0, limit - 1)].rstrip() + ("…" if len(clean) > limit else "")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _mobile_capture_paths(limit: int = 12) -> list[Path]:
    watch_root = Path(WATCH_FOLDER).resolve()
    paths: list[Path] = []
    for dirname in MOBILE_CAPTURE_DIRS:
        root = (watch_root / dirname).resolve()
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                target = path.resolve()
                if target.is_relative_to(watch_root):
                    paths.append(target)
            except Exception:
                continue
    paths.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return paths[:limit]


def _source_summary(path: Path, limit: int = 260) -> str:
    source_path = str(path)
    try:
        page = wiki_store.page_for_source(source_path)
        if page and page.get("summary"):
            return _trim(page["summary"], limit)
    except Exception:
        pass
    try:
        chunks = get_source_chunks(source_path, limit=1)
        if chunks:
            return _trim(chunks[0].get("text") or "", limit)
    except Exception:
        pass
    return ""


def _mobile_capture_items() -> list[dict]:
    items = []
    for path in _mobile_capture_paths():
        stat = path.stat()
        kind = {
            "mobile_recordings": "recording",
            "mobile_uploads": "file",
            "mobile_clips": "clip",
        }.get(path.parent.name, "file")
        items.append(
            {
                "path": str(path),
                "name": path.name,
                "kind": kind,
                "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "summary": _source_summary(path),
            }
        )
    return items


def _wiki_items(folder: str, limit: int) -> list[dict]:
    try:
        data = wiki_store.list_pages(folder=folder, limit=limit, offset=0)
    except Exception:
        return []
    items = []
    for page in data.get("items", []):
        items.append(
            {
                "path": page.get("path", ""),
                "title": page.get("title", ""),
                "summary": _trim(page.get("summary") or "", 240),
                "tags": page.get("tags") or [],
                "updated_at": page.get("updated_at", ""),
            }
        )
    return items


def _document_items(limit: int = 16) -> list[dict]:
    try:
        data = list_documents(limit=limit, offset=0)
    except Exception:
        return []
    items = []
    for item in data.get("items", []):
        meta = item.get("metadata") or {}
        source_path = item.get("id") or meta.get("source_path") or ""
        items.append(
            {
                "source_path": source_path,
                "file_name": meta.get("file_name") or Path(source_path).name,
                "file_type": meta.get("file_type", ""),
                "chunk_count": item.get("chunk_count", 0),
            }
        )
    return items


def _memory_excerpt(limit_chars: int = 1800) -> dict:
    try:
        data = memory_store.get_context(agent="personal-context-snapshot", limit_chars=limit_chars)
        context = data.get("context") or ""
        return {"total_chars": data.get("total_chars", 0), "excerpt": _trim(context, limit_chars)}
    except Exception as e:
        return {"total_chars": 0, "excerpt": "", "error": str(e)}


def _line_items(items: list[dict], title_key: str = "title") -> str:
    if not items:
        return "- 暂无"
    lines = []
    for item in items:
        title = item.get(title_key) or item.get("name") or item.get("file_name") or item.get("path") or "Untitled"
        summary = item.get("summary") or ""
        suffix = f" — {summary}" if summary else ""
        lines.append(f"- **{title}**{suffix}")
    return "\n".join(lines)


def _snapshot_markdown(snapshot: dict) -> str:
    stats = snapshot.get("stats") or {}
    vector_stats = stats.get("vector") or {}
    wiki_stats = stats.get("wiki") or {}
    memory = snapshot.get("memory") or {}
    generated_at = snapshot.get("generated_at") or _now()
    return (
        "---\n"
        "title: \"Personal Context Snapshot\"\n"
        "type: \"context_snapshot\"\n"
        "tags: [\"context\", \"snapshot\", \"personal-ai-node\"]\n"
        "maturity: \"evergreen\"\n"
        f"summary: \"个人 AI 节点在 {generated_at} 自动生成的上下文快照\"\n"
        f"updated_at: \"{generated_at}\"\n"
        "---\n\n"
        "# Personal Context Snapshot\n\n"
        f"生成时间：{generated_at}\n\n"
        "## 节点概览\n\n"
        f"- 文档：{vector_stats.get('total_documents', 0)}\n"
        f"- 文本块：{vector_stats.get('total_chunks', 0)}\n"
        f"- Wiki 页面：{wiki_stats.get('total_pages', 0)}\n"
        f"- Wiki 链接：{wiki_stats.get('links', 0)}\n"
        f"- 已整理来源：{wiki_stats.get('organized_sources', 0)}\n\n"
        "## 近期手机采集\n\n"
        f"{_line_items(snapshot.get('recent_mobile') or [], 'name')}\n\n"
        "## 关键 Wiki 概念\n\n"
        f"{_line_items(snapshot.get('concepts') or [])}\n\n"
        "## 最近整理来源\n\n"
        f"{_line_items(snapshot.get('sources') or [])}\n\n"
        "## 文档概览\n\n"
        f"{_line_items(snapshot.get('documents') or [], 'file_name')}\n\n"
        "## 记忆摘录\n\n"
        f"{memory.get('excerpt') or '暂无'}\n"
    )


def build_snapshot(force: bool = False) -> dict:
    """Build, persist, and index the current personal context snapshot."""
    generated_at = _now()
    snapshot = {
        "version": "0.1.0",
        "generated_at": generated_at,
        "wiki_page": SNAPSHOT_REL_PATH,
        "stats": {
            "vector": get_stats(),
            "wiki": wiki_store.stats(),
        },
        "recent_mobile": _mobile_capture_items(),
        "concepts": _wiki_items("Concepts", 16),
        "sources": _wiki_items("Sources", 12),
        "documents": _document_items(16),
        "memory": _memory_excerpt(),
    }
    markdown = _snapshot_markdown(snapshot)
    page = wiki_store.write_page(SNAPSHOT_REL_PATH, markdown, source_agent="context-snapshot")
    snapshot["wiki_page"] = page.get("path", SNAPSHOT_REL_PATH)
    snapshot["markdown"] = markdown
    snapshot["total_chars"] = len(markdown)
    _write_json(SNAPSHOT_JSON_PATH, snapshot)
    return snapshot


def read_snapshot(auto_build: bool = True) -> dict:
    data = _read_json(SNAPSHOT_JSON_PATH)
    if data or not auto_build:
        return data
    return build_snapshot(force=True)


def start_snapshot_loop() -> None:
    def _loop():
        time.sleep(75)
        while True:
            try:
                build_snapshot()
                logger.info("个人 Context 快照已刷新")
            except Exception as e:
                logger.warning(f"个人 Context 快照刷新失败: {e}")
            time.sleep(SNAPSHOT_INTERVAL_SECONDS)

    threading.Thread(target=_loop, daemon=True).start()
