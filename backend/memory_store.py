"""Agent 记忆存储 — 基于 ChromaDB 向量存储 + 文件系统

对标 CCSwitch 的 workspace 记忆设计：
  SOUL.md     → 性格、价值观、表达风格与边界
  AGENTS.md   → Agent 行为规则与工作流程
  IDENTITY.md → 名称、角色、使命和自我定义
  USER.md     → 用户画像、偏好与上下文

所有记忆文件写入后自动向量化，支持语义搜索。
"""

import hashlib
import json
import logging
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from config import (
    MEMORY_COLLECTION,
    MEMORY_DIR,
    MEMORY_FILES,
    JOURNAL_DIR_NAME,
    MEMORY_CONTEXT_RECENT_DAYS,
    MEMORY_CONTEXT_CHAR_LIMIT,
)

logger = logging.getLogger(__name__)

# 在 Chroma 侧创建专属 collection（复用 vector_store 的 client）
_memory_collection = None
_COLLECTION_LOCK = threading.Lock()
# 全量重建互斥：客户端超时不会中止服务端重建，无锁时周期同步会叠加多个并发全量重建
_REINDEX_LOCK = threading.Lock()


def _content_hash(content: str) -> str:
    """与索引 metadata 中 content_hash 字段一致的内容指纹（跳过未变更文件的依据）"""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _get_memory_collection():
    """获取或创建记忆专属 ChromaDB collection"""
    from vector_store import ensure_index_readable, raise_if_index_storage_corruption

    ensure_index_readable()
    global _memory_collection
    if _memory_collection is None:
        with _COLLECTION_LOCK:
            if _memory_collection is None:
                try:
                    from vector_store import get_or_create_collection
                    _memory_collection = get_or_create_collection(MEMORY_COLLECTION)
                    logger.info(
                        f"Agent 记忆集合已就绪，条目数: {_memory_collection.count()}"
                    )
                except Exception as exc:
                    raise_if_index_storage_corruption(exc, "memory_collection")
                    raise
    return _memory_collection


def release_memory_collection():
    """释放记忆集合句柄（供空闲自动卸载调用；vector_store.release_chroma 会真正关 client）"""
    global _memory_collection
    with _COLLECTION_LOCK:
        _memory_collection = None


# ==================== 文件系统操作 ====================


def _ensure_memory_dirs():
    """确保记忆目录结构存在"""
    root = Path(MEMORY_DIR)
    root.mkdir(parents=True, exist_ok=True)
    journal_dir = root / JOURNAL_DIR_NAME
    journal_dir.mkdir(parents=True, exist_ok=True)
    # 创建默认记忆文件（如果不存在）
    for fname in MEMORY_FILES:
        fpath = root / fname
        if not fpath.exists():
            fpath.write_text(_default_content(fname), encoding="utf-8")
    return root


def _default_content(fname: str) -> str:
    """默认记忆文件模板"""
    if fname == "SOUL.md":
        return (
            "# SOUL.md — 人格与价值观\n\n"
            "定义所有 Agent 共享的性格、表达方式、价值判断与行为边界。\n\n"
            "## 核心价值观\n\n"
            "- 诚实，不确定时明确说明\n"
            "- 尊重用户意图和隐私\n"
            "- 以清晰、有帮助的方式交流\n\n"
            "## 表达风格\n\n"
            "- 默认使用中文\n"
            "- 直接、自然，避免空话\n\n"
            "## 边界\n\n"
            "- 不伪造事实或执行结果\n"
            "- 高风险或不可逆操作前确认\n"
        )
    if fname == "IDENTITY.md":
        return (
            "# IDENTITY.md — 身份定义\n\n"
            "定义所有 Agent 共享的名称、角色、使命和自我认知。\n\n"
            "- **名称**：（待填写）\n"
            "- **角色**：个人 AI 助手\n"
            "- **使命**：（待填写）\n"
            "- **与用户的关系**：长期、可信赖的协作伙伴\n"
        )
    if fname == "MEMORY.md":
        return (
            "# MEMORY.md — 长期记忆\n\n"
            "此文件是 Agent 的长期记忆，跨会话持久化。\n"
            "记录决策、偏好、关键上下文和学到的东西。\n\n"
            "## 用户偏好\n\n"
            "- 时区：GMT+8\n"
            "- 语言：中文\n\n"
            "## 项目上下文\n\n"
            "（记录正在进行的项目、重要决策、踩过的坑）\n\n"
            "## 技术栈\n\n"
            "（记录当前使用的技术、工具版本、配置要点）\n"
        )
    elif fname == "USER.md":
        return (
            "# USER.md — 用户画像\n\n"
            "此文件记录关于用户的信息，帮助所有 Agent 使用同一份用户上下文。\n\n"
            "- **称呼**：（待填写）\n"
            "- **时区**：GMT+8\n"
            "- **语言**：中文\n"
            "- **风格偏好**：（简洁/详细、直接/委婉）\n"
            "- **角色**：（开发者/管理者/学生...）\n"
            "- **关注领域**：（AI、编程、产品...）\n"
        )
    elif fname == "AGENTS.md":
        return (
            "# AGENTS.md — Agent 行为规则\n\n"
            "所有 Agent 应遵循的执行规则、工具约束和工作约定。\n\n"
            "## 核心原则\n\n"
            "- 简洁直接，不废话\n"
            "- 先做再说，不空谈计划\n"
            "- 遇到不确定的事主动确认\n\n"
            "## 工作约定\n\n"
            "- 文件操作前先读取确认内容\n"
            "- 破坏性操作前先确认\n"
            "- 优先复用已有方案\n"
        )
    return ""


def _metadata_value(raw: str):
    """Decode one small YAML-compatible scalar emitted by local importers."""
    value = raw.strip()
    try:
        return json.loads(value)
    except Exception:
        return value


def _frontmatter_metadata(content: str) -> dict:
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    metadata = {}
    for line in parts[1].splitlines():
        match = re.match(r"^([a-zA-Z0-9_]+):\s*(.*)$", line.strip())
        if match:
            metadata[match.group(1)] = _metadata_value(match.group(2))
    return metadata


def _timestamp_ms(value) -> int | None:
    if isinstance(value, (int, float)):
        return int(value if value > 1_000_000_000_000 else value * 1000)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(raw).timestamp() * 1000)
    except ValueError:
        return None


def list_memory_files() -> list[dict]:
    """列出所有记忆文件"""
    _ensure_memory_dirs()
    root = Path(MEMORY_DIR)
    files = []
    for fpath in sorted(root.rglob("*.md")):
        rel = fpath.relative_to(root).as_posix()
        stat = fpath.stat()
        item = {
            "path": rel,
            "size": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }
        if rel.startswith("conversations/"):
            try:
                head = fpath.read_text(encoding="utf-8", errors="replace")[:16_000]
                title_match = re.search(r"(?m)^#\s+(.+)$", head)
                metadata = _frontmatter_metadata(head)
                provider = str(metadata.get("provider") or "").strip()
                item["memory_type"] = "conversation"
                item["title"] = title_match.group(1).strip() if title_match else fpath.stem
                item["provider"] = provider or (Path(rel).parts[1] if len(Path(rel).parts) > 2 else "unknown")
                item["agent"] = item["provider"]
                item["occurred_at"] = (
                    _timestamp_ms(metadata.get("created_at"))
                    or _timestamp_ms(metadata.get("updated_at"))
                    or int(stat.st_mtime * 1000)
                )
                for key in ("owner_key", "user_id", "user_name", "user_email", "source_type"):
                    value = metadata.get(key)
                    if value is not None and str(value).strip():
                        item[key] = value
            except OSError:
                pass
        elif rel.startswith("imports/"):
            try:
                head = fpath.read_text(encoding="utf-8", errors="replace")[:8_000]
                title_match = re.search(r"(?m)^#\s+(.+)$", head)
                metadata = _frontmatter_metadata(head)
                if metadata.get("source") == "tokenmanager-memory":
                    agent = str(metadata.get("provider") or fpath.parent.name or "unknown")
                    item.update(
                        {
                            "memory_type": "agent_import",
                            "agent": agent,
                            "provider": agent,
                            "title": title_match.group(1).strip() if title_match else fpath.stem,
                            "occurred_at": (
                                _timestamp_ms(metadata.get("source_modified_at"))
                                or int(stat.st_mtime * 1000)
                            ),
                            "scope": metadata.get("scope"),
                            "kind": metadata.get("kind"),
                            "memory_id": metadata.get("memory_id"),
                        }
                    )
                    files.append(item)
                    continue
                agent_match = re.search(r"(?m)^\s*-\s*source_agent:\s*(.+)$", head)
                imported_match = re.search(r"(?m)^\s*-\s*imported_at:\s*(.+)$", head)
                agent = (
                    _metadata_value(agent_match.group(1)) if agent_match else fpath.stem
                )
                imported_at = (
                    _metadata_value(imported_match.group(1)) if imported_match else None
                )
                item.update(
                    {
                        "memory_type": "agent_import",
                        "agent": str(agent or fpath.stem),
                        "provider": str(agent or fpath.stem),
                        "title": title_match.group(1).strip() if title_match else fpath.stem,
                        "occurred_at": _timestamp_ms(imported_at) or int(stat.st_mtime * 1000),
                    }
                )
            except OSError:
                pass
        files.append(item)
    return files


def read_memory_file(rel_path: str) -> Optional[dict]:
    """读取记忆文件内容"""
    root = Path(MEMORY_DIR)
    fpath = (root / rel_path).resolve()
    if not fpath.is_relative_to(root) or not fpath.exists():
        return None
    content = fpath.read_text(encoding="utf-8")
    stat = fpath.stat()
    return {
        "path": rel_path,
        "content": content,
        "size": stat.st_size,
        "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


def write_memory_file(rel_path: str, content: str, source_agent: str = "manual", *, skip_index: bool = False) -> dict:
    """写入记忆文件并自动向量化"""
    _ensure_memory_dirs()
    root = Path(MEMORY_DIR)
    fpath = (root / rel_path).resolve()
    if not fpath.is_relative_to(root):
        raise ValueError(f"非法路径: {rel_path}")
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(content, encoding="utf-8")

    # 向量化（批量导入时可跳过，由调用方统一重建）
    if not skip_index:
        _index_memory_file(str(fpath), rel_path, content, source_agent)

    stat = fpath.stat()
    return {
        "path": rel_path,
        "size": stat.st_size,
        "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "indexed": True,
    }


def delete_memory_file(rel_path: str) -> bool:
    """删除记忆文件及其向量"""
    root = Path(MEMORY_DIR)
    fpath = (root / rel_path).resolve()
    if not fpath.is_relative_to(root) or not fpath.exists():
        return False

    # 删向量
    try:
        col = _get_memory_collection()
        col.delete(where={"source_path": str(fpath)})
    except Exception as e:
        from vector_store import record_index_operation_failure
        record_index_operation_failure(e, "memory_delete")
        logger.error(f"删除记忆向量失败: {e}")

    fpath.unlink()
    return True


# ==================== 日记操作 ====================


def list_journals(from_date: Optional[str] = None, to_date: Optional[str] = None) -> list[dict]:
    """列出日记文件"""
    _ensure_memory_dirs()
    journal_dir = Path(MEMORY_DIR) / JOURNAL_DIR_NAME
    journals = []
    for fpath in sorted(journal_dir.glob("*.md"), reverse=True):
        fname = fpath.stem
        try:
            date.fromisoformat(fname)
        except ValueError:
            continue
        if from_date and fname < from_date:
            continue
        if to_date and fname > to_date:
            continue
        stat = fpath.stat()
        journals.append({
            "date": fname,
            "size": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return journals


def read_journal(journal_date: str) -> Optional[dict]:
    """读取指定日期的日记"""
    fname = f"{journal_date}.md"
    return read_memory_file(f"{JOURNAL_DIR_NAME}/{fname}")


def write_journal(journal_date: str, content: str, source_agent: str = "manual") -> dict:
    """写入日记并向量化"""
    try:
        date.fromisoformat(journal_date)
    except ValueError:
        raise ValueError(f"无效日期格式: {journal_date}")
    fname = f"{journal_date}.md"
    return write_memory_file(f"{JOURNAL_DIR_NAME}/{fname}", content, source_agent)


def get_recent_journals(days: int | None = None) -> str:
    """获取最近 N 天日记内容（合并为一段文本，供上下文注入）"""
    if days is None:
        days = MEMORY_CONTEXT_RECENT_DAYS
    today = date.today()
    parts = []
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        result = read_journal(d)
        if result and result.get("content", "").strip():
            parts.append(f"## {d}\n{result['content']}")
    return "\n\n".join(parts)


def _agent_import_paths(agent: str) -> list[str]:
    """Return legacy and TokenManager-managed imports for one safe agent ID."""
    normalized = (agent or "").strip().lower()
    if not normalized or normalized in {"default", "shared", "global"}:
        return []
    safe = "".join(c for c in normalized if c.isalnum() or c in {"-", "_"})
    if not safe:
        return []
    paths = []
    legacy = Path(MEMORY_DIR) / "imports" / f"{safe}.md"
    if legacy.is_file():
        paths.append(f"imports/{safe}.md")
    managed = Path(MEMORY_DIR) / "imports" / "tokenmanager" / safe
    if managed.is_dir():
        paths.extend(
            path.relative_to(Path(MEMORY_DIR)).as_posix()
            for path in sorted(managed.glob("*.md"))
            if path.is_file()
        )
    return paths


def _agent_import_content(agent: str) -> str | None:
    parts = []
    for rel_path in _agent_import_paths(agent):
        result = read_memory_file(rel_path)
        if result and result.get("content", "").strip():
            parts.append(result["content"])
    return "\n\n---\n\n".join(parts) if parts else None


# ==================== 向量化 ====================


def index_memory_file(full_path: str, rel_path: str, content: str, source_agent: str):
    """将记忆文件内容分块向量化存入 ChromaDB（公开接口，供批量导入后单独调用）"""
    _index_memory_file(full_path, rel_path, content, source_agent)


def _index_memory_file(full_path: str, rel_path: str, content: str, source_agent: str):
    from embedder import embed_query
    from vector_store import ensure_index_writable, raise_if_index_storage_corruption

    ensure_index_writable()
    col = _get_memory_collection()
    # 删除旧向量
    try:
        col.delete(where={"source_path": full_path})
    except Exception as exc:
        raise_if_index_storage_corruption(exc, "memory_replace_delete")
        raise

    if not content.strip():
        return

    # 按段落/消息边界分块；超长工具输出继续切片，避免只索引前 2000 字。
    raw_paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    paragraphs = []
    for paragraph in raw_paragraphs:
        if len(paragraph) <= 1800:
            paragraphs.append(paragraph)
            continue
        start = 0
        while start < len(paragraph):
            paragraphs.append(paragraph[start:start + 1800])
            if start + 1800 >= len(paragraph):
                break
            start += 1700
    if not paragraphs:
        paragraphs = [content[:2000]]

    chunks = []
    embeddings = []
    metadatas = []
    ids = []

    # 推断记忆类型
    if "/journal/" in rel_path or rel_path.startswith("journal/"):
        mem_type = "journal"
        journal_date = Path(rel_path).stem
    elif rel_path == "MEMORY.md":
        mem_type = "long_term"
        journal_date = ""
    elif rel_path == "USER.md":
        mem_type = "user_profile"
        journal_date = ""
    elif rel_path == "AGENTS.md":
        mem_type = "agents_rules"
        journal_date = ""
    elif rel_path == "SOUL.md":
        mem_type = "identity_soul"
        journal_date = ""
    elif rel_path == "IDENTITY.md":
        mem_type = "identity_profile"
        journal_date = ""
    elif rel_path.startswith("conversations/"):
        mem_type = "conversation"
        journal_date = ""
    else:
        mem_type = "custom"
        journal_date = ""

    content_hash = _content_hash(content)

    for i, para in enumerate(paragraphs):
        # 截断过长段落
        text = para[:1800]
        emb = embed_query(text)
        if not emb:
            continue
        chunks.append(text)
        embeddings.append(emb)
        metadata = {
            "source_path": full_path,
            "rel_path": rel_path,
            "memory_type": mem_type,
            "date": journal_date,
            "source_agent": source_agent,
            "chunk_index": i,
            "content_hash": content_hash,
        }
        if mem_type == "conversation":
            frontmatter = content.split("---", 2)[1] if content.startswith("---") and content.count("---") >= 2 else ""
            for key in (
                "conversation_id",
                "provider",
                "source_type",
                "status",
                "created_at",
                "updated_at",
                "tokenmanager_revision",
                "owner_key",
                "user_id",
                "user_name",
                "user_email",
            ):
                match = re.search(rf"(?m)^{key}:\s*(.+)$", frontmatter)
                if match:
                    raw = match.group(1).strip()
                    try:
                        value = json.loads(raw)
                    except Exception:
                        value = raw
                    if isinstance(value, (str, int, float, bool)):
                        metadata[key] = value
        metadatas.append(metadata)
        ids.append(f"{full_path}::chunk::{i}")

    if chunks:
        # chroma 单批上限 5461 条（sqlite 变量数限制），超大文件需分批写入
        batch = 5000
        for start in range(0, len(chunks), batch):
            end = start + batch
            try:
                col.add(
                    ids=ids[start:end],
                    embeddings=embeddings[start:end],
                    documents=chunks[start:end],
                    metadatas=metadatas[start:end],
                )
            except Exception as exc:
                raise_if_index_storage_corruption(exc, "memory_add")
                raise
        logger.info(f"记忆已索引: {rel_path} ({len(chunks)} 块)")


def _load_indexed_hashes(col) -> dict:
    """分页拉取全量 metadata，构建 source_path → content_hash 映射。

    只走 sqlite metadata segment，不触碰 HNSW 索引，避免空跑 reindex
    把整个向量索引页进内存。同一文件各块 hash 相同，任取其一即可。
    """
    hashes: dict = {}
    offset = 0
    page = 5000
    while True:
        batch = col.get(include=["metadatas"], limit=page, offset=offset)
        metas = batch.get("metadatas") or []
        if not metas:
            break
        for meta in metas:
            path = (meta or {}).get("source_path")
            if path and path not in hashes:
                hashes[path] = meta.get("content_hash")
        if len(metas) < page:
            break
        offset += len(metas)
    return hashes


def reindex_all_memory():
    """增量重建记忆向量索引：内容未变的文件跳过；磁盘已删的文件清理孤儿向量"""
    if not _REINDEX_LOCK.acquire(blocking=False):
        logger.info("记忆重建已在进行中，跳过本次请求")
        return {"files": 0, "indexed": 0, "skipped": 0, "orphans_removed": 0,
                "already_running": True}
    try:
        _ensure_memory_dirs()
        col = _get_memory_collection()
        indexed_hashes = _load_indexed_hashes(col)
        root = Path(MEMORY_DIR)
        files = sorted(root.rglob("*.md"))
        on_disk = set()
        indexed = 0
        skipped = 0
        for fpath in files:
            full = str(fpath)
            on_disk.add(full)
            content = fpath.read_text(encoding="utf-8")
            existing = indexed_hashes.get(full)
            if existing is not None and existing == _content_hash(content):
                skipped += 1
                continue
            rel = str(fpath.relative_to(root))
            _index_memory_file(full, rel, content, source_agent="reindex")
            indexed += 1
        # 防御性限定记忆目录前缀，避免误删其他来源写入的向量
        memory_prefix = str(root) + "/"
        orphans = [p for p in indexed_hashes
                   if p not in on_disk and p.startswith(memory_prefix)]
        for path in orphans:
            try:
                col.delete(where={"source_path": path})
            except Exception as exc:
                from vector_store import record_index_operation_failure
                record_index_operation_failure(exc, "memory_orphan_delete")
                logger.warning(f"孤儿向量清理失败: {path}", exc_info=True)
        logger.info(
            f"记忆重建完成: {len(files)} 个文件"
            f"（重建 {indexed}，跳过 {skipped}，清理孤儿 {len(orphans)}）"
        )
        return {"files": len(files), "indexed": indexed, "skipped": skipped,
                "orphans_removed": len(orphans)}
    finally:
        _REINDEX_LOCK.release()


# ==================== 语义搜索 ====================


def search_memory(
    query: str,
    n_results: int = 10,
    memory_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    identity_only: bool = False,
) -> list[dict]:
    """语义搜索记忆内容"""
    from embedder import embed_query

    qv = embed_query(query)
    if not qv:
        return []

    from vector_store import ensure_index_readable, raise_if_index_storage_corruption

    ensure_index_readable()
    # 类型筛选
    where = None
    if memory_type:
        where = {"memory_type": memory_type}

    try:
        from vector_store import query_union_collection
        results = query_union_collection(MEMORY_COLLECTION, qv, n_results * 3, where=where)
    except Exception as exc:
        raise_if_index_storage_corruption(exc, "memory_query")
        raise

    items = []
    if results["ids"] and results["ids"][0]:
        for i, chunk_id in enumerate(results["ids"][0]):
            metas = results.get("metadatas")
            meta = (metas[0][i] if metas and metas[0] and i < len(metas[0]) else None) or {}
            if identity_only and meta.get("rel_path", "") not in MEMORY_FILES:
                continue
            dists = results.get("distances")
            dist = dists[0][i] if dists and dists[0] and i < len(dists[0]) else 0.0
            score = 1.0 - dist

            # 日期区间过滤
            d = meta.get("date", "")
            if date_from and d < date_from:
                continue
            if date_to and d > date_to:
                continue

            items.append({
                "id": chunk_id,
                "rel_path": meta.get("rel_path", ""),
                "memory_type": meta.get("memory_type", ""),
                "date": d,
                "text": (results["documents"][0][i] if results["documents"][0] else "")[:500],
                "score": round(score, 4),
                "source_agent": meta.get("source_agent", ""),
                "conversation_id": meta.get("conversation_id", ""),
                "provider": meta.get("provider", ""),
            })

    # 去重并截断
    seen = set()
    unique = []
    for item in items:
        key = (item["rel_path"], item["text"][:100])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    unique.sort(key=lambda x: x["score"], reverse=True)
    return unique[:n_results]


# ==================== Agent 上下文注入 ====================

# 核心文件 → imports 章节名映射（当核心文件为模板时从 imports 提取替代内容）
_CORE_TO_IMPORT_SECTION: dict[str, list[str]] = {
    "IDENTITY.md": ["IDENTITY.md", "identity"],
    "SOUL.md": ["SOUL.md", "soul"],
    "AGENTS.md": ["AGENTS.md", "rules"],
    "USER.md":   ["USER.md", "user profile", "用户画像"],
    "MEMORY.md": ["MEMORY.md", "long-term memory", "长期记忆"],
}

# 模板占位符：只要核心文件内容中出现任一关键词，即判定为模板状态
_TEMPLATE_PLACEHOLDERS = [
    "待填写", "（待填写）", "（开发者/管理者/学生...）",
    "（AI、编程、产品...）", "（简洁/详细、直接/委婉）",
    "此文件记录关于用户的信息",  # USER.md 模板首句
    "此文件是 Agent 的长期记忆",  # MEMORY.md 模板首句
]

_TEMPLATE_MIN_CONTENT_CHARS = 150  # 低于此长度的内容视为模板


def _is_template_content(content: str) -> bool:
    """检测记忆文件内容是否为模板/空壳状态。"""
    stripped = content.strip()
    if len(stripped) < _TEMPLATE_MIN_CONTENT_CHARS:
        return True
    for ph in _TEMPLATE_PLACEHOLDERS:
        if ph in stripped:
            return True
    return False


def _extract_import_section(import_content: str, section_hints: list[str]) -> str | None:
    """从 imports 文件中提取匹配 section_hints 的章节内容。

    匹配规则：imports 文件中以 `## Source: ...` 开头的章节，若章节名包含
    section_hints 中任一关键词，则提取该章节文本（到下一个 `## Source:` 或 `---` 或文末）。
    """
    import re

    # 将 imports 按 ## Source: 分割成章节
    sections = re.split(r"\n(?=## Source:)", import_content)
    for section in sections:
        header_line = section.split("\n")[0] if section else ""
        header_lower = header_line.lower()
        # 跳过文件头部元信息（不以 ## Source: 开头）
        if not header_line.startswith("## Source:"):
            continue
        for hint in section_hints:
            if hint.lower() in header_lower:
                # 提取该章节内容：去掉 header 行，截到下一个 ## Source: 或末尾
                body = section[len(header_line):].strip()
                # 去掉尾部可能的分隔线
                body = re.sub(r"\n?---\s*$", "", body)
                if body:
                    return body
                return None
    return None


def get_context(agent: str = "default", limit_chars: int | None = None) -> dict:
    """生成供 Agent 启动注入的上下文文本。

    合并顺序：IDENTITY.md → SOUL.md → USER.md → AGENTS.md → 旧长期记忆、Agent 导入与日记。
    总长度不超过 limit_chars。

    智能降级：当核心身份文件内容仍为模板
    状态时，自动从 agent 专属 imports 中提取对应章节替代，避免空白/模板内容
    污染上下文窗口。
    """
    import re

    if limit_chars is None:
        limit_chars = MEMORY_CONTEXT_CHAR_LIMIT

    _ensure_memory_dirs()
    parts = []
    total = 0

    def append_text(text: str) -> bool:
        nonlocal total
        if not text.strip():
            return True
        if total + len(text) > limit_chars:
            remaining = limit_chars - total - 100
            if remaining > 200:
                text = text[:remaining] + "\n\n（内容已截断以适配上下文窗口）"
            else:
                return False
        parts.append(text)
        total += len(text)
        return True

    # 预先读取 agent imports（核心文件降级时需要）
    import_content = _agent_import_content(agent)

    # 按优先级读取核心文件
    core_paths = ["IDENTITY.md", "SOUL.md", "USER.md", "AGENTS.md"]
    if (Path(MEMORY_DIR) / "MEMORY.md").is_file():
        core_paths.append("MEMORY.md")
    for fname in core_paths:
        result = read_memory_file(fname)
        raw_content = result.get("content", "") if result else ""

        if _is_template_content(raw_content) and import_content:
            # 核心文件为模板 → 从 imports 提取替代内容
            hints = _CORE_TO_IMPORT_SECTION.get(fname, [fname])
            extracted = _extract_import_section(import_content, hints)
            if extracted:
                # 清理可能残留的 markdown 转义/格式噪声
                cleaned = re.sub(r"\n?---\s*$", "", extracted).strip()
                header = f"# {fname.replace('.md', '')}（从 {agent} 记忆自动提取）\n\n"
                raw_content = header + cleaned
                logger.info(
                    "get_context: %s 为模板状态，已从 imports/%s.md 提取 %d 字符替代",
                    fname, agent, len(cleaned),
                )

        if raw_content.strip():
            if not append_text(raw_content):
                break

    # Agent 专属导入记忆（完整追加，核心文件已提取过的章节仍保留供搜索）
    if import_content and import_content.strip():
        append_text(import_content)

    # 最近日记
    recent = get_recent_journals(MEMORY_CONTEXT_RECENT_DAYS)
    if recent:
        append_text(recent)

    context = "\n\n---\n\n".join(parts)
    return {
        "agent": agent,
        "total_chars": len(context),
        "limit_chars": limit_chars,
        "context": context,
    }


# 启动时确保目录和默认文件存在
_ensure_memory_dirs()
