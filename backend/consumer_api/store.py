"""Consumer API Mock 的状态存储（SQLite 持久化，线程安全）。

云端权威语义：
- 手机号即账号（登录即注册），同手机号返回同一 account_id，每端独立 client_id；
- Client 撤销时递增 epoch、作废其全部令牌并关闭连接会话；
- 设备所有权、同步事件与连接票据只由本存储维护，客户端不得伪造。

持久化：db_path 为 None 时使用进程内 SQLite（测试隔离）；传入文件路径时
状态跨重启保留（Mock 服务器用），满足「状态不得仅存内存、重启即丢失」。
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

from runtime_paths import CONSUMER_MOCK_DB_PATH

from .errors import (
    ConsumerApiError,
    ERROR_AUTH_INVALID,
    ERROR_CLIENT_NOT_FOUND,
    ERROR_CLIENT_REVOKED,
    ERROR_DEVICE_ALREADY_OWNED,
    ERROR_DEVICE_NOT_FOUND,
    ERROR_DEVICE_NOT_OWNED,
    ERROR_PROTOCOL_UPGRADE,
    ERROR_PROTOCOL_UNSUPPORTED,
    ERROR_REFRESH_INVALID,
    ERROR_SMS_CODE,
    ERROR_STEP_UP_INVALID,
    ERROR_TOKEN_EXPIRED,
)
from . import signing

ACCESS_TTL_SECONDS = 3600
REFRESH_TTL_SECONDS = 30 * 24 * 3600
TICKET_TTL_SECONDS = 120
TICKET_CONNECT_BEFORE_SECONDS = 60
STEPUP_TTL_SECONDS = 300
STEPUP_RESEND_AFTER_SECONDS = 60

ISSUER = "https://consumer.example.test"
AUDIENCE = "mindos-device-service"
REQUIRED_SCOPE = "mindos:access"

# 业务/隐私协议版本（Auth WP C「协议确认」）。
# 每次破坏性或需用户重新同意的协议变更递增；客户端登录需提起当前版本以完成
# 首次确认，过旧客户端需升级重确认——绝不静默绕过。
CURRENT_PROTOCOL_VERSION = 1
# 0 表示客户端未携带协议版本（旧版客户端）：视为隐式同意当前版本，向前兼容。
PROTOCOL_LEGACY = 0

MOCK_SMS_CODE = "123456"

# Step-up canonical digest（锚定到原型设计 §23.4）：
#   请求原文的固定 canonical 结构，用于把「单次使用、绑定 action/target/digest 的
#   stepUpToken」重放到原始敏感请求（client.revoke），拒绝换作他用。
STEPUP_BANNER = "NEXUSAOS-STEP-UP-V1"
STEPUP_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def stepup_request_digest(action: str, target_client_id: str, method: str, path: str) -> str:
    """Step-up 请求 canonical 摘要：绑定动作/目标/方法与路径的 SHA-256 lowercase hex。"""
    canonical = "\n".join([STEPUP_BANNER, action, target_client_id, method, path, STEPUP_EMPTY_SHA256])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS consumer_accounts (
  phone TEXT PRIMARY KEY,
  account_id TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_clients (
  client_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  name TEXT NOT NULL,
  created_at REAL NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0,
  revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_clients_account ON consumer_clients(account_id);
CREATE TABLE IF NOT EXISTS consumer_protocol_confirmations (
  seq INTEGER PRIMARY KEY,
  account_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  protocol_version INTEGER NOT NULL,
  client_name TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_protocol_account ON consumer_protocol_confirmations(account_id, client_id);
CREATE TABLE IF NOT EXISTS consumer_access (
  access_token TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_refresh (
  refresh_token TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_devices (
  device_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  ota_status TEXT NOT NULL,
  owner_account_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_sync_events (
  seq INTEGER PRIMARY KEY,
  account_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  device_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_events_account ON consumer_sync_events(account_id, seq);
CREATE TABLE IF NOT EXISTS consumer_connectivity (
  account_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  token TEXT NOT NULL,
  nonce TEXT NOT NULL,
  epoch_generation INTEGER NOT NULL,
  expires_at REAL NOT NULL,
  connect_before REAL NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (account_id, client_id, device_id)
);
CREATE TABLE IF NOT EXISTS consumer_revocations (
  seq INTEGER PRIMARY KEY,
  account_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  revoked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_stepup_challenges (
  challenge_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  expires_at REAL NOT NULL,
  consumed_at REAL
);
CREATE TABLE IF NOT EXISTS consumer_stepup_tokens (
  step_up_token TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  expires_at REAL NOT NULL,
  consumed_at REAL
);
CREATE TABLE IF NOT EXISTS consumer_seq_counter (
  key TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
"""

_ALL_TABLES = (
    "consumer_accounts",
    "consumer_clients",
    "consumer_protocol_confirmations",
    "consumer_access",
    "consumer_refresh",
    "consumer_devices",
    "consumer_sync_events",
    "consumer_connectivity",
    "consumer_revocations",
    "consumer_stepup_challenges",
    "consumer_stepup_tokens",
)


def _now() -> int:
    return int(time.time())


class ConsumerState:
    def __init__(self, db_path=None) -> None:
        self._lock = threading.RLock()
        self._path = ":memory:" if db_path is None else str(db_path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False, timeout=15, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute("INSERT OR IGNORE INTO consumer_seq_counter (key, value) VALUES ('global', 0)")
            self._conn.commit()

    def reset(self) -> None:
        with self._lock:
            for table in _ALL_TABLES:
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute("UPDATE consumer_seq_counter SET value=0 WHERE key='global'")
            self._conn.commit()

    def close(self) -> None:
        """释放 SQLite 连接（测试/关闭 Mock 服务器时调用；幂等）。"""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ---------- 账号 / Client / Session ----------

    def resolve_protocol(self, protocol_version: int) -> int:
        """校验客户端声明的协议版本，返回实际确认的版本。

        规则：
        - ``PROTOCOL_LEGACY``（0，未携带）视为隐式同意当前版本，向前兼容；
        - 与当前版本一致 → 确认当前版本；
        - 过旧（>0 且 < 当前）→ 抛 ``PROTOCOL_UPGRADE_REQUIRED``（升级重确认）；
        - 过新（> 当前）→ 抛 ``PROTOCOL_UNSUPPORTED``。
        """
        if protocol_version == PROTOCOL_LEGACY or protocol_version == CURRENT_PROTOCOL_VERSION:
            return CURRENT_PROTOCOL_VERSION
        if 0 < protocol_version < CURRENT_PROTOCOL_VERSION:
            raise ConsumerApiError(
                426, *ERROR_PROTOCOL_UPGRADE,
                details={"latestVersion": CURRENT_PROTOCOL_VERSION, "required": True},
            )
        raise ConsumerApiError(
            422, *ERROR_PROTOCOL_UNSUPPORTED,
            details={"latestVersion": CURRENT_PROTOCOL_VERSION},
        )

    def register_or_login(self, phone: str, client_name: str, protocol_version: int = PROTOCOL_LEGACY) -> dict:
        with self._lock:
            agreed = self.resolve_protocol(protocol_version)
            now = _now()
            row = self._conn.execute(
                "SELECT account_id FROM consumer_accounts WHERE phone=?", (phone,)
            ).fetchone()
            if row is None:
                account_id = f"acct_{uuid.uuid4().hex[:12]}"
                self._conn.execute(
                    "INSERT INTO consumer_accounts (phone, account_id, created_at) VALUES (?, ?, ?)",
                    (phone, account_id, now),
                )
            else:
                account_id = row["account_id"]
            client_id = self._new_client(account_id, client_name)
            self._record_protocol_confirmation(account_id, client_id, agreed, client_name)
            access_token, refresh_token = self._issue_tokens(account_id, client_id)
            return {
                "accountId": account_id,
                "clientId": client_id,
                "clientName": client_name,
                "accessToken": access_token,
                "refreshToken": refresh_token,
                "expiresIn": ACCESS_TTL_SECONDS,
                "accountExists": True,
                "protocol": {
                    "name": "mindos-consumer",
                    "version": agreed,
                    "latestVersion": CURRENT_PROTOCOL_VERSION,
                    "confirmed": True,
                },
            }

    def _record_protocol_confirmation(self, account_id: str, client_id: str, protocol_version: int, client_name: str) -> None:
        """记录一次协议确认的审计轨迹（账号/客户端/版本/名称/时间）。"""
        self._conn.execute(
            "INSERT INTO consumer_protocol_confirmations "
            "(seq, account_id, client_id, protocol_version, client_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self._next_seq(), account_id, client_id, protocol_version, client_name, _now()),
        )

    def protocol_info(self) -> dict:
        """返回当前协议版本（供客户端发现与升级重确认判断）。"""
        return {
            "name": "mindos-consumer",
            "version": CURRENT_PROTOCOL_VERSION,
            "latestVersion": CURRENT_PROTOCOL_VERSION,
        }

    def _new_client(self, account_id: str, name: str) -> str:
        client_id = f"client_{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            "INSERT INTO consumer_clients (client_id, account_id, name, created_at, epoch, revoked_at) "
            "VALUES (?, ?, ?, ?, 0, NULL)",
            (client_id, account_id, name, _now()),
        )
        return client_id

    def _issue_tokens(self, account_id: str, client_id: str) -> tuple[str, str]:
        now = _now()
        access_token = f"at_{secrets.token_hex(16)}"
        refresh_token = f"rt_{secrets.token_hex(16)}"
        self._conn.execute(
            "INSERT INTO consumer_access (access_token, account_id, client_id, expires_at) VALUES (?, ?, ?, ?)",
            (access_token, account_id, client_id, now + ACCESS_TTL_SECONDS),
        )
        self._conn.execute(
            "INSERT INTO consumer_refresh (refresh_token, account_id, client_id, expires_at) VALUES (?, ?, ?, ?)",
            (refresh_token, account_id, client_id, now + REFRESH_TTL_SECONDS),
        )
        return access_token, refresh_token

    def refresh_tokens(self, refresh_token: str) -> dict:
        with self._lock:
            session = self._conn.execute(
                "SELECT * FROM consumer_refresh WHERE refresh_token=?", (refresh_token,)
            ).fetchone()
            if session is None:
                raise ConsumerApiError(401, *ERROR_REFRESH_INVALID)
            if session["expires_at"] <= _now():
                raise ConsumerApiError(401, *ERROR_REFRESH_INVALID)
            client = self._conn.execute(
                "SELECT * FROM consumer_clients WHERE client_id=?", (session["client_id"],)
            ).fetchone()
            if client is None or client["revoked_at"] is not None:
                raise ConsumerApiError(401, *ERROR_CLIENT_REVOKED)
            access_token, _ = self._issue_tokens(session["account_id"], session["client_id"])
            return {
                "accessToken": access_token,
                "refreshToken": refresh_token,
                "expiresIn": ACCESS_TTL_SECONDS,
            }

    def logout(self, access_token: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM consumer_access WHERE access_token=?", (access_token,))

    def authenticate_access(self, access_token: str) -> dict:
        with self._lock:
            session = self._conn.execute(
                "SELECT * FROM consumer_access WHERE access_token=?", (access_token,)
            ).fetchone()
            if session is None:
                raise ConsumerApiError(401, *ERROR_AUTH_INVALID)
            if session["expires_at"] <= _now():
                raise ConsumerApiError(401, *ERROR_TOKEN_EXPIRED)
            client = self._conn.execute(
                "SELECT * FROM consumer_clients WHERE client_id=?", (session["client_id"],)
            ).fetchone()
            if client is None or client["revoked_at"] is not None:
                raise ConsumerApiError(401, *ERROR_CLIENT_REVOKED)
            return {
                "account_id": session["account_id"],
                "client_id": session["client_id"],
                "client_name": client["name"],
            }

    def list_clients(self, account_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM consumer_clients WHERE account_id=? ORDER BY created_at", (account_id,)
            ).fetchall()
            return [
                {
                    "clientId": c["client_id"],
                    "name": c["name"],
                    "createdAt": c["created_at"],
                    "revokedAt": c["revoked_at"],
                }
                for c in rows
            ]

    def revoke_client(self, account_id: str, client_id: str) -> dict:
        with self._lock:
            client = self._conn.execute(
                "SELECT * FROM consumer_clients WHERE client_id=?", (client_id,)
            ).fetchone()
            if client is None or client["account_id"] != account_id:
                raise ConsumerApiError(404, *ERROR_CLIENT_NOT_FOUND)
            now = _now()
            new_epoch = int(client["epoch"]) + 1
            self._conn.execute(
                "UPDATE consumer_clients SET revoked_at=?, epoch=? WHERE client_id=?",
                (now, new_epoch, client_id),
            )
            closed = 0
            revoked_entries: list[dict] = []
            rows = self._conn.execute(
                "SELECT * FROM consumer_connectivity WHERE client_id=?", (client_id,)
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "DELETE FROM consumer_connectivity WHERE account_id=? AND client_id=? AND device_id=?",
                    (row["account_id"], client_id, row["device_id"]),
                )
                if row["expires_at"] > now:
                    closed += 1
                entry = {
                    "seq": self._next_seq(),
                    "account_id": account_id,
                    "client_id": client_id,
                    "device_id": row["device_id"],
                    "revoked_at": now,
                }
                revoked_entries.append(entry)
                self._conn.execute(
                    "INSERT INTO consumer_revocations (seq, account_id, client_id, device_id, revoked_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (entry["seq"], account_id, client_id, row["device_id"], now),
                )
            return {"newEpoch": new_epoch, "closedSessions": closed, "revocations": revoked_entries}

    def _next_seq(self) -> int:
        self._conn.execute("UPDATE consumer_seq_counter SET value = value + 1 WHERE key = 'global'")
        row = self._conn.execute("SELECT value FROM consumer_seq_counter WHERE key='global'").fetchone()
        return int(row["value"])

    # ---------- Step-up（敏感操作二次验证）----------

    def begin_stepup(self, account_id: str, client_id: str, action: str, target: dict, request_digest: str) -> dict:
        """发起一次敏感操作验证，生成一次性 challenge。返回 {challengeId, expiresIn, resendAfter}。"""
        with self._lock:
            challenge_id = f"ch_{secrets.token_hex(12)}"
            now = _now()
            self._conn.execute(
                "INSERT INTO consumer_stepup_challenges "
                "(challenge_id, account_id, client_id, action, target, request_digest, expires_at, consumed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (challenge_id, account_id, client_id, action,
                 json.dumps(target, ensure_ascii=False), request_digest, now + STEPUP_TTL_SECONDS),
            )
            return {
                "challengeId": challenge_id,
                "expiresIn": STEPUP_TTL_SECONDS,
                "resendAfter": STEPUP_RESEND_AFTER_SECONDS,
                # Strict Mock 才返回固定验证码；发布包整体排除 consumer_api。
                "devOnlyCode": MOCK_SMS_CODE,
            }

    def verify_stepup(self, account_id: str, client_id: str, challenge_id: str, code: str) -> dict:
        """校验验证码并换发单次使用、绑定当前会话的 stepUpToken。"""
        with self._lock:
            if code != MOCK_SMS_CODE:
                raise ConsumerApiError(400, *ERROR_SMS_CODE)
            row = self._conn.execute(
                "SELECT * FROM consumer_stepup_challenges WHERE challenge_id=?", (challenge_id,)
            ).fetchone()
            if row is None:
                raise ConsumerApiError(403, *ERROR_STEP_UP_INVALID)
            if row["account_id"] != account_id or row["client_id"] != client_id:
                raise ConsumerApiError(403, *ERROR_STEP_UP_INVALID)
            if row["consumed_at"] is not None:
                raise ConsumerApiError(403, *ERROR_STEP_UP_INVALID)
            if row["expires_at"] <= _now():
                raise ConsumerApiError(403, *ERROR_STEP_UP_INVALID)
            now = _now()
            self._conn.execute(
                "UPDATE consumer_stepup_challenges SET consumed_at=? WHERE challenge_id=?",
                (now, challenge_id),
            )
            token = f"su_{secrets.token_hex(16)}"
            expires_at = now + STEPUP_TTL_SECONDS
            self._conn.execute(
                "INSERT INTO consumer_stepup_tokens "
                "(step_up_token, account_id, client_id, action, target, request_digest, expires_at, consumed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (token, account_id, client_id, row["action"], row["target"],
                 row["request_digest"], expires_at),
            )
            target_digest = hashlib.sha256(
                json.dumps(json.loads(row["target"]), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            return {
                "stepUpToken": token,
                "expiresAt": expires_at,
                "action": row["action"],
                "targetDigest": target_digest,
            }

    def redeem_client_revoke(
        self,
        *,
        account_id: str,
        current_client_id: str,
        step_up_token: str,
        target_client_id: str,
        request_digest: str,
    ) -> dict:
        """消费 stepUpToken 执行「移除其他终端」：仅重放原始 client.revoke 动作。

        Token 单次使用，绑定当前会话/action/target/request_digest；不符合即视为
        换作他用的敏感操作，拒绝执行。不移除当前 Client（退出走 logout）。
        """
        with self._lock:
            token = self._conn.execute(
                "SELECT * FROM consumer_stepup_tokens WHERE step_up_token=?", (step_up_token,)
            ).fetchone()
            if token is None:
                raise ConsumerApiError(403, *ERROR_STEP_UP_INVALID)
            if token["account_id"] != account_id or token["client_id"] != current_client_id:
                raise ConsumerApiError(403, *ERROR_STEP_UP_INVALID)
            if token["consumed_at"] is not None:
                raise ConsumerApiError(403, *ERROR_STEP_UP_INVALID)
            if token["expires_at"] <= _now():
                raise ConsumerApiError(403, *ERROR_STEP_UP_INVALID)
            if token["action"] != "client.revoke":
                raise ConsumerApiError(403, *ERROR_STEP_UP_INVALID)
            if json.loads(token["target"]).get("clientId") != target_client_id:
                raise ConsumerApiError(403, *ERROR_STEP_UP_INVALID)
            if token["request_digest"] != request_digest:
                raise ConsumerApiError(403, *ERROR_STEP_UP_INVALID)
            if target_client_id == current_client_id:
                raise ConsumerApiError(400, *ERROR_STEP_UP_INVALID)
            now = _now()
            self._conn.execute(
                "UPDATE consumer_stepup_tokens SET consumed_at=? WHERE step_up_token=?",
                (now, step_up_token),
            )
            result = self.revoke_client(account_id, target_client_id)
            return {"revokedClientId": target_client_id, "revokedAt": now, **result}

    # ---------- 设备 / 所有权 ----------

    def create_device(self, device_id: str, name: str, ota_status: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM consumer_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if row is not None:
                return dict(row)
            now = _now()
            device = {
                "device_id": device_id,
                "name": name or f"AI盒子-{device_id[-4:]}",
                "ota_status": ota_status,
                "owner_account_id": None,
                "created_at": now,
                "updated_at": now,
            }
            self._conn.execute(
                "INSERT INTO consumer_devices (device_id, name, ota_status, owner_account_id, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, ?, ?)",
                (device_id, device["name"], ota_status, now, now),
            )
            return dict(device)

    def list_devices(self, account_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM consumer_devices WHERE owner_account_id=? ORDER BY created_at", (account_id,)
            ).fetchall()
            return [self._public_device(d) for d in rows]

    def get_device(self, account_id: str, device_id: str) -> dict:
        with self._lock:
            device = self._conn.execute(
                "SELECT * FROM consumer_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if device is None:
                raise ConsumerApiError(404, *ERROR_DEVICE_NOT_FOUND)
            if device["owner_account_id"] != account_id:
                raise ConsumerApiError(403, *ERROR_DEVICE_NOT_OWNED)
            return self._public_device(device)

    @staticmethod
    def _public_device(device) -> dict:
        return {
            "deviceId": device["device_id"],
            "name": device["name"],
            "otaStatus": device["ota_status"],
            "ownerAccountId": device["owner_account_id"],
            "createdAt": device["created_at"],
            "updatedAt": device["updated_at"],
        }

    def claim_device(self, account_id: str, device_id: str, idempotency_key: str) -> dict:
        with self._lock:
            device = self._conn.execute(
                "SELECT * FROM consumer_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if device is None:
                raise ConsumerApiError(404, *ERROR_DEVICE_NOT_FOUND)
            if device["owner_account_id"] is not None and device["owner_account_id"] != account_id:
                raise ConsumerApiError(409, *ERROR_DEVICE_ALREADY_OWNED)
            if device["owner_account_id"] == account_id:
                return self._public_device(device)
            self._conn.execute(
                "UPDATE consumer_devices SET owner_account_id=?, updated_at=? WHERE device_id=?",
                (account_id, _now(), device_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM consumer_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            self._emit(account_id, "device_added", device_id, {"device": self._public_device(updated)})
            return self._public_device(updated)

    def rename_device(self, account_id: str, device_id: str, name: str) -> dict:
        with self._lock:
            device = self._conn.execute(
                "SELECT * FROM consumer_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if device is None:
                raise ConsumerApiError(404, *ERROR_DEVICE_NOT_FOUND)
            if device["owner_account_id"] != account_id:
                raise ConsumerApiError(403, *ERROR_DEVICE_NOT_OWNED)
            self._conn.execute(
                "UPDATE consumer_devices SET name=?, updated_at=? WHERE device_id=?",
                (name, _now(), device_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM consumer_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            self._emit(account_id, "device_renamed", device_id, {"name": name})
            return self._public_device(updated)

    def update_device_ota(self, account_id: str, device_id: str, ota_status: str) -> dict:
        with self._lock:
            device = self._conn.execute(
                "SELECT * FROM consumer_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if device is None:
                raise ConsumerApiError(404, *ERROR_DEVICE_NOT_FOUND)
            if device["owner_account_id"] != account_id:
                raise ConsumerApiError(403, *ERROR_DEVICE_NOT_OWNED)
            self._conn.execute(
                "UPDATE consumer_devices SET ota_status=?, updated_at=? WHERE device_id=?",
                (ota_status, _now(), device_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM consumer_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            self._emit(account_id, "device_ota_status", device_id, {"otaStatus": ota_status})
            return self._public_device(updated)

    # ---------- 同步 ----------

    def _emit(self, account_id: str, event_type: str, device_id: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO consumer_sync_events (seq, account_id, event_type, device_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self._next_seq(), account_id, event_type, device_id,
             json.dumps(payload, ensure_ascii=False), _now()),
        )

    @staticmethod
    def _event_dict(row) -> dict:
        return {
            "seq": row["seq"],
            "type": row["event_type"],
            "device_id": row["device_id"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }

    def bootstrap(self, account_id: str) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM consumer_sync_events WHERE account_id=? ORDER BY seq", (account_id,)
            ).fetchall()
            events = [self._event_dict(e) for e in rows]
            return {"cursor": events[-1]["seq"] if events else 0, "events": events}

    def changes(self, account_id: str, cursor: int) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM consumer_sync_events WHERE account_id=? AND seq > ? ORDER BY seq",
                (account_id, cursor),
            ).fetchall()
            events = [self._event_dict(e) for e in rows]
            return {"cursor": events[-1]["seq"] if events else cursor, "events": events, "hasMore": False}

    # ---------- 连接票据 ----------

    def create_connectivity_session(self, account_id: str, client_id: str, device_id: str, idempotency_key: str) -> dict:
        with self._lock:
            self.get_device(account_id, device_id)
            existing = self._conn.execute(
                "SELECT * FROM consumer_connectivity WHERE account_id=? AND client_id=? AND device_id=?",
                (account_id, client_id, device_id),
            ).fetchone()
            now = _now()
            if existing is not None and existing["idempotency_key"] == idempotency_key and existing["expires_at"] > now:
                return self._public_ticket(existing)
            if existing is not None and existing["expires_at"] > now:
                # 单 active session：不同 Key 且原 Session 仍 active 时返回它。
                return self._public_ticket(existing)

            nonce = secrets.token_hex(16)
            token, expires_at, connect_before = signing.issue_ticket(
                issuer=ISSUER,
                audience=AUDIENCE,
                device_id=device_id,
                account_id=account_id,
                client_id=client_id,
                scope=REQUIRED_SCOPE,
                nonce=nonce,
                epoch_generation=self._client_epoch(client_id),
                ttl_seconds=TICKET_TTL_SECONDS,
                connect_before_seconds=TICKET_CONNECT_BEFORE_SECONDS,
            )
            entry = {
                "idempotency_key": idempotency_key,
                "token": token,
                "nonce": nonce,
                "epoch_generation": self._client_epoch(client_id),
                "expires_at": expires_at,
                "connect_before": connect_before,
                "created_at": now,
            }
            self._conn.execute(
                "INSERT OR REPLACE INTO consumer_connectivity "
                "(account_id, client_id, device_id, idempotency_key, token, nonce, epoch_generation, "
                "expires_at, connect_before, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, client_id, device_id, idempotency_key, token, nonce,
                 entry["epoch_generation"], expires_at, connect_before, now),
            )
            return self._public_ticket(entry)

    def _client_epoch(self, client_id: str) -> int:
        row = self._conn.execute(
            "SELECT epoch FROM consumer_clients WHERE client_id=?", (client_id,)
        ).fetchone()
        return int(row["epoch"]) if row else 0

    @staticmethod
    def _public_ticket(entry) -> dict:
        return {
            "ticket": entry["token"],
            "nonce": entry["nonce"],
            "epochGeneration": entry["epoch_generation"],
            "expiresAt": entry["expires_at"],
            "connectBefore": entry["connect_before"],
        }

    # ---------- Mock 管理 ----------

    def revocations_since(self, seq: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM consumer_revocations WHERE seq > ? ORDER BY seq", (seq,)
            ).fetchall()
            return [
                {
                    "seq": e["seq"],
                    "accountId": e["account_id"],
                    "clientId": e["client_id"],
                    "deviceId": e["device_id"],
                    "revokedAt": e["revoked_at"],
                }
                for e in rows
            ]

    def snapshot(self) -> dict:
        with self._lock:
            accounts = self._conn.execute("SELECT COUNT(*) AS n FROM consumer_accounts").fetchone()["n"]
            clients = self._conn.execute(
                "SELECT * FROM consumer_clients ORDER BY created_at"
            ).fetchall()
            devices = self._conn.execute("SELECT * FROM consumer_devices ORDER BY created_at").fetchall()
            active = self._conn.execute(
                "SELECT COUNT(*) AS n FROM consumer_connectivity WHERE expires_at > ?", (_now(),)
            ).fetchone()["n"]
            revocations = self._conn.execute("SELECT COUNT(*) AS n FROM consumer_revocations").fetchone()["n"]
            return {
                "accounts": accounts,
                "clients": [
                    {
                        "clientId": c["client_id"],
                        "accountId": c["account_id"],
                        "revokedAt": c["revoked_at"],
                        "epoch": c["epoch"],
                    }
                    for c in clients
                ],
                "devices": [self._public_device(d) for d in devices],
                "activeConnectivitySessions": active,
                "revocationCount": revocations,
            }


def open_persistent_state() -> ConsumerState:
    """Mock 服务器使用的文件持久化状态（跨重启保留）。"""
    return ConsumerState(db_path=CONSUMER_MOCK_DB_PATH)
