"""Incrementally import TokenManager conversations into personal memory.

TokenManager remains the authority for parsing Agent-native history.  This
module consumes its loopback-only, read-only API and persists inspectable
Markdown plus derived vectors.  Source deletion is intentionally not mirrored:
personal memory is a long-term archive and requires an explicit local delete.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

import memory_store
from config import MEMORY_DIR
from runtime_paths import TOKENMANAGER_CONFIG_DIR

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:15722"
DEFAULT_INTERVAL_SECONDS = 3600
MAX_PAGES_PER_RUN = 20
PAGE_SIZE = 50
MAX_LLM_SUMMARIES_PER_RUN = 3
CONFIG_DIR = TOKENMANAGER_CONFIG_DIR
CONFIG_PATH = CONFIG_DIR / "tokenmanager-sync.json"
IDENTITY_STATE_PATH = CONFIG_DIR / "tokenmanager-identity-state.json"
CONVERSATION_DIR = Path(MEMORY_DIR) / "conversations"
MEMORY_IMPORT_DIR = Path(MEMORY_DIR) / "imports" / "tokenmanager"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
SUMMARY_MODEL = os.environ.get("CENTAUR_MEMORY_AI_MODEL", "qwen3:1.7b")
SYNC_SCHEMA_VERSION = 3

_SYNC_LOCK = threading.Lock()
_IDENTITY_LOCK = threading.Lock()
_RUNTIME_LOCK = threading.Lock()
# 本批次新增的文件路径 (full_path, rel_path)，同步完成后统一索引
_NEW_FILES: list[tuple[str, str]] = []
_NEW_FILES_LOCK = threading.Lock()
_RUNTIME = {
    "running": False,
    "last_started_at": None,
    "last_completed_at": None,
    "last_imported": 0,
    "last_skipped": 0,
    "last_failed": 0,
    "last_error": None,
    "last_conversation_imported": 0,
    "last_memory_imported": 0,
    "last_memory_deleted": 0,
    "memory_api_supported": None,
    "capabilities": [],
    "sync_mode": "unknown",
    "fallback_reason": None,
    "identity_running": False,
    "identity_last_revision": None,
    "identity_last_completed_at": None,
    "identity_last_error": None,
    "identity_last_result": None,
}


def _default_config() -> dict:
    return {
        "enabled": False,
        "url": DEFAULT_URL,
        "token": "",
        "cursor": "",
        "conversation_cursor": "",
        "memory_cursor": "",
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        # Zero deliberately means "not backfilled yet". Existing configs did
        # not persist user metadata in conversation Markdown, so the next sync
        # replays the read-only feed once and upgrades those files in place.
        "schema_version": 0,
    }


def _load_config() -> dict:
    config = _default_config()
    if CONFIG_PATH.is_file():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                config.update(raw)
        except (OSError, ValueError) as exc:
            logger.warning("读取 TokenManager 同步配置失败: %s", exc)
    if not str(config.get("conversation_cursor") or ""):
        config["conversation_cursor"] = str(config.get("cursor") or "")
    config["cursor"] = str(config.get("conversation_cursor") or "")
    return config


def _atomic_save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass
    temporary = CONFIG_PATH.with_suffix(".tmp")
    payload = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CONFIG_PATH)
        try:
            CONFIG_PATH.chmod(0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _load_identity_state() -> dict:
    state = {
        "pending_revision": None,
        "last_revision": None,
        "last_completed_at": None,
        "last_result": None,
        "last_error": None,
    }
    if IDENTITY_STATE_PATH.is_file():
        try:
            raw = json.loads(IDENTITY_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update(raw)
        except (OSError, ValueError) as exc:
            logger.warning("读取身份同步状态失败: %s", exc)
    return state


def _atomic_save_identity_state(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass
    temporary = IDENTITY_STATE_PATH.with_suffix(".tmp")
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, IDENTITY_STATE_PATH)
        try:
            IDENTITY_STATE_PATH.chmod(0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _validated_url(value: str) -> str:
    parsed = urlparse((value or "").strip().rstrip("/"))
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("TokenManager 地址必须是本机 HTTP loopback 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("TokenManager 地址不能包含凭据、查询参数或片段")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("TokenManager 端口无效") from exc
    return parsed.geturl().rstrip("/")


def save_config(*, enabled: bool, url: str, token: str | None, interval_seconds: int = 60) -> dict:
    config = _load_config()
    previous_url = str(config.get("url") or DEFAULT_URL).rstrip("/")
    config["url"] = _validated_url(url)
    endpoint_changed = config["url"] != previous_url
    if endpoint_changed:
        config["cursor"] = ""
        config["conversation_cursor"] = ""
        config["memory_cursor"] = ""
    config["enabled"] = bool(enabled)
    config["interval_seconds"] = max(30, min(int(interval_seconds), 3600))
    if token is not None and token.strip():
        if len(token.strip()) < 32:
            raise ValueError("TokenManager 本机 API 令牌格式无效")
        config["token"] = token.strip()
        config["cursor"] = ""
        config["conversation_cursor"] = ""
        config["memory_cursor"] = ""
        endpoint_changed = True
    if config["enabled"] and not config.get("token"):
        raise ValueError("启用同步前需要粘贴 TokenManager 本机 API 令牌")
    _atomic_save_config(config)
    # A previous test/sync can leave a configuration-related error in the
    # process-local runtime state. Once a valid configuration is persisted it
    # is misleading to keep rendering that stale error in the UI.
    with _RUNTIME_LOCK:
        _RUNTIME["last_error"] = None
        if endpoint_changed:
            _RUNTIME["memory_api_supported"] = None
            _RUNTIME["capabilities"] = []
            _RUNTIME["sync_mode"] = "unknown"
            _RUNTIME["fallback_reason"] = None
    return public_status()


def public_status() -> dict:
    config = _load_config()
    with _RUNTIME_LOCK:
        runtime = dict(_RUNTIME)
    identity_state = _load_identity_state()
    conversation_count = 0
    if CONVERSATION_DIR.is_dir():
        conversation_count = sum(1 for _ in CONVERSATION_DIR.rglob("*.md"))
    memory_count = 0
    if MEMORY_IMPORT_DIR.is_dir():
        memory_count = sum(1 for _ in MEMORY_IMPORT_DIR.rglob("*.md"))
    return {
        "enabled": bool(config.get("enabled")),
        "url": config.get("url") or DEFAULT_URL,
        "token_configured": bool(config.get("token")),
        "interval_seconds": int(config.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS),
        "cursor": str(config.get("conversation_cursor") or config.get("cursor") or ""),
        "conversation_cursor": str(config.get("conversation_cursor") or ""),
        "memory_cursor": str(config.get("memory_cursor") or ""),
        "conversation_count": conversation_count,
        "memory_count": memory_count,
        **runtime,
        "identity_pending": bool(identity_state.get("pending_revision")),
        "identity_pending_revision": identity_state.get("pending_revision"),
        "identity_last_revision": identity_state.get("last_revision"),
        "identity_last_completed_at": identity_state.get("last_completed_at"),
        "identity_last_result": identity_state.get("last_result"),
        "identity_last_error": identity_state.get("last_error"),
    }


def _api_request(config: dict, path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        config["url"].rstrip("/") + path,
        data=data,
        headers={
            "Authorization": f"Bearer {config['token']}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
            "User-Agent": "CentaurAI-Personal-Memory/1",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error")
        except Exception:
            detail = None
        raise RuntimeError(detail or f"TokenManager API 返回 HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"无法连接 TokenManager：{exc}") from exc


def _health_request(config: dict) -> dict:
    request = Request(
        config["url"].rstrip("/") + "/v1/health",
        headers={"Accept": "application/json", "User-Agent": "CentaurAI-Personal-Memory/1"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"无法读取 TokenManager 健康状态：{exc}") from exc


def _record_capabilities(health: dict) -> list[str]:
    capabilities = [
        str(value)
        for value in (health.get("capabilities") or [])
        if isinstance(value, str)
    ]
    api_enabled = bool(health.get("enabled", True))
    supports_memories = "memories" in capabilities and api_enabled
    with _RUNTIME_LOCK:
        _RUNTIME["capabilities"] = capabilities
        _RUNTIME["memory_api_supported"] = supports_memories
        _RUNTIME["sync_mode"] = "tokenmanager-api" if supports_memories else "legacy-filesystem"
        _RUNTIME["fallback_reason"] = None if supports_memories else (
            "TokenManager 本机 API 未启用"
            if not api_enabled
            else "TokenManager 未声明 memories capability"
        )
    return capabilities


def should_use_legacy_memory_scanner() -> bool:
    """Return whether the old direct scanner should run for this cycle.

    A transient outage never flips an already confirmed memory API back to the
    filesystem path, preventing two writers from racing over the same corpus.
    """
    config = _load_config()
    if not config.get("enabled") or not config.get("token"):
        with _RUNTIME_LOCK:
            _RUNTIME["sync_mode"] = "legacy-filesystem"
            _RUNTIME["fallback_reason"] = "TokenManager 同步尚未启用或令牌未配置"
        return True
    try:
        _record_capabilities(_health_request(config))
        with _RUNTIME_LOCK:
            return _RUNTIME.get("memory_api_supported") is not True
    except RuntimeError as exc:
        with _RUNTIME_LOCK:
            previously_supported = _RUNTIME.get("memory_api_supported") is True
            if not previously_supported:
                _RUNTIME["sync_mode"] = "legacy-filesystem"
                _RUNTIME["fallback_reason"] = str(exc)
        return not previously_supported


def test_connection() -> dict:
    config = _load_config()
    if not config.get("token"):
        raise ValueError("尚未配置 TokenManager 本机 API 令牌")
    try:
        health = _health_request(config)
        _record_capabilities(health)
        _api_request(config, "/v1/conversations/changes?limit=1")
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
    with _RUNTIME_LOCK:
        _RUNTIME["last_error"] = None
    return {"success": True, "health": health}


def _identity_snapshot() -> tuple[dict, str]:
    files = {}
    hasher = hashlib.sha256()
    for name in ("SOUL.md", "AGENTS.md", "IDENTITY.md", "USER.md"):
        result = memory_store.read_memory_file(name)
        if result is None:
            raise RuntimeError(f"统一身份文件不存在：{name}")
        content = str(result.get("content") or "")
        files[name] = content
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(content.encode("utf-8"))
        hasher.update(b"\xff")
    return {"schemaVersion": 1, "files": files}, hasher.hexdigest()


def publish_identity() -> dict:
    """Publish the latest four-file identity snapshot without rolling back local saves."""
    with _IDENTITY_LOCK:
        snapshot, revision = _identity_snapshot()
        state = _load_identity_state()
        state.update(pending_revision=revision, last_error=None)
        _atomic_save_identity_state(state)
        with _RUNTIME_LOCK:
            _RUNTIME.update(identity_running=True, identity_last_error=None)

        config = _load_config()
        error = None
        result = None
        try:
            if not config.get("token"):
                raise RuntimeError("尚未配置 TokenManager 本机 API Bearer Token")
            health = _health_request(config)
            capabilities = _record_capabilities(health)
            if not health.get("enabled", True):
                raise RuntimeError("TokenManager 本机 API 尚未启用")
            if "identity-write" not in capabilities:
                raise RuntimeError("TokenManager 尚未开启“允许身份写入”")
            result = _api_request(config, "/v1/identity", method="PUT", payload=snapshot)
            remote_state = str(result.get("state") or "partial")
            state = _load_identity_state()
            if state.get("pending_revision") == revision and remote_state in {"applied", "unchanged"}:
                state["pending_revision"] = None
            state.update(
                last_revision=str(result.get("revision") or revision),
                last_result=result,
                last_error=None if remote_state in {"applied", "unchanged"} else "部分 Agent 身份写入失败",
            )
            _atomic_save_identity_state(state)
            if remote_state == "partial":
                error = "部分 Agent 身份写入失败"
        except Exception as exc:
            error = str(exc)
            state = _load_identity_state()
            state.update(pending_revision=revision, last_error=error)
            _atomic_save_identity_state(state)

        completed_at = int(time.time() * 1000)
        state = _load_identity_state()
        state["last_completed_at"] = completed_at
        _atomic_save_identity_state(state)
        with _RUNTIME_LOCK:
            _RUNTIME.update(
                identity_running=False,
                identity_last_revision=revision,
                identity_last_completed_at=completed_at,
                identity_last_error=error,
                identity_last_result=result,
            )
        return {
            "success": error is None,
            "state": "pending" if error else str(result.get("state") or "applied"),
            "revision": revision,
            "error": error,
            "result": result,
        }


def retry_pending_identity() -> dict | None:
    if not _load_identity_state().get("pending_revision"):
        return None
    return publish_identity()


def _safe_provider(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", (value or "unknown").strip()).strip("-.")
    return safe[:80] or "unknown"


def _frontmatter_value(value: object) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _latest_messages(detail: dict) -> list[dict]:
    latest: dict[int, dict] = {}
    for message in detail.get("messages") or []:
        try:
            position = int(message.get("logicalPosition", 0))
            revision = int(message.get("revision", 0))
        except (TypeError, ValueError):
            continue
        current = latest.get(position)
        if current is None or revision >= int(current.get("revision", 0)):
            latest[position] = message
    return [latest[position] for position in sorted(latest)]


def _extractive_summary(title: str, messages: list[dict]) -> str:
    user = next(
        (str(message.get("content", "")).strip() for message in messages if message.get("role") == "user" and str(message.get("content", "")).strip()),
        "",
    )
    assistant = next(
        (str(message.get("content", "")).strip() for message in reversed(messages) if message.get("role") == "assistant" and str(message.get("content", "")).strip()),
        "",
    )
    parts = [f"主题：{title.strip() or '未命名对话'}。"]
    if user:
        parts.append("用户重点：" + user[:280].replace("\n", " "))
    if assistant:
        parts.append("最近结论：" + assistant[:360].replace("\n", " "))
    return "\n".join(parts)


def _llm_summary(title: str, messages: list[dict]) -> str | None:
    transcript = "\n\n".join(
        f"{message.get('role', 'unknown')}: {str(message.get('content', '')).strip()}"
        for message in messages
        if str(message.get("content", "")).strip()
    )
    transcript = transcript[-12_000:]
    if not transcript:
        return None
    payload = json.dumps(
        {
            "model": SUMMARY_MODEL,
            "stream": False,
            "think": False,
            "keep_alive": 0,
            "options": {"temperature": 0.2, "num_predict": 420, "num_ctx": 4096},
            "messages": [
                {
                    "role": "system",
                    "content": "你负责把个人 Agent 对话整理成可检索记忆。只总结事实、决策、偏好、待办和重要上下文，不要编造。使用简洁中文。",
                },
                {
                    "role": "user",
                    "content": f"对话标题：{title}\n\n{transcript}\n\n请给出一段摘要和最多 5 条关键记忆。",
                },
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        request = Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
        summary = str((data.get("message") or {}).get("content") or "").strip()
        return summary or None
    except Exception as exc:
        logger.info("TokenManager 会话本地摘要暂时降级: %s", exc)
        return None


def _render_markdown(detail: dict, sequence: int, allow_llm: bool) -> tuple[str, bool]:
    conversation = detail.get("conversation") or {}
    messages = _latest_messages(detail)
    title = str(conversation.get("title") or "未命名 Agent 对话")
    summary = str(conversation.get("summary") or "").strip()
    llm_used = False
    if not summary and allow_llm:
        summary = _llm_summary(title, messages) or ""
        llm_used = bool(summary)
    if not summary:
        summary = _extractive_summary(title, messages)

    lines = [
        "---",
        "source: tokenmanager",
        f"conversation_id: {_frontmatter_value(conversation.get('id'))}",
        f"provider: {_frontmatter_value(conversation.get('provider'))}",
        f"source_type: {_frontmatter_value(conversation.get('source'))}",
        f"status: {_frontmatter_value(conversation.get('status'))}",
        f"created_at: {int(conversation.get('createdAt') or 0)}",
        f"updated_at: {int(conversation.get('updatedAt') or 0)}",
        f"tokenmanager_revision: {int(sequence)}",
    ]
    for frontmatter_key, api_key in (
        ("owner_key", "ownerKey"),
        ("user_id", "userId"),
        ("user_name", "userName"),
        ("user_email", "userEmail"),
    ):
        value = conversation.get(api_key)
        if value is not None and str(value).strip():
            lines.append(f"{frontmatter_key}: {_frontmatter_value(value)}")
    lines.extend(
        [
            "---",
            "",
            f"# {title}",
            "",
            "## 会话摘要",
            "",
            summary,
            "",
            "## 对话全文",
            "",
        ]
    )
    for message in messages:
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        role = str(message.get("role") or "unknown")
        timestamp = message.get("createdAt")
        suffix = f" · {timestamp}" if timestamp else ""
        lines.extend([f"### {role}{suffix}", "", content, ""])
    return "\n".join(lines).rstrip() + "\n", llm_used


def _conversation_path(detail: dict) -> tuple[Path, str]:
    conversation = detail.get("conversation") or {}
    conversation_id = str(conversation.get("id") or "")
    if not conversation_id:
        raise ValueError("TokenManager 会话缺少 ID")
    provider = _safe_provider(str(conversation.get("provider") or "unknown"))
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:24]
    relative = f"conversations/{provider}/{digest}.md"
    return Path(MEMORY_DIR) / relative, relative


def _import_detail(detail: dict, sequence: int, allow_llm: bool) -> bool:
    target, relative = _conversation_path(detail)
    content, _ = _render_markdown(detail, sequence, allow_llm)
    if target.is_file() and target.read_text(encoding="utf-8", errors="replace") == content:
        return False
    provider = str((detail.get("conversation") or {}).get("provider") or "unknown")
    memory_store.write_memory_file(relative, content, source_agent=f"tokenmanager:{provider}", skip_index=True)
    with _NEW_FILES_LOCK:
        _NEW_FILES.append((str(target), relative))
    return True


def _memory_path(memory: dict) -> tuple[Path, str]:
    memory_id = str(memory.get("id") or "")
    if not memory_id:
        raise ValueError("TokenManager 记忆缺少 ID")
    provider = _safe_provider(str(memory.get("provider") or "unknown"))
    digest = hashlib.sha256(memory_id.encode("utf-8")).hexdigest()[:24]
    relative = f"imports/tokenmanager/{provider}/{digest}.md"
    return Path(MEMORY_DIR) / relative, relative


def _render_memory_markdown(detail: dict, sequence: int) -> str:
    memory = detail.get("memory") or {}
    content = str(detail.get("content") or "").strip()
    title = str(memory.get("title") or "Agent memory").strip()
    lines = [
        "---",
        "source: tokenmanager-memory",
        f"memory_id: {_frontmatter_value(memory.get('id'))}",
        f"provider: {_frontmatter_value(memory.get('provider'))}",
        f"scope: {_frontmatter_value(memory.get('scope'))}",
        f"kind: {_frontmatter_value(memory.get('kind'))}",
        f"source_path: {_frontmatter_value(memory.get('path'))}",
        f"content_hash: {_frontmatter_value(memory.get('contentHash'))}",
        f"tokenmanager_revision: {int(sequence)}",
    ]
    if memory.get("projectDir"):
        lines.append(f"project_dir: {_frontmatter_value(memory.get('projectDir'))}")
    if memory.get("sourceModifiedAt") is not None:
        lines.append(f"source_modified_at: {int(memory.get('sourceModifiedAt') or 0)}")
    lines.extend(["---", "", f"# {title}", "", content, ""])
    return "\n".join(lines).rstrip() + "\n"


def _import_memory_detail(detail: dict, sequence: int) -> bool:
    memory = detail.get("memory") or {}
    target, relative = _memory_path(memory)
    content = _render_memory_markdown(detail, sequence)
    if target.is_file() and target.read_text(encoding="utf-8", errors="replace") == content:
        return False
    provider = str(memory.get("provider") or "unknown")
    memory_store.write_memory_file(relative, content, source_agent=f"tokenmanager-memory:{provider}", skip_index=True)
    with _NEW_FILES_LOCK:
        _NEW_FILES.append((str(target), relative))
    return True


def _delete_memory(memory: dict) -> bool:
    target, relative = _memory_path(memory)
    if not target.is_file():
        return False
    head = target.read_text(encoding="utf-8", errors="replace")[:16_000]
    expected_id = str(memory.get("id") or "")
    source_match = re.search(r"(?m)^source:\s*tokenmanager-memory\s*$", head)
    id_match = re.search(r"(?m)^memory_id:\s*(.+)$", head)
    if not source_match or not id_match:
        raise RuntimeError(f"拒绝删除无法确认来源的记忆文件：{relative}")
    try:
        stored_id = str(json.loads(id_match.group(1).strip()))
    except (ValueError, TypeError):
        stored_id = id_match.group(1).strip().strip('"')
    if stored_id != expected_id:
        raise RuntimeError(f"拒绝删除 ID 不匹配的记忆文件：{relative}")
    return bool(memory_store.delete_memory_file(relative))


def _cleanup_legacy_generated(providers: set[str]) -> int:
    removed = 0
    imports_root = Path(MEMORY_DIR) / "imports"
    for provider in providers:
        candidate = imports_root / f"{_safe_provider(provider)}.md"
        if not candidate.is_file():
            continue
        head = candidate.read_text(encoding="utf-8", errors="replace")[:12_000]
        if "This file is generated by scripts/sync_agent_memories.py." not in head:
            continue
        relative = str(candidate.relative_to(Path(MEMORY_DIR)))
        if memory_store.delete_memory_file(relative):
            removed += 1
    return removed


def _managed_memory_providers() -> set[str]:
    if not MEMORY_IMPORT_DIR.is_dir():
        return set()
    return {path.name for path in MEMORY_IMPORT_DIR.iterdir() if path.is_dir()}


def _sync_conversations(config: dict, cursor: str, max_pages: int) -> tuple[str, int, int, int]:
    imported = skipped = failed = 0
    llm_remaining = MAX_LLM_SUMMARIES_PER_RUN
    for _ in range(max(1, max_pages)):
        query = urlencode({"cursor": cursor, "limit": PAGE_SIZE}) if cursor else urlencode({"limit": PAGE_SIZE})
        page = _api_request(config, f"/v1/conversations/changes?{query}")
        items = page.get("items") or []
        latest_by_id: dict[str, dict] = {}
        for item in items:
            conversation = item.get("conversation") or {}
            conversation_id = str(conversation.get("id") or "")
            if conversation_id:
                latest_by_id[conversation_id] = item
        for item in latest_by_id.values():
            conversation = item.get("conversation") or {}
            detail = _api_request(config, f"/v1/conversations/{quote(str(conversation['id']), safe='')}")
            allow_llm = llm_remaining > 0 and not conversation.get("hasPartialResponse", False)
            try:
                changed = _import_detail(detail, int(item.get("sequence") or 0), allow_llm)
                if changed:
                    imported += 1
                    if allow_llm:
                        llm_remaining -= 1
                else:
                    skipped += 1
            except Exception:
                failed += 1
                raise
        next_cursor = str(page.get("nextCursor") or cursor)
        if items and next_cursor == cursor:
            raise RuntimeError("TokenManager 对话 API 返回了未前进的游标")
        cursor = next_cursor
        config["conversation_cursor"] = cursor
        config["cursor"] = cursor
        _atomic_save_config(config)
        if not page.get("hasMore"):
            break
    return cursor, imported, skipped, failed


def _sync_memories(
    config: dict, cursor: str, max_pages: int
) -> tuple[str, int, int, int, set[str], bool]:
    imported = skipped = deleted = 0
    providers: set[str] = set()
    completed = False
    for _ in range(max(1, max_pages)):
        query = urlencode({"cursor": cursor, "limit": PAGE_SIZE}) if cursor else urlencode({"limit": PAGE_SIZE})
        page = _api_request(config, f"/v1/memories/changes?{query}")
        items = page.get("items") or []
        for item in items:
            memory = item.get("memory") or {}
            memory_id = str(memory.get("id") or "")
            if not memory_id:
                raise RuntimeError("TokenManager 记忆变更缺少 ID")
            providers.add(str(memory.get("provider") or "unknown"))
            operation = str(item.get("operation") or "upsert")
            if operation == "delete":
                if _delete_memory(memory):
                    deleted += 1
                else:
                    skipped += 1
                continue
            detail = _api_request(config, f"/v1/memories/{quote(memory_id, safe='')}")
            if _import_memory_detail(detail, int(item.get("sequence") or 0)):
                imported += 1
            else:
                skipped += 1
        next_cursor = str(page.get("nextCursor") or cursor)
        if items and next_cursor == cursor:
            raise RuntimeError("TokenManager 记忆 API 返回了未前进的游标")
        cursor = next_cursor
        config["memory_cursor"] = cursor
        _atomic_save_config(config)
        if not page.get("hasMore"):
            completed = True
            break
    return cursor, imported, skipped, deleted, providers, completed


def sync_now(*, max_pages: int = MAX_PAGES_PER_RUN) -> dict:
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"success": False, "busy": True, **public_status()}
    with _RUNTIME_LOCK:
        _RUNTIME.update(
            running=True,
            last_started_at=int(time.time() * 1000),
            last_imported=0,
            last_skipped=0,
            last_failed=0,
            last_error=None,
            last_conversation_imported=0,
            last_memory_imported=0,
            last_memory_deleted=0,
        )
    imported = skipped = failed = 0
    conversation_imported = memory_imported = memory_deleted = 0
    try:
        config = _load_config()
        if not config.get("enabled"):
            raise RuntimeError("TokenManager 对话与记忆同步未启用")
        if not config.get("token"):
            raise RuntimeError("TokenManager 本机 API 令牌尚未配置")
        schema_version = int(config.get("schema_version") or 0)
        conversation_cursor = (
            ""
            if schema_version < 2
            else str(config.get("conversation_cursor") or config.get("cursor") or "")
        )
        memory_cursor = "" if schema_version < 3 else str(config.get("memory_cursor") or "")
        health = _health_request(config)
        capabilities = _record_capabilities(health)

        conversation_cursor, conversation_imported, conversation_skipped, conversation_failed = _sync_conversations(
            config, conversation_cursor, max_pages
        )
        imported += conversation_imported
        skipped += conversation_skipped
        failed += conversation_failed

        memory_skipped = 0
        if "memories" in capabilities:
            (
                memory_cursor,
                memory_imported,
                memory_skipped,
                memory_deleted,
                providers,
                memory_backfill_complete,
            ) = _sync_memories(config, memory_cursor, max_pages)
            imported += memory_imported
            skipped += memory_skipped
            if memory_backfill_complete:
                _cleanup_legacy_generated(providers | _managed_memory_providers())
        config["conversation_cursor"] = conversation_cursor
        config["cursor"] = conversation_cursor
        config["memory_cursor"] = memory_cursor
        config["schema_version"] = SYNC_SCHEMA_VERSION
        _atomic_save_config(config)
        # 仅对本次新增文件做向量索引（不做全量重建）
        pending: list[tuple[str, str]] = []
        with _NEW_FILES_LOCK:
            pending = _NEW_FILES[:]
            _NEW_FILES.clear()
        if pending:
            logger.info(f"TokenManager 同步: 本批次导入 {len(pending)} 个新文件，开始建立索引")
            for full_path, rel_path in pending:
                try:
                    content = Path(full_path).read_text(encoding="utf-8")
                    memory_store.index_memory_file(full_path, rel_path, content, source_agent="tokenmanager-sync")
                except Exception as exc:
                    logger.warning(f"记忆索引失败 {rel_path}: {exc}")
            logger.info(f"TokenManager 同步: 新文件索引完成 ({len(pending)} 个)")
        with _RUNTIME_LOCK:
            _RUNTIME.update(
                running=False,
                last_completed_at=int(time.time() * 1000),
                last_imported=imported,
                last_skipped=skipped,
                last_failed=failed,
                last_error=None,
                last_conversation_imported=conversation_imported,
                last_memory_imported=memory_imported,
                last_memory_deleted=memory_deleted,
            )
        return {
            "success": True,
            "imported": imported,
            "skipped": skipped,
            "failed": failed,
            "conversation_imported": conversation_imported,
            "memory_imported": memory_imported,
            "memory_deleted": memory_deleted,
            **public_status(),
        }
    except Exception as exc:
        logger.warning("TokenManager 对话同步失败: %s", exc)
        with _RUNTIME_LOCK:
            _RUNTIME.update(
                running=False,
                last_completed_at=int(time.time() * 1000),
                last_imported=imported,
                last_skipped=skipped,
                last_failed=max(failed, 1),
                last_error=str(exc),
                last_conversation_imported=conversation_imported,
                last_memory_imported=memory_imported,
                last_memory_deleted=memory_deleted,
            )
        return {
            "success": False,
            "imported": imported,
            "skipped": skipped,
            "failed": max(failed, 1),
            "conversation_imported": conversation_imported,
            "memory_imported": memory_imported,
            "memory_deleted": memory_deleted,
            "error": str(exc),
            **public_status(),
        }
    finally:
        _SYNC_LOCK.release()


def run_forever() -> None:
    """Background pull loop; failures are isolated from the main memory API."""
    time.sleep(12)
    backoff = DEFAULT_INTERVAL_SECONDS
    while True:
        config = _load_config()
        interval = max(30, min(int(config.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS), 3600))
        identity_result = retry_pending_identity()
        if not config.get("enabled"):
            time.sleep(min(interval, 60))
            continue
        result = sync_now()
        if result.get("success") and (not identity_result or identity_result.get("success")):
            backoff = interval
        else:
            backoff = min(max(backoff * 2, interval), 900)
        time.sleep(backoff)
