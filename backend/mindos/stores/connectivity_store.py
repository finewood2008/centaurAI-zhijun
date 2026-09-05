"""Consumer Connectivity 本地状态存储。

阶段 2：外部 Consumer API/Webhook 未接入时，本存储为 MindOS 提供 fail-closed 的
- nonce 单次使用（重放防护）：connectivity_nonce_tombstones
- Client/Device 撤销与连接 epoch：connectivity_revocations / connectivity_epochs
- 活动连接会话登记与「撤销后 5 秒内断开」支撑：connectivity_sessions
- 本机可管理的设备禁用 ACL：connectivity_acl

未来 Consumer Webhook/轮询接入后，只需把下游事件翻译为 mark_revoked /
rotate_epoch / set_acl 等调用，不允许新增第二条状态分支。全部函数线程安全、幂等。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time

from runtime_paths import CONNECTIVITY_DB_PATH

_LOCK = threading.RLock()
_PATH = CONNECTIVITY_DB_PATH
_READY = False

_GLOBAL_ACL_SCOPE = "global"


def _connect() -> sqlite3.Connection:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    global _READY
    with _LOCK:
        if _READY:
            return
        with _connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS connectivity_nonce_tombstones (
              nonce_hash TEXT PRIMARY KEY,
              account_id TEXT NOT NULL,
              client_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              consumed_at REAL NOT NULL,
              expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_nonce_expires ON connectivity_nonce_tombstones(expires_at);
            CREATE TABLE IF NOT EXISTS connectivity_revocations (
              account_id TEXT NOT NULL,
              client_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              revoked_at REAL NOT NULL,
              reason TEXT NOT NULL DEFAULT '',
              PRIMARY KEY (account_id, client_id, device_id)
            );
            CREATE TABLE IF NOT EXISTS connectivity_epochs (
              account_id TEXT NOT NULL,
              client_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              epoch_generation INTEGER NOT NULL,
              updated_at REAL NOT NULL,
              reason TEXT NOT NULL DEFAULT '',
              PRIMARY KEY (account_id, client_id, device_id)
            );
            CREATE TABLE IF NOT EXISTS connectivity_sessions (
              session_id TEXT PRIMARY KEY,
              account_id TEXT NOT NULL,
              client_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              epoch_generation INTEGER NOT NULL,
              created_at REAL NOT NULL,
              expires_at REAL NOT NULL,
              closed_at REAL,
              close_reason TEXT,
              scopes TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_tuple ON connectivity_sessions(account_id, client_id, device_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON connectivity_sessions(expires_at);
            CREATE TABLE IF NOT EXISTS connectivity_acl (
              scope TEXT PRIMARY KEY,
              denied INTEGER NOT NULL,
              reason TEXT NOT NULL DEFAULT '',
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consumer_sync_cursors (
              key TEXT PRIMARY KEY,
              value INTEGER NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consumer_applied_events (
              event_key TEXT NOT NULL,
              seq INTEGER NOT NULL,
              applied_at REAL NOT NULL,
              PRIMARY KEY (event_key, seq)
            );
            """)
            try:
                conn.execute("ALTER TABLE connectivity_sessions ADD COLUMN scopes TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        _READY = True


def reset() -> None:
    """测试钩子：清空全部连接状态（生产运行不调用）。"""
    _init()
    with _LOCK:
        with _connect() as conn:
            for table in (
                "connectivity_nonce_tombstones",
                "connectivity_revocations",
                "connectivity_epochs",
                "connectivity_sessions",
                "connectivity_acl",
                "consumer_sync_cursors",
                "consumer_applied_events",
            ):
                conn.execute(f"DELETE FROM {table}")


def _nonce_key(account_id: str, client_id: str, device_id: str, nonce: str) -> str:
    raw = f"{account_id}|{client_id}|{device_id}|{nonce}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def consume_nonce(
    *,
    account_id: str,
    client_id: str,
    device_id: str,
    nonce: str,
    expires_at: float,
) -> bool:
    """首次使用返回 True 并记录墓碑；重复使用返回 False（重放被拒绝）。"""
    _init()
    key = _nonce_key(account_id, client_id, device_id, nonce)
    now = time.time()
    with _LOCK:
        with _connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO connectivity_nonce_tombstones "
                "(nonce_hash, account_id, client_id, device_id, consumed_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, account_id, client_id, device_id, now, max(now, expires_at)),
            )
            return cursor.rowcount == 1


def prune_expired_nonces(now: float | None = None) -> int:
    """清理已过期的 nonce 墓碑，返回删除行数。"""
    _init()
    cutoff = now if now is not None else time.time()
    with _LOCK:
        with _connect() as conn:
            cursor = conn.execute(
                "DELETE FROM connectivity_nonce_tombstones WHERE expires_at < ?",
                (cutoff,),
            )
            return cursor.rowcount


def is_revoked(*, account_id: str, client_id: str, device_id: str) -> float | None:
    """返回撤销时间戳；未撤销返回 None。"""
    _init()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT revoked_at FROM connectivity_revocations "
                "WHERE account_id=? AND client_id=? AND device_id=?",
                (account_id, client_id, device_id),
            ).fetchone()
            return float(row["revoked_at"]) if row else None


def current_epoch(*, account_id: str, client_id: str, device_id: str) -> int:
    """当前连接 epoch；从未轮换/撤销时返回 0（表示没有历史代际约束）。"""
    _init()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT epoch_generation FROM connectivity_epochs "
                "WHERE account_id=? AND client_id=? AND device_id=?",
                (account_id, client_id, device_id),
            ).fetchone()
            return int(row["epoch_generation"]) if row else 0


def _upsert_epoch(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    client_id: str,
    device_id: str,
    epoch: int,
    reason: str,
) -> None:
    conn.execute(
        "INSERT INTO connectivity_epochs "
        "(account_id, client_id, device_id, epoch_generation, updated_at, reason) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account_id, client_id, device_id) DO UPDATE SET "
        "epoch_generation=excluded.epoch_generation, updated_at=excluded.updated_at, "
        "reason=excluded.reason",
        (account_id, client_id, device_id, epoch, time.time(), reason),
    )


def rotate_epoch(
    *,
    account_id: str,
    client_id: str,
    device_id: str,
    reason: str,
) -> dict:
    """递增连接 epoch 并关闭该 tuple 的旧 epoch 活动会话。返回 {newEpoch, closedSessions}。"""
    _init()
    now = time.time()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT epoch_generation FROM connectivity_epochs "
                "WHERE account_id=? AND client_id=? AND device_id=?",
                (account_id, client_id, device_id),
            ).fetchone()
            current = int(row["epoch_generation"]) if row else 0
            new_epoch = current + 1
            _upsert_epoch(
                conn,
                account_id=account_id,
                client_id=client_id,
                device_id=device_id,
                epoch=new_epoch,
                reason=reason,
            )
            cursor = conn.execute(
                "UPDATE connectivity_sessions SET closed_at=?, close_reason=? "
                "WHERE account_id=? AND client_id=? AND device_id=? "
                "AND epoch_generation < ? AND closed_at IS NULL",
                (now, reason, account_id, client_id, device_id, new_epoch),
            )
            return {"newEpoch": new_epoch, "closedSessions": cursor.rowcount}


def mark_revoked(
    *,
    account_id: str,
    client_id: str,
    device_id: str,
    reason: str,
) -> dict:
    """撤销 Client/Device 连接权限：记录撤销时间并递增 epoch、关闭活动会话。"""
    _init()
    now = time.time()
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO connectivity_revocations "
                "(account_id, client_id, device_id, revoked_at, reason) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id, client_id, device_id) DO UPDATE SET "
                "revoked_at=excluded.revoked_at, reason=excluded.reason",
                (account_id, client_id, device_id, now, reason),
            )
            row = conn.execute(
                "SELECT epoch_generation FROM connectivity_epochs "
                "WHERE account_id=? AND client_id=? AND device_id=?",
                (account_id, client_id, device_id),
            ).fetchone()
            current = int(row["epoch_generation"]) if row else 0
            new_epoch = current + 1
            _upsert_epoch(
                conn,
                account_id=account_id,
                client_id=client_id,
                device_id=device_id,
                epoch=new_epoch,
                reason=reason,
            )
            cursor = conn.execute(
                "UPDATE connectivity_sessions SET closed_at=?, close_reason=? "
                "WHERE account_id=? AND client_id=? AND device_id=? AND closed_at IS NULL",
                (now, reason, account_id, client_id, device_id),
            )
            return {"newEpoch": new_epoch, "closedSessions": cursor.rowcount}


def register_session(
    *,
    session_id: str,
    account_id: str,
    client_id: str,
    device_id: str,
    epoch_generation: int,
    expires_at: float,
    scopes: str = "",
) -> None:
    """登记一个活动连接会话（P2P/长连接建立时调用；同一 id 重复登记为幂等替换）。"""
    _init()
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO connectivity_sessions "
                "(session_id, account_id, client_id, device_id, epoch_generation, "
                "created_at, expires_at, scopes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, account_id, client_id, device_id, epoch_generation, time.time(), expires_at, scopes),
            )


def get_session(*, session_id: str) -> dict | None:
    """按 session_id 返回会话行（含 scopes 与关闭状态）；不存在返回 None。"""
    _init()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM connectivity_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None


def close_session(*, session_id: str, reason: str) -> bool:
    """关闭指定会话；首次关闭返回 True，幂等。"""
    _init()
    with _LOCK:
        with _connect() as conn:
            cursor = conn.execute(
                "UPDATE connectivity_sessions SET closed_at=?, close_reason=? "
                "WHERE session_id=? AND closed_at IS NULL",
                (time.time(), reason, session_id),
            )
            return cursor.rowcount == 1


def active_sessions(
    *,
    account_id: str | None = None,
    client_id: str | None = None,
    device_id: str | None = None,
) -> list[dict]:
    _init()
    query = "SELECT * FROM connectivity_sessions WHERE closed_at IS NULL"
    params: list[object] = []
    if account_id:
        query += " AND account_id=?"
        params.append(account_id)
    if client_id:
        query += " AND client_id=?"
        params.append(client_id)
    if device_id:
        query += " AND device_id=?"
        params.append(device_id)
    query += " ORDER BY created_at DESC"
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]


def close_stale_sessions(now: float | None = None) -> int:
    """关闭已过期、或所属 tuple 的当前 epoch 已推进（被撤销/轮换）的会话。"""
    _init()
    cutoff = now if now is not None else time.time()
    with _LOCK:
        with _connect() as conn:
            cursor = conn.execute(
                "UPDATE connectivity_sessions SET closed_at=?, close_reason=? "
                "WHERE closed_at IS NULL AND (expires_at < ? OR epoch_generation < "
                "(SELECT COALESCE(MAX(epoch_generation), 0) FROM connectivity_epochs "
                "WHERE connectivity_epochs.account_id = connectivity_sessions.account_id "
                "AND connectivity_epochs.client_id = connectivity_sessions.client_id "
                "AND connectivity_epochs.device_id = connectivity_sessions.device_id))",
                (cutoff, "stale", cutoff),
            )
            return cursor.rowcount


def set_acl(*, scope: str, denied: bool, reason: str) -> None:
    """设置 ACL：scope 为 'global' 或 'device:<device_id>'。"""
    _init()
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO connectivity_acl (scope, denied, reason, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET denied=excluded.denied, "
                "reason=excluded.reason, updated_at=excluded.updated_at",
                (scope, 1 if denied else 0, reason, time.time()),
            )


def acl_denied(*, scope: str) -> bool:
    _init()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT denied FROM connectivity_acl WHERE scope=?",
                (scope,),
            ).fetchone()
            return bool(row) and int(row["denied"]) == 1


def is_device_denied(*, device_id: str) -> bool:
    """全局禁用或设备级禁用任一命中即拒绝。"""
    return acl_denied(scope=_GLOBAL_ACL_SCOPE) or acl_denied(scope=f"device:{device_id}")


def snapshot() -> dict:
    """本机管理只读快照（不含秘密；用于撤销/失效的端到端验证）。"""
    _init()
    with _LOCK:
        with _connect() as conn:
            revocations = [
                dict(row) for row in conn.execute(
                    "SELECT * FROM connectivity_revocations ORDER BY revoked_at DESC"
                ).fetchall()
            ]
            epochs = [
                dict(row) for row in conn.execute(
                    "SELECT * FROM connectivity_epochs ORDER BY updated_at DESC"
                ).fetchall()
            ]
            sessions = [
                dict(row) for row in conn.execute(
                    "SELECT * FROM connectivity_sessions WHERE closed_at IS NULL "
                    "ORDER BY created_at DESC"
                ).fetchall()
            ]
            acls = [dict(row) for row in conn.execute("SELECT * FROM connectivity_acl").fetchall()]
    return {"revocations": revocations, "epochs": epochs, "activeSessions": sessions, "acl": acls}


def get_sync_cursor(key: str) -> int:
    """读取已应用的 Consumer 事件游标；从未同步返回 0。"""
    _init()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute("SELECT value FROM consumer_sync_cursors WHERE key=?", (key,)).fetchone()
            return int(row["value"]) if row else 0


def set_sync_cursor(key: str, value: int) -> None:
    """持久化已应用的 Consumer 事件游标（幂等覆盖）。"""
    _init()
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO consumer_sync_cursors (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, time.time()),
            )


def record_applied_event(event_key: str, seq: int) -> bool:
    """以 Consumer 事件 seq 做持久化幂等：首次应用返回 True，重复返回 False。"""
    _init()
    with _LOCK:
        with _connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO consumer_applied_events (event_key, seq, applied_at) VALUES (?, ?, ?)",
                (event_key, seq, time.time()),
            )
            return cursor.rowcount == 1


def sync_apply_revocations(*, cursor_key: str, since: int, entries: list[dict]) -> dict:
    """原子应用一批 Consumer 撤销事件。

    把「事件去重标记 → 撤销 → epoch 递增 → 关闭活动会话 → 游标推进」合并进同一个
    SQLite 事务：任一步失败都不会落盘中途状态，重启后从原游标续传。杜绝此前
    「已去重但撤销/游标未提交」的部分落盘（违反『撤销后 5 秒内断开』）。

    entries 为 [{seq, accountId, clientId, deviceId}]；seq <= since 的事件忽略、
    已应用（consumer_applied_events 冲突）的事件跳过。返回
    {applied, newSince, cursor, affectedDeviceIds}。
    """
    _init()
    now = time.time()
    applied = 0
    new_since = since
    affected_device_ids: list[str] = []
    with _LOCK:
        with _connect() as conn:
            for entry in entries:
                seq = int(entry["seq"])
                if seq <= since:
                    continue
                mark = conn.execute(
                    "INSERT OR IGNORE INTO consumer_applied_events (event_key, seq, applied_at) VALUES (?, ?, ?)",
                    (cursor_key, seq, now),
                )
                if mark.rowcount != 1:
                    continue
                account_id = entry["accountId"]
                client_id = entry["clientId"]
                device_id = entry["deviceId"]
                reason = f"consumer-revoke-{seq}"
                conn.execute(
                    "INSERT INTO connectivity_revocations "
                    "(account_id, client_id, device_id, revoked_at, reason) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_id, client_id, device_id) DO UPDATE SET "
                    "revoked_at=excluded.revoked_at, reason=excluded.reason",
                    (account_id, client_id, device_id, now, reason),
                )
                row = conn.execute(
                    "SELECT epoch_generation FROM connectivity_epochs "
                    "WHERE account_id=? AND client_id=? AND device_id=?",
                    (account_id, client_id, device_id),
                ).fetchone()
                current = int(row["epoch_generation"]) if row else 0
                new_epoch = current + 1
                conn.execute(
                    "INSERT INTO connectivity_epochs "
                    "(account_id, client_id, device_id, epoch_generation, updated_at, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_id, client_id, device_id) DO UPDATE SET "
                    "epoch_generation=excluded.epoch_generation, updated_at=excluded.updated_at, "
                    "reason=excluded.reason",
                    (account_id, client_id, device_id, new_epoch, now, reason),
                )
                conn.execute(
                    "UPDATE connectivity_sessions SET closed_at=?, close_reason=? "
                    "WHERE account_id=? AND client_id=? AND device_id=? AND closed_at IS NULL",
                    (now, reason, account_id, client_id, device_id),
                )
                applied += 1
                new_since = max(new_since, seq)
                affected_device_ids.append(device_id)
            conn.execute(
                "INSERT INTO consumer_sync_cursors (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (cursor_key, new_since, now),
            )
    return {
        "applied": applied,
        "newSince": new_since,
        "cursor": new_since,
        "affectedDeviceIds": affected_device_ids,
    }
