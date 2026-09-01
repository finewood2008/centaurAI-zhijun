"""外部 Agent Gateway 持久化存储（AG-01）。

存储外部服务凭证与审计事件。凭证仅保存强散列（SHA-256）与不可逆前缀
（散列前 10 位，用于展示识别），绝不保存明文 token。V1 固定单工作区，
但表结构保留 allowed_workspace 字段，避免未来多工作区改造重做审计与 URL。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from runtime_paths import AGENT_DB_PATH

from . import config as agent_config

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

# 初始 scope 白名单（与 Review 指引 5.2 一致）。
SCOPES = frozenset(
    {
        "mindos.read",
        "mindos.search",
        "mindos.answer",
        "mindos.import",
        "mindos.knowledge.draft",
        "mindos.knowledge.commit",
        "mindos.correction.draft",
    }
)

# 外部 token 前缀（识别一次性的 service credential）
TOKEN_PREFIX = "agk_"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_clients (
    client_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    allowed_workspace TEXT NOT NULL DEFAULT 'default',
    expires_at REAL,
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_agent_clients_status ON agent_clients(status);
CREATE TABLE IF NOT EXISTS agent_audit_events (
    event_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    action TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    resource_type TEXT NOT NULL DEFAULT '',
    resource_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    request_digest TEXT NOT NULL DEFAULT '',
    response_digest TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    latency_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_audit_trace ON agent_audit_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_agent_audit_client ON agent_audit_events(client_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_audit_created ON agent_audit_events(created_at DESC);
"""

_LOCK = threading.Lock()
_INITIALIZED = False
_DB_PATH = AGENT_DB_PATH


def hash_token(token: str) -> str:
    """凭证强散列；仅此值可入库。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    """不可逆前缀：散列前 10 位，用于展示识别，无法反推明文。"""
    return hash_token(token)[:10]


def validate_scopes(scopes) -> list[str]:
    """校验并规整 scope 列表；非法项抛 ValueError（API 层转 400）。"""
    if not isinstance(scopes, (list, tuple, set)):
        raise ValueError("scopes 必须是列表")
    cleaned: list[str] = []
    for raw in scopes:
        scope = str(raw or "").strip()
        if not scope:
            continue
        if scope not in SCOPES:
            raise ValueError(f"未知 scope: {scope}")
        if scope not in cleaned:
            cleaned.append(scope)
    return cleaned


class AgentStore:
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._ensure()

    @classmethod
    def instance(cls) -> "AgentStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = AgentStore()
            return cls._instance

    # ---- SQLite helpers ----

    def _connect(self) -> sqlite3.Connection:
        self._ensure()
        conn = sqlite3.connect(str(_DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure(self) -> None:
        global _INITIALIZED
        if _INITIALIZED:
            return
        with _LOCK:
            if _INITIALIZED:
                return
            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(_DB_PATH), timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                # 旧版本遗留的工作区迁移：V1 固定单工作区，历史 allowed_workspace
                # 统一改为当前 MINDOS_AGENT_WORKSPACE_ID，避免旧值继续在认证中生效。
                current_workspace = agent_config.WORKSPACE_ID or "default"
                conn.execute(
                    "UPDATE agent_clients SET allowed_workspace=? "
                    "WHERE allowed_workspace IS NOT NULL AND allowed_workspace != ?",
                    (current_workspace, current_workspace),
                )
                conn.commit()
            finally:
                conn.close()
            # Windows 上 chmod(0o600) 不等价于 NTFS ACL：生产部署须由专属运行账号
            # 目录承载 agent_gateway.db 并通过目录 ACL 限制访问（见开发 Review 指引）。
            _DB_PATH.chmod(0o600)
            _INITIALIZED = True

    # ---- 客户端 / 凭证管理（仅本机管理员接口调用） ----

    def create_client(
        self,
        name: str,
        scopes: list[str],
        expires_at: float | None = None,
    ) -> tuple[dict, str]:
        """创建一个 Agent 客户端并返回 (公开信息, 明文 token)。

        明文 token 仅在创建响应中展示一次，库中只存散列与不可逆前缀。
        V1 固定单工作区：allowed_workspace 一律写入配置的 WORKSPACE_ID，
        不接受调用方传入（防止客户端自行声明工作区形成错误授权模型）。
        """
        name = str(name or "").strip()[:120]
        if not name:
            raise ValueError("客户端名称不能为空")
        valid_scopes = validate_scopes(scopes)
        if not valid_scopes:
            raise ValueError("至少需要一个 scope")
        workspace = agent_config.WORKSPACE_ID or "default"
        client_id = "agc_" + uuid.uuid4().hex[:12]
        raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO agent_clients
                   (client_id, name, token_hash, token_prefix, scopes_json,
                    status, allowed_workspace, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    client_id,
                    name,
                    hash_token(raw_token),
                    token_prefix(raw_token),
                    json.dumps(valid_scopes, ensure_ascii=False),
                    STATUS_ACTIVE,
                    workspace,
                    expires_at,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_client(client_id), raw_token

    def get_client(self, client_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM agent_clients WHERE client_id=?", (client_id,)
            ).fetchone()
            return self._client_public(row) if row else None
        finally:
            conn.close()

    def list_clients(self, include_disabled: bool = False) -> list[dict]:
        clause = "" if include_disabled else "WHERE status=?"
        params = () if include_disabled else (STATUS_ACTIVE,)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM agent_clients {clause} ORDER BY created_at DESC", params
            ).fetchall()
            return [self._client_public(r) for r in rows]
        finally:
            conn.close()

    def rotate_client(self, client_id: str) -> tuple[dict, str]:
        """轮换凭证：旧 token 立即失效，签发新 token 并返回明文一次。"""
        existing = self.get_client(client_id)
        if existing is None:
            raise ValueError("客户端不存在")
        scopes = existing["scopes"]
        raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE agent_clients SET token_hash=?, token_prefix=?, "
                "status=?, revoked_at=NULL, last_used_at=NULL WHERE client_id=?",
                (hash_token(raw_token), token_prefix(raw_token), STATUS_ACTIVE, client_id),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_client(client_id), raw_token

    def disable_client(self, client_id: str) -> bool:
        """停用客户端：下一个请求立即拒绝（不依赖长缓存授权结果）。"""
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE agent_clients SET status=?, revoked_at=? WHERE client_id=?",
                    (STATUS_DISABLED, time.time(), client_id),
                )
                return cur.rowcount > 0
        finally:
            conn.close()

    def _client_public(self, row: sqlite3.Row) -> dict:
        return {
            "client_id": row["client_id"],
            "name": row["name"],
            "token_prefix": row["token_prefix"],
            "scopes": json.loads(row["scopes_json"]),
            "status": row["status"],
            "workspace": row["allowed_workspace"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
            "revoked_at": row["revoked_at"],
        }

    # ---- 认证（外部 Agent 调用） ----

    def authenticate(self, token: str) -> dict | None:
        """按散列查找并校验状态/有效期；成功则刷新 last_used_at。

        不区分「不存在 / 已停用 / 已过期」，统一返回 None 以防御枚举。
        """
        digest = hash_token(token)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM agent_clients WHERE token_hash=?", (digest,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] != STATUS_ACTIVE:
                return None
            if row["expires_at"] is not None and float(row["expires_at"]) < time.time():
                return None
            now = time.time()
            conn.execute(
                "UPDATE agent_clients SET last_used_at=? WHERE client_id=?",
                (now, row["client_id"]),
            )
            conn.commit()
            return self._client_public(row)
        finally:
            conn.close()

    # ---- 审计 ----

    def record_audit(
        self,
        *,
        trace_id: str,
        client_id: str,
        action: str,
        scope: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        status_code: int,
        request_digest: str,
        response_digest: str,
        latency_ms: int,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO agent_audit_events
                   (event_id, trace_id, client_id, action, scope, resource_type, resource_id,
                    outcome, status_code, request_digest, response_digest, created_at, latency_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "aev_" + uuid.uuid4().hex,
                    (trace_id or "")[:200],
                    (client_id or "")[:80],
                    (action or "")[:80],
                    (scope or "")[:80],
                    (resource_type or "")[:80],
                    (resource_id or "")[:160],
                    (outcome or "")[:40],
                    int(status_code),
                    (request_digest or "")[:160],
                    (response_digest or "")[:160],
                    time.time(),
                    int(latency_ms),
                ),
            )
            # 有界保留最近审计，避免无限增长。
            conn.execute(
                "DELETE FROM agent_audit_events WHERE event_id NOT IN "
                "(SELECT event_id FROM agent_audit_events ORDER BY created_at DESC LIMIT 50000)"
            )
            conn.commit()
        finally:
            conn.close()

    def list_audit(self, limit: int = 100, client_id: str = "", trace_id: str = "") -> list[dict]:
        clauses = []
        params: list = []
        if client_id:
            clauses.append("client_id=?")
            params.append(client_id)
        if trace_id:
            clauses.append("trace_id=?")
            params.append(trace_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM agent_audit_events {where} "
                f"ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(limit, 1000))),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def reset_for_tests(db_path=None) -> AgentStore:
    """测试用：切换到独立 DB 并清空全局实例；无参数时恢复默认库路径。"""
    global _INITIALIZED, _DB_PATH, AgentStore
    _INITIALIZED = False
    if db_path is None:
        _DB_PATH = AGENT_DB_PATH
    else:
        _DB_PATH = Path(db_path)
    AgentStore._instance = None
    return AgentStore.instance()


def instance() -> AgentStore:
    """模块级单例访问（与 governance_store 等保持一致）。"""
    return AgentStore.instance()
