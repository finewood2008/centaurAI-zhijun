"""独立的本地 GBrain 适配层。

半人马知识库使用自己的 ``GBRAIN_HOME``，不复用任何 Agent 的进程或数据库。
所有 GBrain 调用串行化，避免 PGLite 被并发的一次性 CLI 进程争用。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from config import PROJECT_ROOT, WIKI_DIR
from runtime_paths import GBRAIN_HOME


GBRAIN_DATA = GBRAIN_HOME / "brain.pglite"
GBRAIN_CONFIG = GBRAIN_HOME / ".gbrain" / "config.json"
EXPECTED_EMBEDDING_MODEL = os.getenv("CENTAUR_GBRAIN_EMBEDDING_MODEL", "ollama:bge-m3")
WIKI_ROOT = Path(WIKI_DIR).resolve()

_COMMAND_LOCK = threading.RLock()


class GBrainError(RuntimeError):
    """GBrain command/configuration failure safe to surface to the local UI."""


def _find_cli() -> str:
    configured = os.getenv("GBRAIN_BIN", "").strip()
    candidates = [configured, str(Path.home() / ".bun" / "bin" / "gbrain"), shutil.which("gbrain") or ""]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    raise GBrainError("未找到 GBrain CLI，请先安装 gbrain")


def _command_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GBRAIN_HOME"] = str(GBRAIN_HOME)
    env["GBRAIN_SKIP_STARTUP_HOOKS"] = "1"
    # systemd 用户服务通常没有交互 shell 的 ~/.bun/bin；GBrain 的 shebang
    # 通过 /usr/bin/env 查找 bun，因此在这里显式补齐而不依赖登录环境。
    bun_bin = str(Path.home() / ".bun" / "bin")
    env["PATH"] = bun_bin + os.pathsep + env.get("PATH", "")
    # 数据库位置只允许来自半人马自己的 config.json，避免父进程变量把它指向别的脑库。
    env.pop("GBRAIN_DATABASE_URL", None)
    env.pop("DATABASE_URL", None)
    return env


def _decode_json_output(stdout: str) -> Any:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # init/import 可能先输出进度，再在末尾输出 JSON；从后向前找最后一个完整值。
    decoder = json.JSONDecoder()
    starts = [i for i, char in enumerate(text) if char in "[{"]
    for start in reversed(starts):
        try:
            value, end = decoder.raw_decode(text[start:])
            if not text[start + end :].strip():
                return value
        except json.JSONDecodeError:
            continue
    raise GBrainError("GBrain 返回了无法解析的数据")


def _run(args: list[str], timeout: int = 45, extra_env: dict[str, str] | None = None) -> Any:
    cli = _find_cli()
    GBRAIN_HOME.mkdir(parents=True, exist_ok=True)
    command_env = _command_env()
    command_env.update(extra_env or {})
    with _COMMAND_LOCK:
        try:
            proc = subprocess.run(
                [cli, *args],
                cwd=str(PROJECT_ROOT),
                env=command_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GBrainError(f"GBrain 操作超时（{timeout} 秒）") from exc
        except OSError as exc:
            raise GBrainError(f"无法启动 GBrain：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "GBrain 操作失败").strip()
        detail = detail[-1200:]
        raise GBrainError(detail)
    return _decode_json_output(proc.stdout)


def _call(tool: str, params: dict[str, Any] | None = None, timeout: int = 45) -> Any:
    return _run(["call", tool, json.dumps(params or {}, ensure_ascii=False, separators=(",", ":"))], timeout)


def _read_config() -> dict[str, Any]:
    try:
        data = json.loads(GBRAIN_CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def initialize() -> dict[str, Any]:
    """Create the app-owned brain when first used; idempotent afterwards."""
    if GBRAIN_CONFIG.is_file():
        return _read_config()
    _run(
        [
            "init", "--pglite", "--path", str(GBRAIN_DATA),
            "--embedding-model", EXPECTED_EMBEDDING_MODEL,
            "--embedding-dimensions", "1024", "--non-interactive",
            "--skip-embed-check", "--json",
        ],
        timeout=180,
    )
    config = _read_config()
    if not config:
        raise GBrainError("GBrain 初始化完成，但配置文件不可读")
    return config


def _ollama_status(model: str) -> dict[str, Any]:
    model_name = model.split(":", 1)[1] if model.startswith("ollama:") else ""
    if not model_name:
        return {"reachable": False, "model_available": False, "error": "当前不是 Ollama 本地模型"}
    try:
        req = Request("http://127.0.0.1:11434/api/tags", headers={"Accept": "application/json"})
        with urlopen(req, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        names = [str(item.get("name") or "") for item in payload.get("models", [])]
        available = any(name == model_name or name.split(":", 1)[0] == model_name.split(":", 1)[0] for name in names)
        return {"reachable": True, "model_available": available, "models": names}
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return {"reachable": False, "model_available": False, "error": str(exc)}


def _assert_local_embeddings() -> dict[str, Any]:
    config = initialize()
    model = str(config.get("embedding_model") or "")
    if not model.startswith("ollama:"):
        raise GBrainError("已阻止操作：GBrain 向量模型不是本地 Ollama 模型")
    ollama = _ollama_status(model)
    if not ollama.get("reachable"):
        raise GBrainError("本地 Ollama 服务不可用")
    if not ollama.get("model_available"):
        raise GBrainError(f"本地 Ollama 未安装向量模型 {model.split(':', 1)[1]}")
    return config


def status() -> dict[str, Any]:
    """Return a non-throwing dashboard envelope so the page can explain setup failures."""
    try:
        config = initialize()
        model = str(config.get("embedding_model") or "")
        ollama = _ollama_status(model)
        stats = _call("get_stats") or {}
        try:
            health = _call("get_health") or {}
        except GBrainError as exc:
            health = {"error": str(exc)}
        local = model.startswith("ollama:")
        ready = bool(local and ollama.get("reachable") and ollama.get("model_available"))
        return {
            "available": True,
            "ready": ready,
            "independent": True,
            "source_of_truth": "wiki",
            "role": "derived_knowledge_engine",
            "engine": config.get("engine", "pglite"),
            "embedding_model": model,
            "embedding_dimensions": config.get("embedding_dimensions"),
            "local_embeddings": local,
            "database_path": str(config.get("database_path") or GBRAIN_DATA),
            "ollama": ollama,
            "stats": stats,
            "health": health,
        }
    except GBrainError as exc:
        return {
            "available": False,
            "ready": False,
            "independent": True,
            "source_of_truth": "wiki",
            "role": "derived_knowledge_engine",
            "error": str(exc),
        }


def list_pages(page_type: str = "", tag: str = "", limit: int = 100) -> dict[str, Any]:
    initialize()
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 100)), "sort": "updated_desc"}
    if page_type:
        params["type"] = page_type
    if tag:
        params["tag"] = tag
    result = _call("list_pages", params) or []
    items = result if isinstance(result, list) else result.get("items", [])
    return {"items": items, "total": len(items)}


def get_page(slug: str) -> dict[str, Any]:
    initialize()
    result = _call("get_page", {"slug": slug, "fuzzy": False})
    if not isinstance(result, dict):
        raise GBrainError("GBrain 页面返回格式错误")
    return result


def search_pages(query_text: str, mode: str = "hybrid", limit: int = 12) -> dict[str, Any]:
    query_text = query_text.strip()
    if not query_text:
        raise GBrainError("查询内容为空")
    _assert_local_embeddings()
    limit = max(1, min(int(limit), 30))
    if mode == "keyword":
        result = _call("search", {"query": query_text, "limit": limit}, timeout=90)
    elif mode == "hybrid":
        result = _call(
            "query",
            {"query": query_text, "limit": limit, "expand": False, "detail": "medium", "mode": "conservative"},
            timeout=120,
        )
    else:
        raise GBrainError("不支持的 GBrain 检索模式")
    items = result if isinstance(result, list) else (result or {}).get("results", [])
    return {"query": query_text, "mode": mode, "items": items, "total": len(items)}


def _slugify(title: str, page_type: str) -> str:
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", title.strip()).strip("-").lower()
    value = value[:72] or f"note-{uuid.uuid4().hex[:10]}"
    prefixes = {"concept": "concepts", "project": "projects", "person": "people", "company": "companies"}
    return f"{prefixes.get(page_type, 'notes')}/{value}"


def slug_for_wiki_path(rel_path: str) -> str:
    """Return the canonical GBrain slug for one Wiki Markdown path."""
    rel = Path(str(rel_path).replace("\\", "/"))
    if rel.suffix.lower() != ".md" or rel.is_absolute() or ".." in rel.parts:
        raise GBrainError("非法 Wiki 页面路径")
    parts: list[str] = []
    for raw in rel.with_suffix("").parts:
        value = unicodedata.normalize("NFKC", raw).strip().lower()
        value = re.sub(r"\s+", "-", value)
        value = re.sub(r"[^0-9a-z\u4e00-\u9fff_-]+", "", value)
        value = re.sub(r"-+", "-", value).strip("-_")
        if value:
            parts.append(value)
    if not parts:
        raise GBrainError("Wiki 页面路径无法生成 GBrain slug")
    return "/".join(parts)


def sync_wiki_page(rel_path: str, content: str | None = None) -> dict[str, Any]:
    """Upsert one Wiki source page into the derived GBrain index."""
    _assert_local_embeddings()
    target = (WIKI_ROOT / rel_path).resolve()
    if not target.is_relative_to(WIKI_ROOT) or target.suffix.lower() != ".md":
        raise GBrainError("非法 Wiki 页面路径")
    if content is None:
        if not target.is_file():
            raise GBrainError("Wiki 页面不存在")
        content = target.read_text(encoding="utf-8", errors="replace")
    slug = slug_for_wiki_path(rel_path)
    result = _call(
        "put_page",
        {
            "slug": slug,
            "content": content,
            "source_kind": "centaur-wiki",
            "source_uri": str(target),
            "ingested_via": "centaur-wiki-sync",
        },
        timeout=180,
    )
    return {"success": True, "slug": slug, "page": result}


def delete_wiki_page(rel_path: str) -> dict[str, Any]:
    """Soft-delete a removed Wiki page from the derived GBrain index."""
    initialize()
    slug = slug_for_wiki_path(rel_path)
    try:
        result = _call("delete_page", {"slug": slug})
    except GBrainError as exc:
        message = str(exc).lower()
        if "not found" not in message and "page_not_found" not in message:
            raise
        result = {"status": "not_found"}
    return {"success": True, "slug": slug, "result": result}


def put_page(title: str, content: str, page_type: str = "note", tags: list[str] | None = None, slug: str = "") -> dict[str, Any]:
    title = title.strip()
    content = content.strip()
    if not title:
        raise GBrainError("标题不能为空")
    if not content:
        raise GBrainError("内容不能为空")
    if len(content) > 300_000:
        raise GBrainError("单页内容不能超过 30 万字符")
    _assert_local_embeddings()
    allowed_types = {"note", "concept", "project", "person", "company", "meeting", "decision", "media"}
    page_type = page_type if page_type in allowed_types else "note"
    safe_tags = [str(tag).strip()[:64] for tag in (tags or []) if str(tag).strip()][:20]
    slug = slug.strip().strip("/") or _slugify(title, page_type)
    frontmatter = [
        "---",
        "title: " + json.dumps(title, ensure_ascii=False),
        "type: " + page_type,
        "tags: " + json.dumps(safe_tags, ensure_ascii=False),
        "source: centaur-wiki",
        "---",
        "",
    ]
    markdown = "\n".join(frontmatter) + content
    result = _call(
        "put_page",
        {
            "slug": slug,
            "content": markdown,
            "source_kind": "centaur-wiki",
            "source_uri": f"centaur://wiki/gbrain/{slug}",
            "ingested_via": "centaur-vector-db-ui",
        },
        timeout=180,
    )
    return {"success": True, "slug": slug, "page": result}


def graph(slug: str, depth: int = 2) -> dict[str, Any]:
    initialize()
    result = _call("traverse_graph", {"slug": slug, "depth": max(1, min(int(depth), 4))}) or []
    nodes = result if isinstance(result, list) else result.get("nodes", [])
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        source = str(node.get("slug") or "")
        for link in node.get("links", []) or []:
            target = str(link.get("to_slug") or "")
            kind = str(link.get("link_type") or "link")
            key = (source, target, kind)
            if source and target and key not in seen:
                seen.add(key)
                edges.append({"source": source, "target": target, "type": kind})
    # 旧 Wiki 多为平面页面，尚未沉淀显式边。此时用本地向量近邻生成“语义关系”，
    # 让图谱从首轮同步起就有可浏览价值；有显式边后则始终优先真实关系。
    if len(nodes) <= 1:
        root = nodes[0] if nodes else {"slug": slug, "title": slug.rsplit("/", 1)[-1], "type": "note", "depth": 0, "links": []}
        related = _call(
            "query",
            {
                "query": str(root.get("title") or slug),
                "limit": 7,
                "expand": False,
                "detail": "low",
                "mode": "conservative",
            },
            timeout=120,
        ) or []
        related_items = related if isinstance(related, list) else related.get("results", [])
        nodes = [root]
        for item in related_items:
            target = str(item.get("slug") or "") if isinstance(item, dict) else ""
            if not target or target == slug or any(node.get("slug") == target for node in nodes):
                continue
            nodes.append(
                {
                    "slug": target,
                    "title": item.get("title") or target,
                    "type": item.get("type") or "note",
                    "depth": 1,
                    "links": [],
                    "score": item.get("score"),
                }
            )
            edges.append({"source": slug, "target": target, "type": "semantic"})
    return {"nodes": nodes, "edges": edges}


def sync_wiki() -> dict[str, Any]:
    """Upsert all current Wiki markdown pages and embed them with local Ollama."""
    _assert_local_embeddings()
    if not WIKI_ROOT.is_dir():
        raise GBrainError("Wiki 目录不存在")
    started = datetime.now().astimezone().isoformat()
    # Wiki 是运行数据，按设计在项目 .gitignore 中。GBrain 的导入器在 Git 仓库内
    # 默认尊重 .gitignore，因此显式把仓库根设为 Git 搜索上限，让它走安全的文件遍历器。
    result = _run(
        ["import", str(WIKI_ROOT), "--workers", "1", "--fresh", "--json"],
        timeout=1800,
        extra_env={"GIT_CEILING_DIRECTORIES": str(Path(PROJECT_ROOT).resolve())},
    )
    return {"success": True, "started_at": started, "result": result}
