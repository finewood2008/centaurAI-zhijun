"""模型运行时设置持久化（P1 §5.1 / §5.2 / §6.1）。

保存非密钥配置、`secret_ref`、版本与更新时间；提供 revision 乐观锁、原子更新与
启动迁移 bootstrap 元数据。密钥原文绝不落入本库；密文仅存于独立
`encrypted_secrets` 表，解密主密钥不写入 SQLite。

- 每个 section 一行（`material_runtime` / `chat_provider`），各自独立 revision；
- 空状态（无任何覆盖）由调用方解释为 `revision: 0`、`source: "defaults"`；
- `runtime_meta` 保存受控元数据（迁移 bootstrap 等），不参与页面可编辑配置与
  revision 乐观锁。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

from runtime_paths import RUNTIME_SETTINGS_DB_PATH

# section 常量
SECTION_MATERIAL = "material_runtime"
SECTION_CHAT = "chat_provider"
SECTION_KEYS = (SECTION_MATERIAL, SECTION_CHAT)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_settings (
    section TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    secret_ref TEXT,
    source TEXT NOT NULL DEFAULT 'runtime_settings',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS external_provider_profiles (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    secret_ref TEXT,
    updated_at REAL NOT NULL
);
-- 本应用创建的 secret_ref 台账：跨后端（keyring 无法列举自有 ref）提供可恢复的孤儿清理。
CREATE TABLE IF NOT EXISTS secret_refs (
    ref TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
-- API Key 密文：仅应用自管 Fernet store 访问，绝不通过配置/诊断/API 投影返回。
CREATE TABLE IF NOT EXISTS encrypted_secrets (
    ref TEXT PRIMARY KEY,
    ciphertext BLOB NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
-- 应用管理的对称密钥元数据；仅便利模式使用，部署主密钥可由环境变量覆盖。
CREATE TABLE IF NOT EXISTS secret_store_metadata (
    key TEXT PRIMARY KEY,
    value BLOB NOT NULL
);
"""


class RevisionConflictError(ValueError):
    """并发保存冲突：携带最新脱敏配置供客户端刷新。"""

    def __init__(self, latest: dict) -> None:
        super().__init__("配置已被其他会话更新，请刷新后重试")
        self.latest = latest


class ActiveProviderError(ValueError):
    pass


class RuntimeSettingsStore:
    _instance: "RuntimeSettingsStore | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, db_path=None) -> None:
        self._db_path = Path(db_path) if db_path is not None else RUNTIME_SETTINGS_DB_PATH
        self._initialized = False
        self._lock = threading.Lock()
        self._ensure()

    @classmethod
    def instance(cls) -> "RuntimeSettingsStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = RuntimeSettingsStore()
            return cls._instance

    # ---- SQLite helpers ----

    def _connect(self) -> sqlite3.Connection:
        # A test data root, restored data root, or administrator-managed mount
        # can disappear after this singleton was initialized.  Do not keep a
        # stale `_initialized=True` flag that makes every model call fail.
        if not self._db_path.is_file():
            with self._lock:
                self._initialized = False
        self._ensure()
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                # Only a persisted legacy channel is imported; no environment
                # token is copied and GET never creates a provider profile.
                conn.execute("BEGIN IMMEDIATE")
                migrated = conn.execute("SELECT 1 FROM runtime_meta WHERE key='external_profiles_migrated'").fetchone()
                legacy = conn.execute("SELECT payload,secret_ref FROM runtime_settings WHERE section=?", (SECTION_CHAT,)).fetchone()
                if not migrated and legacy:
                    payload = json.loads(legacy[0])
                    if payload.get("provider") == "openai" and payload.get("baseUrl") and not payload.get("externalProviderId"):
                        ident = "ext_" + uuid.uuid4().hex[:16]
                        profile = {"name": "已配置的在线服务", "baseUrl": payload["baseUrl"], "model": payload.get("model")}
                        conn.execute("INSERT INTO external_provider_profiles VALUES(?,1,?,?,?)", (ident, json.dumps(profile, ensure_ascii=False), legacy[1], time.time()))
                        payload.update(externalProviderId=ident, externalProviderRevision=1)
                        conn.execute("UPDATE runtime_settings SET payload=? WHERE section=?", (json.dumps(payload, ensure_ascii=False), SECTION_CHAT))
                    conn.execute("INSERT INTO runtime_meta VALUES('external_profiles_migrated','{}',?)", (time.time(),))
                conn.commit()
            finally:
                conn.close()
            self._initialized = True

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        return {
            "section": row["section"],
            "revision": int(row["revision"]),
            "payload": json.loads(row["payload"] or "{}"),
            "secret_ref": row["secret_ref"],
            "source": row["source"],
            "updated_at": row["updated_at"],
        }

    # ---- 配置读写 ----

    def get_section(self, section: str) -> dict | None:
        if section not in SECTION_KEYS:
            raise ValueError(f"unknown runtime settings section: {section}")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM runtime_settings WHERE section=?", (section,)
            ).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def list_sections(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM runtime_settings").fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _profile(row):
        return {"id": row["id"], "revision": row["revision"], "payload": json.loads(row["payload"]),
                "secret_ref": row["secret_ref"], "updated_at": row["updated_at"]} if row else None

    def list_external_profiles(self):
        with self._connect() as db:
            return [self._profile(r) for r in db.execute("SELECT * FROM external_provider_profiles ORDER BY updated_at,id")]

    def get_external_profile(self, ident):
        with self._connect() as db:
            return self._profile(db.execute("SELECT * FROM external_provider_profiles WHERE id=?", (ident,)).fetchone())

    def put_external_profile(self, ident, expected_revision, payload, secret_ref):
        creating = ident is None
        ident = ident or "ext_" + uuid.uuid4().hex[:16]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._profile(db.execute("SELECT * FROM external_provider_profiles WHERE id=?", (ident,)).fetchone())
            if not creating and not old:
                raise KeyError("供应商不存在")
            if old and expected_revision != old["revision"]:
                raise RevisionConflictError(old)
            revision = old["revision"] + 1 if old else 1
            db.execute("INSERT OR REPLACE INTO external_provider_profiles VALUES(?,?,?,?,?)",
                       (ident, revision, json.dumps(payload, ensure_ascii=False), secret_ref, time.time()))
            return self._profile(db.execute("SELECT * FROM external_provider_profiles WHERE id=?", (ident,)).fetchone())

    def activate_external_profile(self, ident, expected_revision, model, chat_revision, defaults):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            profile = self._profile(db.execute("SELECT * FROM external_provider_profiles WHERE id=?", (ident,)).fetchone())
            if not profile:
                raise KeyError("供应商不存在")
            if profile["revision"] != expected_revision:
                raise RevisionConflictError(profile)
            row = db.execute("SELECT * FROM runtime_settings WHERE section=?", (SECTION_CHAT,)).fetchone()
            actual = row["revision"] if row else 0
            if chat_revision != actual:
                raise RevisionConflictError(self._row_to_dict(row) if row else {"revision": 0})
            if not profile["secret_ref"]:
                raise ValueError("供应商尚未配置 API Key")
            now, revision = time.time(), profile["revision"] + 1
            payload = {**profile["payload"], "model": model}
            db.execute("UPDATE external_provider_profiles SET revision=?,payload=?,updated_at=? WHERE id=?",
                       (revision, json.dumps(payload, ensure_ascii=False), now, ident))
            chat = {**defaults, **(json.loads(row["payload"]) if row else {}), "provider": "openai", "externalEnabled": True,
                    "baseUrl": payload["baseUrl"], "model": model, "fallbackOllama": False,
                    "externalProviderId": ident, "externalProviderRevision": revision}
            db.execute("INSERT OR REPLACE INTO runtime_settings VALUES(?,?,?,?,?,?)",
                       (SECTION_CHAT, actual + 1, json.dumps(chat, ensure_ascii=False), profile["secret_ref"], "runtime_settings", now))
            return self._profile(db.execute("SELECT * FROM external_provider_profiles WHERE id=?", (ident,)).fetchone())

    def delete_external_profile(self, ident, expected_revision):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._profile(db.execute("SELECT * FROM external_provider_profiles WHERE id=?", (ident,)).fetchone())
            if not old:
                raise KeyError("供应商不存在")
            if old["revision"] != expected_revision:
                raise RevisionConflictError(old)
            chat = db.execute("SELECT payload FROM runtime_settings WHERE section=?", (SECTION_CHAT,)).fetchone()
            if chat and json.loads(chat[0]).get("externalProviderId") == ident:
                raise ActiveProviderError("当前使用的供应商不能删除，请先切换到其他服务或本地模型")
            db.execute("DELETE FROM external_provider_profiles WHERE id=?", (ident,))
            return old

    def referenced_secret_refs(self):
        with self._connect() as db:
            return {row[0] for row in db.execute("SELECT secret_ref FROM runtime_settings UNION SELECT secret_ref FROM external_provider_profiles") if row[0]}

    def put_section(
        self,
        section: str,
        expected_revision: int | None,
        payload: dict,
        *,
        secret_ref: str | None = None,
    ) -> dict:
        """原子写入并递增 revision（乐观锁）。

        - 已存在行：`expected_revision` 必须等于当前 revision，否则抛
          `RevisionConflictError`（携带最新配置）；
        - 空状态：`expected_revision` 必须为 None/0，写入后 revision=1。
        """
        if section not in SECTION_KEYS:
            raise ValueError(f"unknown runtime settings section: {section}")
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM runtime_settings WHERE section=?", (section,)
            ).fetchone()
            if row is not None:
                current_rev = int(row["revision"])
                # 已存在配置：必须携带与当前一致的 revision（None 视为未携带 → 冲突），
                # 防止「首次可省略、后续必须携带」的约定被静默覆盖。
                if expected_revision is None or expected_revision != current_rev:
                    conn.rollback()
                    raise RevisionConflictError(self._row_to_dict(row))
                conn.execute(
                    "UPDATE runtime_settings SET revision=revision+1, payload=?, secret_ref=?, "
                    "source='runtime_settings', updated_at=? WHERE section=?",
                    (json.dumps(payload, ensure_ascii=False), secret_ref, now, section),
                )
                new_rev = current_rev + 1
            else:
                if expected_revision is not None and expected_revision != 0:
                    conn.rollback()
                    raise RevisionConflictError(
                        {
                            "section": section,
                            "revision": 0,
                            "payload": {},
                            "secret_ref": None,
                            "source": "defaults",
                            "updated_at": 0,
                        }
                    )
                conn.execute(
                    "INSERT INTO runtime_settings(section, revision, payload, secret_ref, source, updated_at) "
                    "VALUES (?, 1, ?, ?, 'runtime_settings', ?)",
                    (section, json.dumps(payload, ensure_ascii=False), secret_ref, now),
                )
                new_rev = 1
            conn.commit()
        except RevisionConflictError:
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "section": section,
            "revision": new_rev,
            "payload": dict(payload),
            "secret_ref": secret_ref,
            "source": "runtime_settings",
            "updated_at": now,
        }

    # ---- 受控元数据（迁移 bootstrap 等，不计入 revision） ----

    def get_meta(self, key: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM runtime_meta WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row["value"]) if row else None
        finally:
            conn.close()

    def put_meta(self, key: str, value: dict) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO runtime_meta(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    # ---- secret_ref 台账（§5.2.1 可恢复孤儿清理） ----

    def add_secret_ref(self, ref: str, created_at: float | None = None) -> None:
        """记录本应用创建的 secret_ref（写密钥之前落账，崩溃后可据此回收）。"""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO secret_refs(ref, created_at) VALUES (?, ?)",
                (ref, time.time() if created_at is None else created_at),
            )
            conn.commit()
        finally:
            conn.close()

    def remove_secret_ref(self, ref: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM secret_refs WHERE ref=?", (ref,))
            conn.commit()
        finally:
            conn.close()

    def list_ledger_refs(self) -> list[dict]:
        """返回台账全部 ref 及创建时间（孤儿回收按此而非枚举密钥后端）。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT ref, created_at FROM secret_refs ORDER BY created_at"
            ).fetchall()
            return [{"ref": r["ref"], "created_at": r["created_at"]} for r in rows]
        finally:
            conn.close()

    # ---- 测试 ----

    def close(self) -> None:
        self._initialized = False


def reset_for_tests(db_path=None) -> RuntimeSettingsStore:
    """测试用：切换独立 DB 并重建全局实例。"""
    global RuntimeSettingsStore
    store = RuntimeSettingsStore(db_path=db_path)
    store._initialized = False
    store._ensure()
    RuntimeSettingsStore._instance = store
    # RuntimeConfigProvider caches the store instance.  Test suites switch the
    # database repeatedly; retaining a provider that points at a deleted
    # TemporaryDirectory makes otherwise unrelated tests (and reload-style
    # process tests) fail with "unable to open database file".
    provider_module = sys.modules.get("mindos.runtime_config_provider")
    reset_provider = getattr(provider_module, "reset_provider_for_tests", None)
    if callable(reset_provider):
        reset_provider()
    return store
