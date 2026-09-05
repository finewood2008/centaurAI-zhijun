"""运行时配置提供者（P1 §5.1 / §5.1.1 / §5.2 / §6.1）。

职责：
- 由「部署默认值(config) + 持久化运行时覆盖(runtime_settings_store)」生成**不可变
  快照**；请求/任务边界 `get_snapshot()` 只取一次并沿调用链下传，业务函数不直接读
  `config.*`（§5.1.1）；
- URL、模型与超时的格式校验；
- PUT 的 secret saga 编排（§5.2.1）：先写密钥 → SQLite 提交 → 失败补偿 → 发布快照 →
  延迟清理旧密钥。

本模块不持有 FastAPI Request；管理端通过本模块的校验器与保存流程完成统一行为。
"""
from __future__ import annotations

import config
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .secret_store import (
    SECRET_PREFIX,
    SecretStore,
    UnavailableSecretStore,
    _REF_RETENTION_SECONDS,
    get_default_secret_store,
    new_secret_ref,
)
from .stores.runtime_settings_store import (
    SECTION_CHAT,
    SECTION_MATERIAL,
    ActiveProviderError,
    RevisionConflictError,
    RuntimeSettingsStore,
)

# 旧密钥延迟清理窗口：给进行中的旧快照请求留出完成时间（QA 信号量为 1，总预算上限 90s）。
_OLD_SECRET_CLEANUP_DELAY_SECONDS = 120.0

# ---- 校验限制 ----
_URL_MAX_LEN = 2048
_MODEL_MAX_LEN = 128
_MODEL_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:/+-"
)


class RuntimeConfigError(ValueError):
    """运行时配置通用错误（API 层映射为 400）。"""


class ValidationError(RuntimeConfigError):
    """字段格式/范围校验失败。"""


# =====================================================================
# 不可变快照
# =====================================================================


@dataclass(frozen=True)
class LocalOllamaSnapshot:
    """材料处理（含 Wiki 共享）的本地 Ollama 通道快照。"""

    base_url: str
    model: str
    timeout_seconds: int
    keep_alive: int
    context_window: int


@dataclass(frozen=True)
class ChatProviderSnapshot:
    """对话问答通道快照。secret_ref 为内部字段，绝不通过 GET/API 投影返回。"""

    provider: str
    external_enabled: bool
    base_url: str | None
    model: str | None
    api_key_configured: bool
    secret_ref: str | None
    timeout_seconds: int
    total_budget_seconds: int
    fallback_ollama: bool
    local: LocalOllamaSnapshot
    external_provider_id: str | None = None


# =====================================================================
# 校验
# =====================================================================


def _parse_url(url: str) -> tuple[str, str, str, str, str]:
    """解析并做基础安全检查，返回 (scheme, host, port, path, netloc)。"""
    if not url or len(url) > _URL_MAX_LEN:
        raise ValidationError("URL 为空或超长")
    if "\\" in url:
        raise ValidationError("URL 不能包含反斜杠")
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValidationError("仅支持 http/https")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValidationError("URL 缺少主机名")
    if parts.username or parts.password:
        raise ValidationError("URL 不能包含用户名密码")
    if parts.query or parts.fragment:
        raise ValidationError("URL 不能包含 query 或 fragment")
    path = parts.path or ""
    if path != "":
        # path.split("/")[0] 恒为前导斜杠产生的空段；仅检查其余段。
        # 禁止空段（//）、`.`、`..` 等路径规范化绕过。
        if any(seg in ("", ".", "..") for seg in path.split("/")[1:]):
            raise ValidationError("URL 路径包含非法段")
    return scheme, host, parts.port, path, parts.netloc


def validate_ollama_base_url(url: str) -> str:
    """本地 Ollama：仅 `scheme://host[:port]`，路径必须为空或 `/`。

    后端按固定后缀追加 `/api/*`；禁止路径、用户名密码、query、fragment。
    """
    scheme, host, port, path, _netloc = _parse_url(url)
    if path not in ("", "/"):
        raise ValidationError("Ollama 服务地址不允许带路径")
    host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
    netloc = host_part if port is None else f"{host_part}:{port}"
    return f"{scheme}://{netloc}"


def validate_qa_base_url(url: str) -> str:
    """外部 OpenAI 兼容 QA：`scheme://host[:port][/base-prefix]`。

    - 允许规范化的版本前缀（如 `/v1`），由后端在该前缀后追加固定 `/chat/completions`；
    - path 只能由斜杠分隔的普通段组成；保存时规范化为无尾随斜杠；
    - 禁止完整 endpoint、任意资源路径、`..`、query、fragment、反斜杠。
    """
    scheme, host, port, path, _netloc = _parse_url(url)
    segments = [s for s in path.split("/") if s]
    if any(
        seg in ("..", ".", "chat", "completions", "chat/completions")
        or "/" in seg
        for seg in segments
    ):
        raise ValidationError("外部 QA URL 只允许规范化 base prefix（如 /v1），不能带 endpoint")
    normalized_path = ("/" + "/".join(segments)).rstrip("/") if segments else ""
    host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
    netloc = host_part if port is None else f"{host_part}:{port}"
    return f"{scheme}://{netloc}{normalized_path}"


def validate_external_base_url(url: str) -> str:
    from .llm_transport import _check_destination
    if not isinstance(url, str) or any(ord(c) <= 32 for c in url) or "%" in url:
        raise ValidationError("服务地址包含不支持的字符")
    try:
        value = validate_qa_base_url(url.rstrip("/"))
        _check_destination(value)
    except ValidationError:
        raise
    except (ValueError, UnicodeError):
        raise ValidationError("服务地址无效或本机禁止访问此模型服务") from None
    return value


def validate_model_name(model: str) -> str:
    model = (model or "").strip()
    if not model or len(model) > _MODEL_MAX_LEN:
        raise ValidationError("模型名不合法：长度必须在 1-128 字符")
    if any(ch not in _MODEL_ALLOWED_CHARS for ch in model):
        raise ValidationError("模型名包含不合法字符")
    return model


def validate_timeout(value, min_seconds: int, max_seconds: int) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise ValidationError("超时必须是整数秒") from None
    if not (min_seconds <= seconds <= max_seconds):
        raise ValidationError(f"超时必须在 {min_seconds}-{max_seconds} 秒之间")
    return seconds


# =====================================================================
# 默认快照（部署默认值）
# =====================================================================


def _default_local_snapshot() -> LocalOllamaSnapshot:
    return LocalOllamaSnapshot(
        base_url=config.LOCAL_OLLAMA_URL,
        model=config.LOCAL_OLLAMA_MODEL,
        timeout_seconds=config.RECOGNITION_AI_TIMEOUT_SECONDS,
        keep_alive=config.RECOGNITION_AI_KEEP_ALIVE,
        context_window=config.RECOGNITION_AI_CONTEXT_WINDOW,
    )


def _default_chat_snapshot() -> ChatProviderSnapshot:
    return ChatProviderSnapshot(
        provider=config.QA_AI_PROVIDER,
        external_enabled=config.QA_AI_EXTERNAL_ENABLED,
        base_url=config.QA_AI_BASE_URL or None,
        model=config.QA_AI_MODEL,
        api_key_configured=bool(config.QA_AI_API_KEY),
        secret_ref=None,
        timeout_seconds=config.QA_AI_TIMEOUT_SECONDS,
        total_budget_seconds=config.QA_AI_TOTAL_BUDGET_SECONDS,
        fallback_ollama=config.QA_AI_FALLBACK_OLLAMA,
        local=_default_local_snapshot(),
    )


def _local_from_payload(payload: dict) -> LocalOllamaSnapshot:
    return LocalOllamaSnapshot(
        base_url=payload["baseUrl"],
        model=payload["model"],
        timeout_seconds=int(payload["timeoutSeconds"]),
        keep_alive=0,
        context_window=4096,
    )


def _chat_from_payload(
    payload: dict, secret_ref: str | None, local: LocalOllamaSnapshot
) -> ChatProviderSnapshot:
    return ChatProviderSnapshot(
        provider=payload.get("provider", "ollama"),
        external_enabled=bool(payload.get("externalEnabled", False)),
        base_url=payload.get("baseUrl") or None,
        model=payload.get("model") or None,
        api_key_configured=bool(secret_ref),
        secret_ref=secret_ref,
        timeout_seconds=int(payload.get("timeoutSeconds", config.QA_AI_TIMEOUT_SECONDS)),
        total_budget_seconds=int(
            payload.get("totalBudgetSeconds", config.QA_AI_TOTAL_BUDGET_SECONDS)
        ),
        fallback_ollama=bool(payload.get("fallbackOllama", True)),
        local=local,  # 材料通道快照（问答本地回退复用同一份，随材料配置联动）
        external_provider_id=payload.get("externalProviderId"),
    )


# =====================================================================
# 运行时配置提供者
# =====================================================================


class RuntimeConfigProvider:
    def __init__(self, store=None, secret_store=None) -> None:
        self._store = store or RuntimeSettingsStore.instance()
        self._secret_store = secret_store or get_default_secret_store()
        self._secret_store_available = not isinstance(
            self._secret_store, UnavailableSecretStore
        )
        self._lock = threading.RLock()
        self._local = _default_local_snapshot()
        self._chat = _default_chat_snapshot()
        self._reload()

    # ---- 内部 ----

    def _reload(self) -> None:
        # 先解析材料通道（问答本地回退复用其快照），再构建问答快照。
        material = self._store.get_section(SECTION_MATERIAL)
        if material:
            self._local = _local_from_payload(material["payload"])
        chat = self._store.get_section(SECTION_CHAT)
        if chat:
            self._chat = _chat_from_payload(chat["payload"], chat.get("secret_ref"), self._local)

    def _chat_with_local(self, local: LocalOllamaSnapshot) -> ChatProviderSnapshot:
        """按最新材料快照重建问答快照的 local（本地回退跟随材料配置）。"""
        return ChatProviderSnapshot(
            provider=self._chat.provider,
            external_enabled=self._chat.external_enabled,
            base_url=self._chat.base_url,
            model=self._chat.model,
            api_key_configured=self._chat.api_key_configured,
            secret_ref=self._chat.secret_ref,
            timeout_seconds=self._chat.timeout_seconds,
            total_budget_seconds=self._chat.total_budget_seconds,
            fallback_ollama=self._chat.fallback_ollama,
            local=local,
            external_provider_id=self._chat.external_provider_id,
        )

    # ---- 快照获取（请求/任务边界调用一次，沿调用链下传） ----

    def get_local_snapshot(self) -> LocalOllamaSnapshot:
        with self._lock:
            return self._local

    def get_chat_snapshot(self) -> ChatProviderSnapshot:
        with self._lock:
            return self._chat

    def resolve_api_key(self, snapshot: ChatProviderSnapshot) -> str | None:
        if snapshot.secret_ref:
            return self._secret_store.get_secret(snapshot.secret_ref)
        # 默认（未持久化覆盖）路径：密钥由部署环境变量提供。
        return config.QA_AI_API_KEY or None

    @property
    def secret_store_available(self) -> bool:
        return self._secret_store_available

    @property
    def store(self) -> RuntimeSettingsStore:
        """返回运行时设置存储，供调用方保持一致的配置上下文。"""
        return self._store

    # ---- 状态投影（内部；API 层再脱敏） ----

    def section_status(self, section: str) -> dict:
        row = self._store.get_section(section)
        if row is None:
            return {"section": section, "revision": 0, "source": "defaults"}
        return row

    def external_profile_projection(self, row):
        current = self._store.get_section(SECTION_CHAT) or {}
        active = (current.get("payload") or {}).get("externalProviderId") == row["id"]
        return {"id": row["id"], "revision": row["revision"], **row["payload"],
                "apiKeyConfigured": bool(row["secret_ref"]), "active": active,
                "pendingActivation": active and (current["payload"].get("externalProviderRevision") != row["revision"])}

    def list_external_providers(self):
        current = self._store.get_section(SECTION_CHAT) or {}
        return {"providers": [self.external_profile_projection(r) for r in self._store.list_external_profiles()],
                "activeProviderId": (current.get("payload") or {}).get("externalProviderId"),
                "chatRevision": current.get("revision", 0)}

    def save_external_provider(self, *, name, base_url, api_key=None, model=None, ident=None, expected_revision=None):
        name = (name or "").strip()
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValidationError("供应商名称长度应为 1-80 字符")
        base_url = validate_external_base_url(base_url)
        model = validate_model_name(model) if model else None
        old = self._store.get_external_profile(ident) if ident else None
        if ident and not old:
            raise KeyError("供应商不存在")
        if old and old["revision"] != expected_revision:
            raise RevisionConflictError(old)
        if old and model is None:
            model = old["payload"].get("model")
        if api_key and (len(api_key) > 8192 or any(ord(c) < 32 for c in api_key)):
            raise ValidationError("API Key 为空、过长或包含控制字符")
        supplied_key = (api_key or "").strip()
        if not supplied_key and (not old or old["payload"]["baseUrl"] != base_url):
            raise ValidationError("新增服务或更改地址时请重新输入该服务的 API Key")
        old_ref = old["secret_ref"] if old else None
        new_ref = old_ref
        if supplied_key:
            if not self._secret_store_available:
                raise RuntimeConfigError("密钥存储不可用")
            new_ref = new_secret_ref()
            self._store.add_secret_ref(new_ref)
            try:
                self._secret_store.set_secret(new_ref, supplied_key)
            except Exception:
                self._compensate_secret(new_ref)
                raise RuntimeConfigError("密钥保存失败") from None
        try:
            row = self._store.put_external_profile(ident, expected_revision,
                {"name": name, "baseUrl": base_url, "model": model}, new_ref)
        except Exception:
            if new_ref and new_ref != old_ref:
                self._compensate_secret(new_ref)
            raise
        if old_ref and old_ref != new_ref:
            self._schedule_secret_cleanup(old_ref)
        return self.external_profile_projection(row)

    def _compensate_secret(self, ref):
        try:
            if ref not in self._store.referenced_secret_refs():
                self._secret_store.delete_secret(ref)
                self._store.remove_secret_ref(ref)
        except Exception:
            pass

    def activate_external_provider(self, ident, *, expected_revision, model, chat_revision):
        model = validate_model_name(model)
        row = self._store.get_external_profile(ident)
        if not row:
            raise KeyError("供应商不存在")
        validate_external_base_url(row["payload"]["baseUrl"])
        if not row["secret_ref"] or not self._secret_store.get_secret(row["secret_ref"]):
            raise ValidationError("API Key 不可用，请重新保存")
        before = self._store.get_section(SECTION_CHAT) or {}
        defaults = {"timeoutSeconds": self._chat.timeout_seconds, "totalBudgetSeconds": self._chat.total_budget_seconds}
        with self._lock:
            result = self._store.activate_external_profile(ident, expected_revision, model, chat_revision, defaults)
            self._reload()
        old_ref = before.get("secret_ref")
        if old_ref and old_ref != result["secret_ref"]:
            self._schedule_secret_cleanup(old_ref)
        return self.external_profile_projection(result)

    def delete_external_provider(self, ident, expected_revision):
        try:
            row = self._store.delete_external_profile(ident, expected_revision)
        except ValueError as exc:
            if isinstance(exc, (RevisionConflictError, ActiveProviderError)):
                raise
            raise ValidationError(str(exc)) from None
        if row["secret_ref"]:
            self._schedule_secret_cleanup(row["secret_ref"])
        return {"deleted": True, "providerId": ident}

    def discover_external_models(self, ident, expected_revision):
        from .external_model_discovery import discover_models
        row = self._store.get_external_profile(ident)
        if not row:
            raise KeyError("供应商不存在")
        if row["revision"] != expected_revision:
            raise RevisionConflictError(row)
        key = self._secret_store.get_secret(row["secret_ref"]) if row["secret_ref"] else None
        if not key:
            raise ValidationError("API Key 不可用，请重新保存")
        result = discover_models(row["payload"]["baseUrl"], key)
        fresh = self._store.get_external_profile(ident)
        if not fresh or fresh["revision"] != row["revision"]:
            raise RevisionConflictError(fresh or {"revision": None})
        return {"models": result, "providerId": ident, "revision": row["revision"]}

    # ---- 保存（PUT；含 secret saga §5.2.1） ----

    def save_material_runtime(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: int,
        expected_revision: int | None,
    ) -> dict:
        base_url = validate_ollama_base_url(base_url)
        model = validate_model_name(model)
        timeout_seconds = validate_timeout(timeout_seconds, 10, 600)
        payload = {"baseUrl": base_url, "model": model, "timeoutSeconds": timeout_seconds}
        row = self._store.put_section(
            SECTION_MATERIAL, expected_revision, payload, secret_ref=None
        )
        with self._lock:
            self._reload()
            self._chat = self._chat_with_local(self._local)
        return row

    def save_chat_provider(
        self,
        *,
        provider: str,
        external_enabled: bool,
        base_url: str | None,
        model: str | None,
        timeout_seconds: int,
        total_budget_seconds: int,
        fallback_ollama: bool,
        api_key: str | None = None,
        clear_api_key: bool = False,
        expected_revision: int | None,
    ) -> dict:
        provider = (provider or "ollama").lower().strip()
        if provider not in ("ollama", "openai"):
            raise ValidationError("仅支持 ollama / openai 提供商")
        external_enabled = bool(external_enabled)
        if provider == "openai":
            base_url = validate_external_base_url(base_url or "")
            model = validate_model_name(model or "")
        else:
            # provider=ollama 时外部开关无意义：强制关闭，避免「ollama 却开启外发」的矛盾态。
            external_enabled = False
            base_url = None
            model = None
        timeout_seconds = validate_timeout(timeout_seconds, 1, 300)
        total_budget_seconds = validate_timeout(total_budget_seconds, 1, 600)

        current = self._store.get_section(SECTION_CHAT)
        old_ref = current["secret_ref"] if current else None
        if provider == "openai" and old_ref and current["payload"].get("baseUrl") != base_url and not (api_key or "").strip() and not clear_api_key:
            raise ValidationError("更改服务地址时请重新输入该服务的 API Key")
        # 先做矛盾状态校验（在写密钥之前），避免「启用外部但没有密钥」被保存。
        if clear_api_key:
            effective_ref = None
        elif api_key is not None and api_key.strip() != "":
            effective_ref = "__new__"
        else:
            effective_ref = old_ref
        if provider == "openai" and external_enabled and not effective_ref:
            raise ValidationError("启用外部问答时必须配置 API Key")

        new_ref = old_ref
        if clear_api_key:
            new_ref = None
        elif api_key is not None and api_key.strip() != "":
            if not self._secret_store_available:
                raise RuntimeConfigError("密钥存储不可用：API Key 仅由部署环境提供")
            new_ref = new_secret_ref()
            # saga 第 1 步：先落台账（可恢复），再写密钥；失败不写库、不发布。
            self._store.add_secret_ref(new_ref)
            self._secret_store.set_secret(new_ref, api_key)

        payload = {
            "provider": provider,
            "externalEnabled": external_enabled,
            "baseUrl": base_url,
            "model": model,
            "timeoutSeconds": int(timeout_seconds),
            "totalBudgetSeconds": int(total_budget_seconds),
            "fallbackOllama": bool(fallback_ollama),
        }
        previous_payload = (current or {}).get("payload") or {}
        if (previous_payload.get("externalProviderId") and provider == previous_payload.get("provider")
                and base_url == previous_payload.get("baseUrl") and model == previous_payload.get("model")
                and new_ref == old_ref):
            # Timeout/online toggles are not a request to abandon the selected
            # profile, including when that profile has not-yet-activated edits.
            payload.update({key: previous_payload[key] for key in ("externalProviderId", "externalProviderRevision") if key in previous_payload})
        try:
            row = self._store.put_section(
                SECTION_CHAT, expected_revision, payload, secret_ref=new_ref
            )
        except Exception:
            # saga 第 3 步：SQLite 提交失败，删除刚写入的新密钥；
            # 删除失败则保留台账（无引用），由启动孤儿回收兜底。
            if new_ref and new_ref != old_ref:
                try:
                    self._secret_store.delete_secret(new_ref)
                    self._store.remove_secret_ref(new_ref)
                except Exception:
                    pass
            raise
        with self._lock:
            self._reload()
        if old_ref and old_ref != new_ref:
            self._schedule_secret_cleanup(old_ref)
        return row

    def _schedule_secret_cleanup(self, ref: str, delay: float = _OLD_SECRET_CLEANUP_DELAY_SECONDS) -> None:
        """saga 第 4 步：新快照发布后延迟清理旧密钥；失败保留台账由启动回收。"""

        def _worker() -> None:
            time.sleep(delay)
            try:
                if ref in self._store.referenced_secret_refs():
                    return
                self._secret_store.delete_secret(ref)
                self._store.remove_secret_ref(ref)
            except Exception:
                pass  # 台账保留，启动孤儿回收重试

        threading.Thread(target=_worker, daemon=True).start()

    # ---- 候选快照（test 端点：不持久化、不发布，§6.4） ----

    def candidate_local_snapshot(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LocalOllamaSnapshot:
        current = self._local
        base = validate_ollama_base_url(base_url or current.base_url)
        mdl = validate_model_name(model or current.model)
        timeout = validate_timeout(
            current.timeout_seconds if timeout_seconds is None else timeout_seconds, 10, 600
        )
        return LocalOllamaSnapshot(
            base_url=base, model=mdl, timeout_seconds=timeout,
            keep_alive=current.keep_alive, context_window=current.context_window,
        )

    def candidate_chat_snapshot(
        self,
        *,
        provider: str | None = None,
        external_enabled: bool | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
        total_budget_seconds: int | None = None,
        fallback_ollama: bool | None = None,
        api_key: str | None = None,
    ) -> ChatProviderSnapshot:
        current = self._chat
        provider = (provider or current.provider or "ollama").lower().strip()
        if provider not in ("ollama", "openai"):
            raise ValidationError("仅支持 ollama / openai 提供商")
        enabled = current.external_enabled if external_enabled is None else bool(external_enabled)
        if provider == "openai":
            base = validate_external_base_url(base_url or current.base_url or "")
            if base != current.base_url and not (api_key or "").strip():
                raise ValidationError("测试其他服务地址时请重新输入该服务的 API Key")
            mdl = validate_model_name(model or current.model or "")
            if enabled and not (bool(api_key) or current.api_key_configured):
                raise ValidationError("启用外部问答时必须配置 API Key")
        else:
            enabled = False  # ollama 强制关闭外发
            base = None
            mdl = None
        timeout = validate_timeout(
            current.timeout_seconds if timeout_seconds is None else timeout_seconds, 1, 300
        )
        budget = validate_timeout(
            current.total_budget_seconds if total_budget_seconds is None else total_budget_seconds,
            1,
            600,
        )
        fallback = current.fallback_ollama if fallback_ollama is None else bool(fallback_ollama)
        candidate_key_configured = bool(api_key) or current.api_key_configured
        return ChatProviderSnapshot(
            provider=provider,
            external_enabled=enabled,
            base_url=base,
            model=mdl,
            api_key_configured=candidate_key_configured,
            secret_ref=current.secret_ref,
            timeout_seconds=timeout,
            total_budget_seconds=budget,
            fallback_ollama=fallback,
            local=self._local,
        )

    def resolve_candidate_api_key(
        self, snapshot: ChatProviderSnapshot, candidate_key: str | None
    ) -> str | None:
        """test 端点：候选 key 优先于已保存 key，仅用于本次请求。"""
        if candidate_key:
            return candidate_key
        return self.resolve_api_key(snapshot)

    # ---- 启动初始化 ----

    def cleanup_orphan_secrets(self, retention_seconds: float = _REF_RETENTION_SECONDS) -> list[str]:
        """回收「无引用且超过保留期」的 secret_ref（基于台账，不依赖密钥后端列举能力）。"""
        return _cleanup_ledger_orphans(
            self._store, self._secret_store, retention_seconds=retention_seconds
        )


def _cleanup_ledger_orphans(
    store: RuntimeSettingsStore,
    secret_store: SecretStore,
    retention_seconds: float,
) -> list[str]:
    """按台账回收孤儿 secret_ref：只清理本应用创建、无 section 引用且超过保留期的引用。"""
    valid_refs = store.referenced_secret_refs()
    removed: list[str] = []
    now = time.time()
    for item in store.list_ledger_refs():
        ref = item["ref"]
        if ref in valid_refs:
            continue
        if now - item["created_at"] < retention_seconds:
            continue
        try:
            if ref in store.referenced_secret_refs():
                continue
            secret_store.delete_secret(ref)
            store.remove_secret_ref(ref)
            removed.append(ref)
        except Exception:
            continue
    return removed


_provider: RuntimeConfigProvider | None = None
_provider_lock = threading.Lock()


def get_provider() -> RuntimeConfigProvider:
    """模块级单例（生产路径）。测试用独立实例避免共享状态。"""
    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = RuntimeConfigProvider()
        return _provider


def reset_provider_for_tests() -> None:
    """测试用：清空模块级单例，使其下次按当前存储重建。"""
    global _provider
    with _provider_lock:
        _provider = None


def initialize_runtime_system(store=None, secret_store=None) -> dict:
    """启动时回收无引用且超过保留期的本应用 secret_ref。"""
    store = store or RuntimeSettingsStore.instance()
    secret = secret_store or get_default_secret_store()
    removed = _cleanup_ledger_orphans(
        store, secret, retention_seconds=_REF_RETENTION_SECONDS
    )
    return {"cleaned_secrets": removed}
