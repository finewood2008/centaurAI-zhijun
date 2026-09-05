"""Obsidian-style Wiki source layer.

Markdown files are the only durable source of truth. SQLite stores lightweight
derived metadata/link mappings; GBrain owns semantic chunks, vectors and graph
retrieval. The legacy Wiki Chroma collection is removed during reconciliation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.error
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Optional
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from config import (
    WATCH_FOLDER,
    WIKI_AI_BLOCK_END,
    WIKI_AI_BLOCK_START,
    WIKI_AI_MAX_CHARS,
    WIKI_AI_MIN_AVAILABLE_MEMORY_MB,
    WIKI_COLLECTION,
    WIKI_DB_PATH,
    WIKI_DIR,
)
from parser import file_hash, is_supported, parse_file
from mindos import llm_transport as wiki_transport
from mindos.runtime_config_provider import get_provider as wiki_get_provider

logger = logging.getLogger(__name__)

_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wiki")
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
_wiki_collection = None
_COLLECTION_LOCK = threading.Lock()
_WIKI_OBSERVER: Optional[Observer] = None
_WIKI_EVENT_TIMES: dict[str, float] = {}
_WIKI_EVENT_LOCK = threading.Lock()

_PARA_FOLDERS = {"Projects", "Areas", "Resources", "Archives"}
_SYSTEM_FOLDERS = {"Sources", "Concepts", *_PARA_FOLDERS}
_INVALID_TITLE_CHARS = re.compile(r"[\\/:*?\"<>|#\[\]\n\r\t]+")
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:[#\|][^\]]*)?\]\]")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _wiki_root() -> Path:
    return Path(WIKI_DIR)


def _ensure_wiki_dirs() -> None:
    root = _wiki_root()
    root.mkdir(parents=True, exist_ok=True)
    for folder in sorted(_SYSTEM_FOLDERS):
        (root / folder).mkdir(parents=True, exist_ok=True)
    home = root / "Home.md"
    if not home.exists():
        home.write_text(
            _frontmatter(
                {
                    "title": "Home",
                    "type": "home",
                    "tags": ["wiki"],
                    "maturity": "evergreen",
                    "created_at": _now(),
                    "updated_at": _now(),
                }
            )
            + "# Home\n\n"
            + "这里是“半人马AI 个人记忆库”的 Wiki 首页。\n\n"
            + f"{WIKI_AI_BLOCK_START}\n"
            + "## 自动维护\n\n"
            + "- Wiki 层会把导入资料沉淀为 Sources 与 Concepts。\n"
            + "- 每次维护会刷新链接、反链和向量索引。\n"
            + f"{WIKI_AI_BLOCK_END}\n",
            encoding="utf-8",
        )


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    _ensure_wiki_dirs()
    conn = sqlite3.connect(WIKI_DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        with conn:
            yield conn
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pages (
              path TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              folder TEXT NOT NULL,
              type TEXT NOT NULL DEFAULT 'note',
              tags_json TEXT NOT NULL DEFAULT '[]',
              aliases_json TEXT NOT NULL DEFAULT '[]',
              maturity TEXT NOT NULL DEFAULT 'seedling',
              summary TEXT NOT NULL DEFAULT '',
              source_path TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL DEFAULT '',
              size INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_pages_title ON pages(title);
            CREATE INDEX IF NOT EXISTS idx_pages_folder ON pages(folder);
            CREATE INDEX IF NOT EXISTS idx_pages_source ON pages(source_path);

            CREATE TABLE IF NOT EXISTS links (
              src_path TEXT NOT NULL,
              target_title TEXT NOT NULL,
              target_path TEXT NOT NULL DEFAULT '',
              PRIMARY KEY (src_path, target_title)
            );
            CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_path);

            CREATE TABLE IF NOT EXISTS source_map (
              source_path TEXT PRIMARY KEY,
              page_path TEXT NOT NULL,
              content_hash TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending',
              error TEXT NOT NULL DEFAULT '',
              organized_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS concept_sources (
              concept_path TEXT NOT NULL,
              source_page TEXT NOT NULL,
              source_path TEXT NOT NULL,
              PRIMARY KEY (concept_path, source_page)
            );

            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gbrain_sync (
              path TEXT PRIMARY KEY,
              slug TEXT NOT NULL,
              content_hash TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending',
              error TEXT NOT NULL DEFAULT '',
              synced_at TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gbrain_sync_slug ON gbrain_sync(slug);

            CREATE TABLE IF NOT EXISTS wiki_jobs (
              job_id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              source_path TEXT NOT NULL,
              name TEXT NOT NULL DEFAULT '',
              state TEXT NOT NULL,
              result_json TEXT NOT NULL DEFAULT '',
              error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.commit()
        _SCHEMA_READY = True


def _safe_title(title: str, fallback: str = "Untitled") -> str:
    title = _INVALID_TITLE_CHARS.sub(" ", (title or "").strip())
    title = re.sub(r"\s+", " ", title).strip(" .-_")
    return (title or fallback)[:80]


def _filename_for_title(title: str) -> str:
    name = _safe_title(title).replace(" ", "-")
    return name[:90] or "Untitled"


def _resolve_rel_path(rel_path: str) -> Path:
    root = _wiki_root().resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"非法 Wiki 路径: {rel_path}")
    if target.suffix.lower() != ".md":
        raise ValueError("Wiki 页面必须是 .md 文件")
    return target


def _rel_from_path(path: Path) -> str:
    # 统一使用正斜杠相对路径（跨平台一致），避免同一文件因反斜杠/正斜杠
    # 混用而被识别为两个不同页面（导致 MindOS 知识卡片重复出现）。
    return path.resolve().relative_to(_wiki_root().resolve()).as_posix()


def _frontmatter(meta: dict) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, (list, dict)):
            encoded = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            encoded = "true" if value else "false"
        elif isinstance(value, (int, float)):
            encoded = str(value)
        else:
            encoded = json.dumps(str(value), ensure_ascii=False)
        lines.append(f"{key}: {encoded}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---\n"):
        return {}, content
    end = content.find("\n---", 4)
    if end == -1:
        return {}, content
    raw = content[4:end].strip()
    after = content.find("\n", end + 4)
    body = content[after + 1 :] if after != -1 else ""
    meta: dict = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        try:
            meta[key] = json.loads(value)
        except Exception:
            if value.lower() in {"true", "false"}:
                meta[key] = value.lower() == "true"
            else:
                meta[key] = value.strip("\"'")
    return meta, body


def _title_from_content(rel_path: str, content: str, meta: dict) -> str:
    if meta.get("title"):
        return _safe_title(str(meta["title"]))
    match = re.search(r"^#\s+(.+)$", content, flags=re.MULTILINE)
    if match:
        return _safe_title(match.group(1))
    return _safe_title(Path(rel_path).stem)


def _coerce_str_list(value) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = re.split(r"[,，;；]", value)
    else:
        items = []
    out = []
    seen = set()
    for item in items:
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s[:40])
    return out[:12]


def _replace_ai_block(existing: str, generated_markdown: str) -> str:
    block = f"{WIKI_AI_BLOCK_START}\n{generated_markdown.strip()}\n{WIKI_AI_BLOCK_END}"
    start = existing.find(WIKI_AI_BLOCK_START)
    end = existing.find(WIKI_AI_BLOCK_END)
    if start != -1 and end != -1 and end > start:
        return existing[:start].rstrip() + "\n\n" + block + existing[end + len(WIKI_AI_BLOCK_END) :]
    return existing.rstrip() + "\n\n" + block + "\n"


def _find_page_by_title(title: str, folder: Optional[str] = None) -> Optional[str]:
    with _connect() as conn:
        if folder:
            row = conn.execute(
                "SELECT path FROM pages WHERE title=? AND folder=? LIMIT 1",
                (title, folder),
            ).fetchone()
        else:
            row = conn.execute("SELECT path FROM pages WHERE title=? LIMIT 1", (title,)).fetchone()
    return row["path"] if row else None


def _resolve_link_target(src_path: str, link_target: str) -> str:
    """Resolve Obsidian-style links while keeping duplicate titles useful.

    Source pages usually link to Concepts, and Concept pages usually link back
    to Sources. That preference prevents same-title source/concept pairs from
    collapsing into self-links in the graph.
    """
    target = (link_target or "").strip().replace("\\", "/")
    if not target:
        return ""

    path_candidates = []
    if "/" in target or target.endswith(".md"):
        rel = target if target.endswith(".md") else f"{target}.md"
        path_candidates.append(rel.lstrip("/"))

    title = _safe_title(Path(target).stem if "/" in target else target, "")
    src_folder = Path(src_path).parent.as_posix()
    if src_folder == ".":
        src_folder = ""
    preferred = {
        "Sources": ["Concepts", "Projects", "Areas", "Resources", "Archives", "Sources", ""],
        "Concepts": ["Sources", "Projects", "Areas", "Resources", "Archives", "Concepts", ""],
        "": ["Sources", "Concepts", "Projects", "Areas", "Resources", "Archives", ""],
    }.get(src_folder, ["Concepts", "Sources", "Projects", "Areas", "Resources", "Archives", ""])

    with _connect() as conn:
        for candidate in path_candidates:
            row = conn.execute("SELECT path FROM pages WHERE path=? LIMIT 1", (candidate,)).fetchone()
            if row:
                return row["path"]
        rows = conn.execute(
            "SELECT path,folder FROM pages WHERE title=? OR path=?",
            (title or target, target),
        ).fetchall()

    if not rows:
        return ""

    def rank(row: sqlite3.Row) -> tuple[int, int, str]:
        folder = row["folder"] or ""
        folder_rank = preferred.index(folder) if folder in preferred else len(preferred)
        self_rank = 1 if row["path"] == src_path else 0
        return (self_rank, folder_rank, row["path"])

    return sorted(rows, key=rank)[0]["path"]


def _page_path_for_title(folder: str, title: str, suffix: str = "") -> str:
    folder = folder if folder in _SYSTEM_FOLDERS else "Resources"
    base = _filename_for_title(title)
    if suffix:
        base = f"{base}-{suffix}"
    rel = f"{folder}/{base}.md"
    if not (_wiki_root() / rel).exists():
        return rel
    for i in range(2, 1000):
        rel = f"{folder}/{base}-{i}.md"
        if not (_wiki_root() / rel).exists():
            return rel
    return f"{folder}/{base}-{uuid.uuid4().hex[:6]}.md"


def _extract_links(content: str) -> list[str]:
    seen = set()
    out = []
    for match in _WIKILINK_RE.finditer(content):
        raw = re.sub(r"\s+", " ", match.group(1).strip()).strip("/")
        title = raw if "/" in raw or raw.endswith(".md") else _safe_title(raw)
        if title and title not in seen:
            seen.add(title)
            out.append(title)
    return out


def _refresh_page(rel_path: str, index_vectors: bool = False) -> dict:
    target = _resolve_rel_path(rel_path)
    if not target.exists():
        raise FileNotFoundError(rel_path)
    content = target.read_text(encoding="utf-8", errors="replace")
    stat = target.stat()
    meta, _body = _parse_frontmatter(content)
    title = _title_from_content(rel_path, content, meta)
    folder = Path(rel_path).parent.as_posix()
    if folder == ".":
        folder = ""
    page_type = str(meta.get("type") or ("source" if folder == "Sources" else "concept" if folder == "Concepts" else "note"))
    tags = _coerce_str_list(meta.get("tags"))
    aliases = _coerce_str_list(meta.get("aliases"))
    maturity = str(meta.get("maturity") or "seedling")
    summary = str(meta.get("summary") or "")[:1000]
    source_path = str(meta.get("source_path") or "")
    updated_at = str(meta.get("updated_at") or datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"))
    created_at = str(meta.get("created_at") or updated_at)

    links = _extract_links(content)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO pages(path,title,folder,type,tags_json,aliases_json,maturity,summary,source_path,created_at,updated_at,size)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              title=excluded.title, folder=excluded.folder, type=excluded.type,
              tags_json=excluded.tags_json, aliases_json=excluded.aliases_json,
              maturity=excluded.maturity, summary=excluded.summary,
              source_path=excluded.source_path, updated_at=excluded.updated_at, size=excluded.size
            """,
            (
                rel_path,
                title,
                folder,
                page_type,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(aliases, ensure_ascii=False),
                maturity,
                summary,
                source_path,
                created_at,
                updated_at,
                stat.st_size,
            ),
        )
        conn.execute("DELETE FROM links WHERE src_path=?", (rel_path,))
        for link_title in links:
            target_path = _resolve_link_target(rel_path, link_title)
            conn.execute(
                "INSERT OR REPLACE INTO links(src_path,target_title,target_path) VALUES(?,?,?)",
                (rel_path, link_title, target_path),
            )
        conn.commit()

    return read_page(rel_path) or {}


def _delete_page_index(rel_path: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM pages WHERE path=?", (rel_path,))
        conn.execute("DELETE FROM links WHERE src_path=? OR target_path=?", (rel_path, rel_path))
        conn.execute("DELETE FROM concept_sources WHERE concept_path=? OR source_page=?", (rel_path, rel_path))
        conn.execute("DELETE FROM source_map WHERE page_path=?", (rel_path,))
        conn.commit()


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _record_gbrain_sync(
    rel_path: str,
    slug: str,
    content_hash: str,
    status: str,
    error: str = "",
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO gbrain_sync(path,slug,content_hash,status,error,synced_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              slug=excluded.slug, content_hash=excluded.content_hash,
              status=excluded.status, error=excluded.error, synced_at=excluded.synced_at
            """,
            (rel_path, slug, content_hash, status, error[:1000], _now()),
        )
        conn.commit()


def gbrain_slug_for_path(rel_path: str) -> str:
    import gbrain_store

    with _connect() as conn:
        row = conn.execute("SELECT slug FROM gbrain_sync WHERE path=?", (rel_path,)).fetchone()
    return row["slug"] if row else gbrain_store.slug_for_wiki_path(rel_path)


def wiki_path_for_gbrain_slug(slug: str) -> str:
    import gbrain_store

    with _connect() as conn:
        row = conn.execute("SELECT path FROM gbrain_sync WHERE slug=?", (slug,)).fetchone()
        if row:
            return row["path"]
        paths = [r["path"] for r in conn.execute("SELECT path FROM pages").fetchall()]
    for rel_path in paths:
        try:
            if gbrain_store.slug_for_wiki_path(rel_path) == slug:
                return rel_path
        except gbrain_store.GBrainError:
            continue
    return ""


def sync_page_to_gbrain(rel_path: str, content: Optional[str] = None, force: bool = False) -> dict:
    """Synchronize one Wiki source file into GBrain, using a content-hash ledger."""
    import gbrain_store

    target = _resolve_rel_path(rel_path)
    if content is None:
        if not target.is_file():
            raise FileNotFoundError(rel_path)
        content = target.read_text(encoding="utf-8", errors="replace")
    digest = _content_digest(content)
    slug = gbrain_store.slug_for_wiki_path(rel_path)
    if not force:
        with _connect() as conn:
            row = conn.execute(
                "SELECT content_hash,status FROM gbrain_sync WHERE path=?", (rel_path,)
            ).fetchone()
        if row and row["content_hash"] == digest and row["status"] == "done":
            return {"success": True, "skipped": True, "path": rel_path, "slug": slug}
    try:
        result = gbrain_store.sync_wiki_page(rel_path, content)
        _record_gbrain_sync(rel_path, slug, digest, "done")
        return {"success": True, "skipped": False, "path": rel_path, "slug": slug, "result": result}
    except Exception as exc:
        _record_gbrain_sync(rel_path, slug, digest, "failed", str(exc))
        raise


def remove_page_from_gbrain(rel_path: str) -> dict:
    """Propagate a Wiki file deletion, while preserving a retryable failure ledger."""
    import gbrain_store

    slug = gbrain_slug_for_path(rel_path)
    error: Optional[Exception] = None
    result: dict = {}
    try:
        result = gbrain_store.delete_wiki_page(rel_path)
    except Exception as exc:
        error = exc
    _delete_page_index(rel_path)
    with _connect() as conn:
        if error is None:
            conn.execute("DELETE FROM gbrain_sync WHERE path=?", (rel_path,))
        else:
            conn.execute(
                """
                INSERT INTO gbrain_sync(path,slug,content_hash,status,error,synced_at)
                VALUES(?,?,?,'delete_failed',?,?)
                ON CONFLICT(path) DO UPDATE SET status='delete_failed',error=excluded.error,synced_at=excluded.synced_at
                """,
                (rel_path, slug, "", str(error)[:1000], _now()),
            )
        conn.commit()
    if error is not None:
        raise error
    return {"success": True, "path": rel_path, "slug": slug, "result": result}


def _drop_legacy_wiki_vectors() -> bool:
    """Delete the superseded Chroma Wiki collection; Markdown and metadata stay intact."""
    global _wiki_collection
    try:
        from vector_store import (
            _get_client,
            ensure_index_writable,
            record_index_operation_failure,
        )

        ensure_index_writable()
        client = _get_client()
        with _COLLECTION_LOCK:
            _wiki_collection = None
            try:
                client.delete_collection(WIKI_COLLECTION)
            except Exception as exc:
                record_index_operation_failure(exc, "legacy_wiki_vector_delete")
                return False
        return True
    except Exception as exc:
        try:
            from vector_store import record_index_operation_failure
            record_index_operation_failure(exc, "legacy_wiki_vector_delete")
        except Exception:
            pass
        logger.warning("清理旧 Wiki 向量索引失败: %s", exc)
        return False


def sync_all_to_gbrain() -> dict:
    """Bulk-upsert current Wiki files, reconcile deletions, then update the sync ledger."""
    import gbrain_store

    current_paths = {
        _rel_from_path(path)
        for path in _wiki_root().rglob("*.md")
        if not path.name.startswith(".")
    }
    with _connect() as conn:
        ledger_paths = {r["path"] for r in conn.execute("SELECT path FROM gbrain_sync").fetchall()}
    delete_failures = []
    for stale_path in sorted(ledger_paths - current_paths):
        try:
            remove_page_from_gbrain(stale_path)
        except Exception as exc:
            delete_failures.append({"path": stale_path, "error": str(exc)})

    bulk = gbrain_store.sync_wiki()
    for rel_path in sorted(current_paths):
        target = _resolve_rel_path(rel_path)
        content = target.read_text(encoding="utf-8", errors="replace")
        _record_gbrain_sync(
            rel_path,
            gbrain_store.slug_for_wiki_path(rel_path),
            _content_digest(content),
            "done",
        )
    legacy_removed = _drop_legacy_wiki_vectors()
    return {
        "success": not delete_failures,
        "pages": len(current_paths),
        "delete_failures": delete_failures,
        "legacy_vectors_removed": legacy_removed,
        "gbrain": bulk,
    }


def initialize() -> None:
    _ensure_wiki_dirs()
    with _connect():
        pass
    scan_wiki_files(index_vectors=False, sync_gbrain=False)


def list_pages(
    folder: Optional[str] = None,
    query: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    _ensure_wiki_dirs()
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM pages").fetchone()["c"]
    if count == 0:
        scan_wiki_files(index_vectors=False)
    clauses = []
    params: list[str | int] = []
    if folder:
        clauses.append("folder=?")
        params.append(folder)
    if query.strip():
        clauses.append("(title LIKE ? OR summary LIKE ? OR tags_json LIKE ?)")
        q = f"%{query.strip()}%"
        params.extend([q, q, q])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM pages{where}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM pages{where} ORDER BY updated_at DESC, title ASC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"total": total, "items": [_row_to_page(r) for r in rows]}


def _row_to_page(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.pop("tags_json") or "[]")
    d["aliases"] = json.loads(d.pop("aliases_json") or "[]")
    return d


def read_page(rel_path: str) -> Optional[dict]:
    try:
        target = _resolve_rel_path(rel_path)
    except ValueError:
        return None
    if not target.exists():
        return None
    content = target.read_text(encoding="utf-8", errors="replace")
    with _connect() as conn:
        row = conn.execute("SELECT * FROM pages WHERE path=?", (rel_path,)).fetchone()
        inbound = conn.execute(
            "SELECT src_path FROM links WHERE target_path=? ORDER BY src_path", (rel_path,)
        ).fetchall()
        outbound = conn.execute(
            "SELECT target_title,target_path FROM links WHERE src_path=? ORDER BY target_title", (rel_path,)
        ).fetchall()
    page = _row_to_page(row) if row else _refresh_page(rel_path, index_vectors=False)
    page["content"] = content
    page["inbound"] = [r["src_path"] for r in inbound]
    page["outbound"] = [dict(r) for r in outbound]
    return page


def page_for_source(source_path: str) -> Optional[dict]:
    source = str(Path(source_path).absolute())
    with _connect() as conn:
        row = conn.execute("SELECT page_path FROM source_map WHERE source_path=?", (source,)).fetchone()
        if not row:
            row = conn.execute("SELECT path AS page_path FROM pages WHERE source_path=?", (source,)).fetchone()
    if not row:
        return None
    return read_page(row["page_path"])


def write_page(rel_path: str, content: str, source_agent: str = "manual") -> dict:
    target = _resolve_rel_path(rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not content.startswith("---\n"):
        title = _safe_title(Path(rel_path).stem)
        content = _frontmatter(
            {
                "title": title,
                "type": "note",
                "tags": [],
                "maturity": "seedling",
                "created_at": _now(),
                "updated_at": _now(),
                "source_agent": source_agent,
            }
        ) + content
    # Wiki Markdown is the source of truth.  A direct overwrite can leave a
    # truncated card after a forced shutdown, so publish a fully fsynced
    # sibling file with an atomic replace instead.
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        if os.name != "nt":
            try:
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass
    page = _refresh_page(rel_path, index_vectors=False)
    try:
        page["gbrain_sync"] = sync_page_to_gbrain(rel_path, content)
    except Exception as exc:
        logger.warning("Wiki 已保存，但 GBrain 增量同步失败 %s: %s", rel_path, exc)
        page["gbrain_sync"] = {"success": False, "error": str(exc)}
    return page


def create_page(
    title: str,
    folder: str = "Resources",
    content: str = "",
    tags: Optional[list[str]] = None,
    page_type: Optional[str] = None,
) -> dict:
    title = _safe_title(title)
    folder = folder if folder in _SYSTEM_FOLDERS else "Resources"
    rel_path = _page_path_for_title(folder, title)
    body = content.strip() or f"# {title}\n\n"
    if not body.startswith("# "):
        body = f"# {title}\n\n{body}\n"
    return write_page(
        rel_path,
        _frontmatter(
            {
                "title": title,
                "type": page_type or ("concept" if folder == "Concepts" else "note"),
                "tags": tags or [],
                "maturity": "seedling",
                "created_at": _now(),
                "updated_at": _now(),
            }
        )
        + body,
    )


def scan_wiki_files(index_vectors: bool = False, sync_gbrain: bool = False) -> int:
    _ensure_wiki_dirs()
    count = 0
    seen_paths = set()
    for path in sorted(_wiki_root().rglob("*.md")):
        if path.name.startswith("."):
            continue
        try:
            rel_path = _rel_from_path(path)
            seen_paths.add(rel_path)
            _refresh_page(rel_path, index_vectors=False)
            if sync_gbrain:
                sync_page_to_gbrain(rel_path)
            count += 1
        except Exception as e:
            logger.warning(f"Wiki 页面扫描失败 {path}: {e}")
    with _connect() as conn:
        indexed_paths = {r["path"] for r in conn.execute("SELECT path FROM pages").fetchall()}
    for stale_path in sorted(indexed_paths - seen_paths):
        try:
            remove_page_from_gbrain(stale_path)
        except Exception as exc:
            logger.warning("Wiki 删除传播失败 %s: %s", stale_path, exc)
    return count


def reindex_all_wiki() -> dict:
    count = scan_wiki_files(index_vectors=False, sync_gbrain=False)
    sync = sync_all_to_gbrain()
    return {"success": sync.get("success", False), "pages_indexed": count, "sync": sync, **stats()}


def search_wiki(query: str, n_results: int = 10) -> list[dict]:
    if not query.strip():
        return []
    try:
        import gbrain_store

        results = gbrain_store.search_pages(query, mode="hybrid", limit=n_results).get("items", [])
        out = []
        for item in results:
            rel_path = wiki_path_for_gbrain_slug(str(item.get("slug") or ""))
            if not rel_path:
                continue
            with _connect() as conn:
                row = conn.execute("SELECT * FROM pages WHERE path=?", (rel_path,)).fetchone()
            page = _row_to_page(row) if row else {}
            score = float(item.get("score") or 0.0)
            metadata = {
                "page_path": rel_path,
                "title": page.get("title") or item.get("title") or rel_path,
                "folder": page.get("folder") or Path(rel_path).parent.as_posix(),
                "wiki_type": page.get("type") or item.get("type") or "note",
                "file_type": "wiki",
                "vector_engine": "gbrain",
            }
            out.append(
                {
                    "id": f"gbrain::{item.get('chunk_id') or item.get('slug')}",
                    "page_path": rel_path,
                    "title": metadata["title"],
                    "folder": metadata["folder"],
                    "wiki_type": metadata["wiki_type"],
                    "text": item.get("chunk_text") or "",
                    "score": score,
                    "distance": max(0.0, 1.0 - score),
                    "match_type": "wiki",
                    "metadata": metadata,
                }
            )
        return out
    except Exception as exc:
        logger.warning("GBrain Wiki 检索失败，降级为标题检索: %s", exc)
        pages = list_pages(query=query, limit=n_results, offset=0).get("items", [])
        return [
            {
                "id": f"wiki-meta::{page['path']}",
                "page_path": page["path"],
                "title": page["title"],
                "folder": page["folder"],
                "wiki_type": page["type"],
                "text": page.get("summary") or page["title"],
                "score": 0.0,
                "distance": 1.0,
                "match_type": "wiki",
                "metadata": {"page_path": page["path"], "vector_engine": "metadata-fallback"},
            }
            for page in pages
        ]


def _local_graph(page_path: Optional[str] = None) -> dict:
    with _connect() as conn:
        pages = conn.execute("SELECT path,title,folder,type FROM pages").fetchall()
        links = conn.execute("SELECT src_path,target_title,target_path FROM links").fetchall()
    nodes = {r["path"]: {"id": r["path"], "title": r["title"], "folder": r["folder"], "type": r["type"]} for r in pages}
    edges = [
        {"source": r["src_path"], "target": r["target_path"] or r["target_title"], "target_title": r["target_title"]}
        for r in links
    ]
    if page_path:
        keep = {page_path}
        for e in edges:
            if e["source"] == page_path:
                keep.add(e["target"])
            if e["target"] == page_path:
                keep.add(e["source"])
        nodes = {k: v for k, v in nodes.items() if k in keep}
        edges = [e for e in edges if e["source"] in keep and e["target"] in keep]
    return {"nodes": list(nodes.values()), "edges": edges}


def graph(page_path: Optional[str] = None) -> dict:
    if not page_path:
        return _local_graph()
    try:
        import gbrain_store

        slug = gbrain_slug_for_path(page_path)
        data = gbrain_store.graph(slug, depth=2)
        nodes = []
        known: dict[str, str] = {}
        for node in data.get("nodes", []):
            node_slug = str(node.get("slug") or "")
            rel_path = wiki_path_for_gbrain_slug(node_slug)
            if not rel_path:
                continue
            known[node_slug] = rel_path
            with _connect() as conn:
                row = conn.execute(
                    "SELECT path,title,folder,type FROM pages WHERE path=?", (rel_path,)
                ).fetchone()
            if row:
                nodes.append(
                    {
                        "id": rel_path,
                        "title": row["title"],
                        "folder": row["folder"],
                        "type": row["type"],
                        "relation": "semantic" if node.get("depth", 0) else "root",
                    }
                )
        edges = []
        for edge in data.get("edges", []):
            source = known.get(str(edge.get("source") or ""))
            target = known.get(str(edge.get("target") or ""))
            if source and target:
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "target_title": target,
                        "type": edge.get("type") or "semantic",
                    }
                )
        if nodes:
            return {"nodes": nodes, "edges": edges, "engine": "gbrain"}
    except Exception as exc:
        logger.warning("GBrain Wiki 图谱失败，降级为 Wiki 链接图: %s", exc)
    return {**_local_graph(page_path), "engine": "wiki-link-fallback"}


def stats() -> dict:
    with _connect() as conn:
        rows = conn.execute("SELECT folder, COUNT(*) AS c FROM pages GROUP BY folder").fetchall()
        total = conn.execute("SELECT COUNT(*) AS c FROM pages").fetchone()["c"]
        links = conn.execute("SELECT COUNT(*) AS c FROM links").fetchone()["c"]
        sources = conn.execute("SELECT COUNT(*) AS c FROM source_map WHERE status='done'").fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM source_map WHERE status IN ('pending','failed')"
        ).fetchone()["c"]
        gbrain_done = conn.execute(
            "SELECT COUNT(*) AS c FROM gbrain_sync WHERE status='done'"
        ).fetchone()["c"]
        gbrain_failed = conn.execute(
            "SELECT COUNT(*) AS c FROM gbrain_sync WHERE status!='done'"
        ).fetchone()["c"]
    return {
        "total_pages": total,
        "links": links,
        "organized_sources": sources,
        "pending_sources": pending,
        "vector_engine": "gbrain",
        "gbrain_synced": gbrain_done,
        "gbrain_pending": gbrain_failed,
        "by_folder": {r["folder"] or "Root": r["c"] for r in rows},
    }


def _source_excerpt(source_path: str) -> tuple[str, str]:
    path = Path(source_path)
    ext = path.suffix.lower()
    try:
        parsed = parse_file(str(path))
        if parsed["file_type"] == "text":
            return parsed.get("text", "")[:WIKI_AI_MAX_CHARS], "text"
        if parsed["file_type"] == "image":
            import annotations as doc_annotations

            caption = doc_annotations.caption_of(str(path.absolute()))
            text = f"图片文件：{path.name}\n用户说明：{caption}" if caption else f"图片文件：{path.name}"
            return text[:WIKI_AI_MAX_CHARS], "image"
    except Exception as e:
        logger.debug(f"读取源文件片段失败，尝试向量块: {e}")

    try:
        # P0-2：复用带当前代过滤的读取（get_source_chunks 内部按 generation 注册表
        # 过滤，且已按 start_time/chunk_index 排序），不再裸查文本集合。
        # 空 chunks → ("", "indexed")，与原裸查行为一致
        from vector_store import get_source_chunks

        chunks = get_source_chunks(str(path.absolute()), limit=24)
        return "\n\n".join(c["text"] for c in chunks)[:WIKI_AI_MAX_CHARS], "indexed"
    except Exception:
        return f"文件：{path.name}", "unknown"


def _ollama_model_names(payload: dict) -> list[str]:
    names = []
    for item in payload.get("models") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "").strip()
        if name:
            names.append(name)
    return names


def _available_memory_mb() -> Optional[int]:
    """Read Linux MemAvailable without adding a runtime dependency."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def local_organizer_status() -> dict:
    """Report the fixed localhost-only Wiki organizer runtime."""
    # §5.1.1：状态读取边界取一次材料通道快照（Wiki 共享本地 Ollama）。
    snap = wiki_get_provider().get_local_snapshot()
    try:
        resp = wiki_transport.allowed_urlopen(
            snap.base_url.rstrip("/") + "/api/tags",
            channel="material",
            store=wiki_get_provider().store,
            timeout=5,
            method="GET",
        )
        payload = json.loads(resp.read().decode("utf-8"))
        models = _ollama_model_names(payload)
        ready = snap.model in models
        return {
            "available": True,
            "ready": ready,
            "local_only": True,
            "provider": "ollama",
            "model": snap.model,
            "models": models,
            "fallback": "local_rules",
            "memory_policy": {
                "mode": "on_demand",
                "keep_alive_seconds": snap.keep_alive,
                "max_loaded_models": 1,
                "max_parallel": 1,
                "context_window": snap.context_window,
                "min_available_memory_mb": WIKI_AI_MIN_AVAILABLE_MEMORY_MB,
            },
            "available_memory_mb": _available_memory_mb(),
            "error": "" if ready else f"本地模型 {snap.model} 尚未安装",
        }
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as exc:
        return {
            "available": False,
            "ready": False,
            "local_only": True,
            "provider": "ollama",
            "model": snap.model,
            "models": [],
            "fallback": "local_rules",
            "memory_policy": {
                "mode": "on_demand",
                "keep_alive_seconds": snap.keep_alive,
                "max_loaded_models": 1,
                "max_parallel": 1,
                "context_window": snap.context_window,
                "min_available_memory_mb": WIKI_AI_MIN_AVAILABLE_MEMORY_MB,
            },
            "available_memory_mb": _available_memory_mb(),
            "error": f"无法连接本机 Ollama: {exc}",
        }


def _parse_local_model_json(content: str) -> dict:
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("本地模型没有返回 JSON 对象")
    return value


def _call_local_organizer(file_name: str, source_type: str, excerpt: str, snap) -> Optional[dict]:
    available_memory_mb = _available_memory_mb()
    if available_memory_mb is not None and available_memory_mb < WIKI_AI_MIN_AVAILABLE_MEMORY_MB:
        logger.warning(
            "可用内存仅 %s MB，低于本地模型安全线 %s MB，使用本地规则降级",
            available_memory_mb,
            WIKI_AI_MIN_AVAILABLE_MEMORY_MB,
        )
        return None

    system = (
        "你是本地运行的私有 Wiki 整理器。严格按给定 JSON Schema 输出，不要输出 Markdown。"
        "根据资料片段生成 Obsidian 风格 Wiki 条目结构；不得编造资料中没有的事实。"
        "摘要、概念和标签优先使用中文，专有名词可保留原文。"
    )
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "para": {"type": "string", "enum": ["Projects", "Areas", "Resources", "Archives"]},
            "concepts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "aliases": {"type": "array", "items": {"type": "string"}},
                        "maturity": {"type": "string", "enum": ["seedling", "budding", "evergreen"]},
                    },
                    "required": ["title", "summary", "tags", "aliases", "maturity"],
                },
            },
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "tags", "para", "concepts", "evidence"],
    }
    user = (
        "整理要求：\n"
        "1. summary 用一到三句话概括资料，不添加资料之外的信息。\n"
        "2. tags 提取一到六个短标签。\n"
        "3. para 按 PARA 方法选择最合适的一个分类。\n"
        "4. concepts 提取一到五个资料中真实出现的核心概念；title 必须具体，"
        "禁止使用‘原子概念标题’、‘概念1’、‘未命名’等占位词。\n"
        "5. evidence 选择一到六条能支持摘要的资料原句。\n\n"
        f"文件名：{file_name}\n资料类型：{source_type}\n\n资料片段：\n{excerpt[:WIKI_AI_MAX_CHARS]}"
    )
    body = {
        "model": snap.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "format": schema,
        "think": False,
        "stream": False,
        # 0 表示响应完成后立即卸载，不让生成模型持续占用内存。
        "keep_alive": snap.keep_alive,
        "options": {"temperature": 0.1, "num_ctx": snap.context_window},
    }
    try:
        resp = wiki_transport.allowed_urlopen(
            snap.base_url.rstrip("/") + "/api/chat",
            channel="material",
            store=wiki_get_provider().store,
            timeout=snap.timeout_seconds,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        payload = json.loads(resp.read().decode("utf-8"))
        return _parse_local_model_json(payload["message"]["content"])
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
        KeyError,
        ValueError,
        json.JSONDecodeError,
        TimeoutError,
    ) as exc:
        logger.warning("本地 Ollama Wiki 整理失败，使用本地规则降级: %s", exc)
        return None



def _fallback_organize(file_name: str, excerpt: str) -> dict:
    title = _safe_title(Path(file_name).stem)
    clean = re.sub(r"\s+", " ", excerpt or "").strip()
    summary = clean[:240] or f"{file_name} 的导入资料"
    tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", clean)
    tags = []
    for tok in tokens:
        if tok not in tags and len(tok) <= 16:
            tags.append(tok)
        if len(tags) >= 4:
            break
    if not tags:
        tags = ["导入资料"]
    return {
        "summary": summary,
        "tags": tags[:4],
        "para": "Resources",
        "concepts": [{"title": title, "summary": summary, "tags": tags[:4], "aliases": [], "maturity": "seedling"}],
        "evidence": [summary[:180]],
    }


def _clean_payload(data: dict, file_name: str, excerpt: str) -> dict:
    if not isinstance(data, dict):
        data = _fallback_organize(file_name, excerpt)
    summary = str(data.get("summary") or "").strip()[:1200]
    if not summary:
        summary = _fallback_organize(file_name, excerpt)["summary"]
    tags = _coerce_str_list(data.get("tags")) or ["导入资料"]
    para = str(data.get("para") or "Resources")
    if para not in _PARA_FOLDERS:
        para = "Resources"
    concepts = data.get("concepts") if isinstance(data.get("concepts"), list) else []
    clean_concepts = []
    for item in concepts[:8]:
        if not isinstance(item, dict):
            continue
        title = _safe_title(str(item.get("title") or ""), "")
        if not title:
            continue
        clean_concepts.append(
            {
                "title": title,
                "summary": str(item.get("summary") or summary).strip()[:1000],
                "tags": _coerce_str_list(item.get("tags")) or tags,
                "aliases": _coerce_str_list(item.get("aliases")),
                "maturity": str(item.get("maturity") or "seedling")[:30],
            }
        )
    if not clean_concepts:
        clean_concepts = _fallback_organize(file_name, excerpt)["concepts"]
    evidence = _coerce_str_list(data.get("evidence"))
    if not evidence:
        evidence = [summary[:180]]
    return {"summary": summary, "tags": tags, "para": para, "concepts": clean_concepts, "evidence": evidence[:6]}


def _source_links_for_concept(concept_path: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT source_page FROM concept_sources WHERE concept_path=? ORDER BY source_page",
            (concept_path,),
        ).fetchall()
    return [r["source_page"] for r in rows]


def _titles_for_pages(paths: list[str]) -> list[str]:
    if not paths:
        return []
    with _connect() as conn:
        out = []
        for rel_path in paths:
            row = conn.execute("SELECT title FROM pages WHERE path=?", (rel_path,)).fetchone()
            out.append(row["title"] if row else Path(rel_path).stem)
    return out


def _record_source_status(source_path: str, page_path: str, content_hash: str, status: str, error: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO source_map(source_path,page_path,content_hash,status,error,organized_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(source_path) DO UPDATE SET
              page_path=excluded.page_path, content_hash=excluded.content_hash,
              status=excluded.status, error=excluded.error, organized_at=excluded.organized_at
            """,
            (source_path, page_path, content_hash, status, error[:1000], _now()),
        )
        conn.commit()


def organize_source(source_path: str, force: bool = False) -> dict:
    _ensure_wiki_dirs()
    source = Path(source_path).resolve()
    watch_root = Path(WATCH_FOLDER).resolve()
    if not source.is_file() or not source.is_relative_to(watch_root):
        raise ValueError("source_path 必须是监控目录内的文件")
    if not is_supported(str(source)):
        raise ValueError("不支持的源文件类型")

    content_hash = file_hash(str(source))
    with _connect() as conn:
        existing = conn.execute("SELECT * FROM source_map WHERE source_path=?", (str(source),)).fetchone()
    if existing and existing["content_hash"] == content_hash and existing["status"] == "done" and not force:
        page = read_page(existing["page_path"])
        return {"skipped": True, "page": page}

    source_rel = existing["page_path"] if existing else ""
    if not source_rel:
        source_rel = _page_path_for_title("Sources", _safe_title(source.stem), content_hash[:8])

    excerpt, source_type = _source_excerpt(str(source))
    # §5.1.1：整理任务边界取一次材料通道快照（Wiki 共享本地 Ollama），下传给模型调用。
    snap = wiki_get_provider().get_local_snapshot()
    local_data = _call_local_organizer(source.name, source_type, excerpt, snap)
    payload = _clean_payload(local_data or _fallback_organize(source.name, excerpt), source.name, excerpt)
    source_title = _safe_title(source.stem)
    concept_links = [f"[[{c['title']}]]" for c in payload["concepts"]]
    tags = sorted(set(["source", source_type, *payload["tags"]]))
    meta = {
        "title": source_title,
        "type": "source",
        "source_path": str(source),
        "source_file": source.name,
        "tags": tags,
        "para": payload["para"],
        "maturity": "seedling",
        "summary": payload["summary"][:500],
        "created_at": _now(),
        "updated_at": _now(),
    }
    target = _resolve_rel_path(source_rel)
    existing_source = target.read_text(encoding="utf-8", errors="replace") if target.exists() else _frontmatter(meta) + f"# {source_title}\n"
    if target.exists():
        old_meta, old_body = _parse_frontmatter(existing_source)
        old_meta.update(meta)
        old_meta["created_at"] = old_meta.get("created_at") or meta["created_at"]
        existing_source = _frontmatter(old_meta) + old_body.lstrip()

    evidence = "\n".join(f"- {e}" for e in payload["evidence"])
    ai_block = (
        "## 摘要\n\n"
        f"{payload['summary']}\n\n"
        "## 相关概念\n\n"
        + "\n".join(f"- {link}" for link in concept_links)
        + "\n\n## 标签\n\n"
        + " ".join(f"#{t}" for t in payload["tags"])
        + "\n\n## 来源\n\n"
        + f"`{source}`\n\n"
        + "## 证据片段\n\n"
        + evidence
    )
    source_page = write_page(source_rel, _replace_ai_block(existing_source, ai_block), source_agent="wiki-organizer")

    concept_pages = []
    for concept in payload["concepts"]:
        concept_title = concept["title"]
        concept_rel = _find_page_by_title(concept_title, "Concepts") or _page_path_for_title("Concepts", concept_title)
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO concept_sources(concept_path,source_page,source_path) VALUES(?,?,?)",
                (concept_rel, source_rel, str(source)),
            )
            conn.commit()
        concept_target = _resolve_rel_path(concept_rel)
        concept_meta = {
            "title": concept_title,
            "type": "concept",
            "aliases": concept["aliases"],
            "tags": sorted(set(["concept", *concept["tags"]])),
            "maturity": concept["maturity"] or "seedling",
            "summary": concept["summary"][:500],
            "created_at": _now(),
            "updated_at": _now(),
        }
        existing_concept = (
            concept_target.read_text(encoding="utf-8", errors="replace")
            if concept_target.exists()
            else _frontmatter(concept_meta) + f"# {concept_title}\n"
        )
        if concept_target.exists():
            old_meta, old_body = _parse_frontmatter(existing_concept)
            old_meta.update(concept_meta)
            old_meta["created_at"] = old_meta.get("created_at") or concept_meta["created_at"]
            existing_concept = _frontmatter(old_meta) + old_body.lstrip()
        sources = _titles_for_pages(_source_links_for_concept(concept_rel))
        concept_ai = (
            "## 概述\n\n"
            f"{concept['summary']}\n\n"
            "## 关联来源\n\n"
            + "\n".join(f"- [[{s}]]" for s in sources)
        )
        concept_pages.append(
            write_page(concept_rel, _replace_ai_block(existing_concept, concept_ai), source_agent="wiki-organizer")
        )

    source_page = _refresh_page(source_rel, index_vectors=True)
    _record_source_status(str(source), source_rel, content_hash, "done")
    return {"skipped": False, "source_page": source_page, "concept_pages": concept_pages}


def _upsert_wiki_job(job: dict) -> None:
    """把整理任务记录持久化到 wiki_jobs 表（终态跨重启保留）。异常仅告警。"""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO wiki_jobs(job_id, kind, source_path, name, state, result_json, error, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    job.get("id", ""),
                    job.get("kind", ""),
                    job.get("source_path", ""),
                    job.get("name", ""),
                    job.get("state", ""),
                    json.dumps(job.get("result") or {}, ensure_ascii=False),
                    job.get("error", ""),
                    job.get("created_at", ""),
                    job.get("updated_at", ""),
                ),
            )
    except Exception as e:
        logger.warning(f"持久化 Wiki 整理任务失败 {job.get('source_path', '')}: {e}")


def submit_source(source_path: str, force: bool = False) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        job = {
            "id": job_id,
            "kind": "organize_source",
            "source_path": str(Path(source_path).absolute()),
            "name": Path(source_path).name,
            "state": "queued",
            "created_at": _now(),
            "updated_at": _now(),
        }
        _JOBS[job_id] = job
    _upsert_wiki_job(job)
    _POOL.submit(_run_source_job, job_id, source_path, force)
    return job_id


def _run_source_job(job_id: str, source_path: str, force: bool) -> None:
    with _JOBS_LOCK:
        _JOBS[job_id]["state"] = "processing"
        _JOBS[job_id]["updated_at"] = _now()
    try:
        result = organize_source(source_path, force=force)
        with _JOBS_LOCK:
            _JOBS[job_id]["state"] = "done"
            _JOBS[job_id]["result"] = {
                "skipped": result.get("skipped", False),
                "page_path": (result.get("page") or result.get("source_page") or {}).get("path", ""),
            }
            _JOBS[job_id]["updated_at"] = _now()
            _upsert_wiki_job(_JOBS[job_id])
    except Exception as e:
        logger.warning(f"Wiki 整理失败 {source_path}: {e}")
        try:
            _record_source_status(str(Path(source_path).absolute()), "", file_hash(source_path), "failed", str(e))
        except Exception:
            pass
        with _JOBS_LOCK:
            _JOBS[job_id]["state"] = "failed"
            _JOBS[job_id]["error"] = str(e)
            _JOBS[job_id]["updated_at"] = _now()
            _upsert_wiki_job(_JOBS[job_id])


def list_jobs(include_done: bool = False) -> list[dict]:
    with _JOBS_LOCK:
        jobs = [dict(j) for j in _JOBS.values() if include_done or j.get("state") in {"queued", "processing"}]
    # 后端重启后 _JOBS 清空：从 wiki_jobs 表恢复任务历史（终态）。
    in_mem = {j["id"] for j in jobs}
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT * FROM wiki_jobs").fetchall()
    except Exception as e:
        logger.warning(f"读取 Wiki 任务历史失败: {e}")
        rows = []
    for row in rows:
        row_id = row["job_id"]
        if row_id in in_mem:
            continue
        job = dict(row)
        job["id"] = job.pop("job_id")
        if job.get("result_json"):
            try:
                job["result"] = json.loads(job["result_json"])
            except Exception:
                pass
        job.pop("result_json", None)
        if not include_done and job.get("state") not in {"queued", "processing"}:
            continue
        jobs.append(job)
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return jobs


def pending_jobs() -> int:
    with _JOBS_LOCK:
        return sum(1 for j in _JOBS.values() if j.get("state") in {"queued", "processing"})


def run_maintenance() -> dict:
    result = reindex_all_wiki()
    g = graph()
    inbound = {e["target"] for e in g["edges"] if e["target"] in {n["id"] for n in g["nodes"]}}
    orphans = [n for n in g["nodes"] if n["id"] not in inbound and n["id"] != "Home.md"][:20]
    home_path = "Home.md"
    home = read_page(home_path)
    content = home["content"] if home else _frontmatter({"title": "Home", "type": "home"}) + "# Home\n"
    ai = (
        "## 自动维护\n\n"
        f"- 最近维护：{_now()}\n"
        f"- Wiki 页面：{result.get('total_pages', 0)}\n"
        f"- Wiki 链接：{result.get('links', 0)}\n"
        f"- 已整理来源：{result.get('organized_sources', 0)}\n"
        "\n## 待连接页面\n\n"
        + ("\n".join(f"- [[{n['title']}]]" for n in orphans) if orphans else "- 暂无")
    )
    write_page(home_path, _replace_ai_block(content, ai), source_agent="wiki-maintenance")
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('last_maintenance',?)",
            (_now(),),
        )
        conn.commit()
    return {"success": True, **stats()}


class _WikiFileHandler(FileSystemEventHandler):
    def _queue(self, file_path: str, action: str) -> None:
        path = Path(file_path)
        if path.suffix.lower() != ".md" or path.name.startswith("."):
            return
        try:
            rel_path = _rel_from_path(path)
        except (ValueError, OSError):
            return
        now = time.time()
        key = f"{action}:{rel_path}"
        with _WIKI_EVENT_LOCK:
            if action != "deleted" and now - _WIKI_EVENT_TIMES.get(key, 0) < 1.0:
                return
            _WIKI_EVENT_TIMES[key] = now
            if len(_WIKI_EVENT_TIMES) > 2048:
                stale = [k for k, v in _WIKI_EVENT_TIMES.items() if now - v > 1.0]
                for k in stale:
                    _WIKI_EVENT_TIMES.pop(k, None)
        _POOL.submit(_handle_wiki_file_event, rel_path, action)

    def on_created(self, event):
        if not event.is_directory:
            self._queue(event.src_path, "changed")

    def on_modified(self, event):
        if not event.is_directory:
            self._queue(event.src_path, "changed")

    def on_deleted(self, event):
        if not event.is_directory:
            self._queue(event.src_path, "deleted")

    def on_moved(self, event):
        if not event.is_directory:
            self._queue(event.src_path, "deleted")
            self._queue(event.dest_path, "changed")


def _handle_wiki_file_event(rel_path: str, action: str) -> None:
    try:
        if action == "deleted":
            remove_page_from_gbrain(rel_path)
            logger.info("Wiki 删除已传播到 GBrain: %s", rel_path)
            return
        target = _resolve_rel_path(rel_path)
        if not target.is_file():
            return
        time.sleep(0.15)
        page = _refresh_page(rel_path, index_vectors=False)
        result = sync_page_to_gbrain(rel_path, page.get("content"))
        if not result.get("skipped"):
            logger.info("Wiki 变更已增量同步到 GBrain: %s", rel_path)
    except Exception as exc:
        logger.warning("Wiki 文件事件同步失败 %s: %s", rel_path, exc)


def start_wiki_watcher() -> None:
    global _WIKI_OBSERVER
    if _WIKI_OBSERVER is not None:
        return
    _ensure_wiki_dirs()
    observer = Observer()
    observer.schedule(_WikiFileHandler(), str(_wiki_root()), recursive=True)
    observer.start()
    _WIKI_OBSERVER = observer
    logger.info("Wiki→GBrain 增量监控已启动: %s", _wiki_root())


def start_maintenance_loop() -> None:
    initialize()
    start_wiki_watcher()

    def _initial_reconcile():
        time.sleep(3)
        try:
            result = reindex_all_wiki()
            logger.info(
                "Wiki/GBrain 启动对账完成: %s 页，待同步 %s",
                result.get("total_pages", 0),
                result.get("gbrain_pending", 0),
            )
        except Exception as exc:
            logger.warning("Wiki/GBrain 启动对账失败: %s", exc)

    threading.Thread(target=_initial_reconcile, daemon=True).start()

    def _loop():
        time.sleep(20)
        while True:
            try:
                today = date.today().isoformat()
                with _connect() as conn:
                    row = conn.execute("SELECT value FROM meta WHERE key='last_maintenance_date'").fetchone()
                if not row or row["value"] != today:
                    run_maintenance()
                    with _connect() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO meta(key,value) VALUES('last_maintenance_date',?)",
                            (today,),
                        )
                        conn.commit()
            except Exception as e:
                logger.warning(f"Wiki 自动维护失败: {e}")
            time.sleep(3600)

    threading.Thread(target=_loop, daemon=True).start()


def shutdown_pool() -> None:
    global _WIKI_OBSERVER
    if _WIKI_OBSERVER is not None:
        try:
            _WIKI_OBSERVER.stop()
            _WIKI_OBSERVER.join(timeout=3)
        except Exception:
            pass
        _WIKI_OBSERVER = None
    try:
        _POOL.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
