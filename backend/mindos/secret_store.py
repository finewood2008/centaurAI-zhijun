"""运行时密钥存储抽象（P1 §5.3 / §5.2.1）。

`CENTAUR_QA_AI_API_KEY` 不得原文落入常规 SQLite 配置表、日志、任务表、前端状态
或备份清单。对外只暴露 `secret_ref`（带 MindOS 前缀的不可预测引用）。

实现选择：
- `get_default_secret_store()`：应用自管 SQLite 加密 store（Fernet）。未配置
  `CENTAUR_SECRET_STORE_KEY` 时首次自动生成并持久化应用密钥；测试注入
  `MemorySecretStore`。
- 孤儿回收只清理带本应用前缀的引用，绝不枚举或清理凭据库中其他应用密钥。
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path

from runtime_paths import RUNTIME_SETTINGS_DB_PATH, SECRET_STORE_DIR

# 本应用创建的 secret_ref 前缀；孤儿回收只清理该前缀的引用。
SECRET_PREFIX = "mindos_rt_"
# 无引用密钥的默认保留期（秒）：异步清理窗口 + 重启孤儿回收的安全边界。
_REF_RETENTION_SECONDS = 7 * 24 * 3600


def new_secret_ref() -> str:
    """生成不可预测、带 MindOS 前缀的 secret_ref。"""
    return SECRET_PREFIX + secrets.token_hex(16)


class SecretStore(ABC):
    @abstractmethod
    def set_secret(self, ref: str, value: str) -> None: ...

    @abstractmethod
    def get_secret(self, ref: str) -> str | None: ...

    @abstractmethod
    def delete_secret(self, ref: str) -> None: ...

    @abstractmethod
    def list_secret_refs(self) -> list[str]: ...


class MemorySecretStore(SecretStore):
    """测试/进程内实现；不持久化。"""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def set_secret(self, ref: str, value: str) -> None:
        self._data[ref] = value

    def get_secret(self, ref: str) -> str | None:
        return self._data.get(ref)

    def delete_secret(self, ref: str) -> None:
        self._data.pop(ref, None)

    def list_secret_refs(self) -> list[str]:
        return [r for r in self._data if r.startswith(SECRET_PREFIX)]


class UnavailableSecretStore(SecretStore):
    """无可用密钥后端（如仅有 .env 的生产部署）：页面只读，不提供修改/清除按钮。

    写操作抛出可操作的错误；读返回 None；列表为空（不存在本应用可回收的密钥）。
    """

    def set_secret(self, ref: str, value: str) -> None:
        raise RuntimeError("密钥存储不可用：API Key 仅由部署环境提供，页面只读")

    def get_secret(self, ref: str) -> str | None:
        return None

    def delete_secret(self, ref: str) -> None:
        return None

    def list_secret_refs(self) -> list[str]:
        return []


class KeyringSecretStore(SecretStore):
    """操作系统凭据库（keyring 可选依赖）。"""

    _SERVICE = "mindos-runtime-settings"

    def __init__(self) -> None:
        import keyring  # 可选依赖；不可用时应走加密文件 store 或注入内存实现
        self._keyring = keyring
        try:
            self._keyring.get_password(self._SERVICE, "probe")
        except Exception:  # keyring 后端不可用（如无头 Linux 无 DBus）时尽早暴露
            raise RuntimeError("keyring 后端不可用，请配置加密文件 secret store")

    def set_secret(self, ref: str, value: str) -> None:
        self._keyring.set_password(self._SERVICE, ref, value)

    def get_secret(self, ref: str) -> str | None:
        return self._keyring.get_password(self._SERVICE, ref)

    def delete_secret(self, ref: str) -> None:
        try:
            self._keyring.delete_password(self._SERVICE, ref)
        except Exception:
            pass  # 不存在视为删除成功（幂等）

    def list_secret_refs(self) -> list[str]:
        # keyring 不提供按前缀列举；调用方应结合运行时设置库的 secret_ref 交集处理。
        return []


def _coerce_fernet_key(raw: str) -> bytes:
    """把部署提供的密钥规范化为 Fernet key。

    合法 Fernet key（urlsafe base64，32 字节）直接使用；否则对任意口令做 SHA-256
    派生，保证部署只需提供一个秘密串即可。
    """
    s = raw.strip()
    try:
        decoded = base64.urlsafe_b64decode(s.encode() + b"==")
        if len(decoded) == 32:
            return s.encode()
    except Exception:
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(s.encode()).digest())


class EncryptedFileSecretStore(SecretStore):
    """数据根之外的加密文件 store（Fernet）。

    - 目录默认 `CENTAUR_SECRET_STORE_DIR`（默认位于数据根之外）；
    - 每个 ref 一个 `<ref>.enc` 文件，内容为 Fernet 密文；
    - 创建时间用文件 mtime 近似，供孤儿回收按保留期过滤。
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        master_key_b64: str | None = None,
    ) -> None:
        from cryptography.fernet import Fernet, InvalidToken  # 已随 OAuth 依赖引入

        self._dir = Path(directory) if directory else SECRET_STORE_DIR
        key = master_key_b64 or os.environ.get("CENTAUR_SECRET_STORE_KEY")
        if not key:
            raise RuntimeError(
                "加密文件 secret store 需要部署环境提供 CENTAUR_SECRET_STORE_KEY"
            )
        self._fernet = Fernet(_coerce_fernet_key(key))
        self._InvalidToken = InvalidToken
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, ref: str) -> Path:
        return self._dir / f"{ref}.enc"

    def set_secret(self, ref: str, value: str) -> None:
        self._path(ref).write_bytes(self._fernet.encrypt(value.encode("utf-8")))

    def get_secret(self, ref: str) -> str | None:
        path = self._path(ref)
        if not path.exists():
            return None
        try:
            return self._fernet.decrypt(path.read_bytes()).decode("utf-8")
        except self._InvalidToken:
            return None

    def delete_secret(self, ref: str) -> None:
        self._path(ref).unlink(missing_ok=True)

    def list_secret_refs(self) -> list[str]:
        return [p.stem for p in self._dir.glob("*.enc") if p.stem.startswith(SECRET_PREFIX)]

    def ref_age_seconds(self, ref: str) -> float | None:
        path = self._path(ref)
        if not path.exists():
            return None
        try:
            return time.time() - path.stat().st_mtime
        except OSError:
            return None


class EncryptedSQLiteSecretStore(SecretStore):
    """应用自管的 SQLite 密文存储。

    未配置部署主密钥时，首次启动在 SQLite 中生成并持久化一把随机 Fernet 密钥，
    因此页面可直接保存并在重启后恢复。此便利模式避免 API Key 明文落库，但不防护
    完整数据库文件被复制后的离线读取；部署可通过 CENTAUR_SECRET_STORE_KEY 覆盖。
    该实现不依赖 Windows/Linux 凭据库或其他系统级密钥服务。
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        master_key_b64: str | None = None,
    ) -> None:
        import sqlite3
        from cryptography.fernet import Fernet, InvalidToken

        self._sqlite3 = sqlite3
        self._db_path = Path(db_path) if db_path else RUNTIME_SETTINGS_DB_PATH
        key = master_key_b64 or os.environ.get("CENTAUR_SECRET_STORE_KEY")
        self._InvalidToken = InvalidToken
        self._ensure()
        self._fernet = Fernet(
            _coerce_fernet_key(key) if key else self._load_or_create_managed_key(Fernet)
        )

    def _connect(self):
        conn = self._sqlite3.connect(str(self._db_path), timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS encrypted_secrets ("
                "ref TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS secret_store_metadata ("
                "key TEXT PRIMARY KEY, value BLOB NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

    def _load_or_create_managed_key(self, fernet_cls) -> bytes:
        """返回 SQLite 内置应用密钥；BEGIN IMMEDIATE 防止并发首次启动产生两把密钥。"""
        metadata_key = "managed_fernet_key_v1"
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM secret_store_metadata WHERE key=?", (metadata_key,)
            ).fetchone()
            if row:
                conn.commit()
                return bytes(row[0])
            key = fernet_cls.generate_key()
            conn.execute(
                "INSERT INTO secret_store_metadata(key, value) VALUES (?, ?)",
                (metadata_key, key),
            )
            conn.commit()
            return key
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def set_secret(self, ref: str, value: str) -> None:
        if not ref.startswith(SECRET_PREFIX):
            raise ValueError("secret ref 不属于 MindOS")
        now = time.time()
        ciphertext = self._fernet.encrypt(value.encode("utf-8"))
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO encrypted_secrets(ref, ciphertext, created_at, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(ref) DO UPDATE SET ciphertext=excluded.ciphertext, "
                "updated_at=excluded.updated_at",
                (ref, ciphertext, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def get_secret(self, ref: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT ciphertext FROM encrypted_secrets WHERE ref=?", (ref,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        try:
            return self._fernet.decrypt(bytes(row[0])).decode("utf-8")
        except self._InvalidToken:
            return None

    def delete_secret(self, ref: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM encrypted_secrets WHERE ref=?", (ref,))
            conn.commit()
        finally:
            conn.close()

    def list_secret_refs(self) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT ref FROM encrypted_secrets").fetchall()
            return [str(row[0]) for row in rows if str(row[0]).startswith(SECRET_PREFIX)]
        finally:
            conn.close()

    def ref_age_seconds(self, ref: str) -> float | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT created_at FROM encrypted_secrets WHERE ref=?", (ref,)
            ).fetchone()
        finally:
            conn.close()
        return max(0.0, time.time() - float(row[0])) if row else None


def get_default_secret_store() -> SecretStore:
    """默认使用 SQLite 加密 store（不调用操作系统凭据库）。

    环境变量 `CENTAUR_SECRET_STORE_KEY` 可覆盖应用自动生成的密钥，供更严格的
    部署场景使用；普通本地 Web 使用无需配置它。
    """
    try:
        return EncryptedSQLiteSecretStore()
    except Exception:
        return UnavailableSecretStore()


def _ref_age_seconds(store: SecretStore, ref: str) -> float | None:
    if isinstance(store, (EncryptedFileSecretStore, EncryptedSQLiteSecretStore)):
        return store.ref_age_seconds(ref)
    return None


def cleanup_orphan_secrets(
    store: SecretStore,
    valid_refs: set[str],
    retention_seconds: float = _REF_RETENTION_SECONDS,
) -> list[str]:
    """删除「无引用且超过保留期」的本应用 secret_ref。

    只能删除带 MindOS 前缀的引用；无法测量年龄的实现（keyring/内存）视为已超期，
    因为调用发生在启动恢复路径（进程崩溃后不存在进行中的旧快照请求）。
    """
    removed: list[str] = []
    for ref in store.list_secret_refs():
        if ref in valid_refs:
            continue
        age = _ref_age_seconds(store, ref)
        if age is not None and age < retention_seconds:
            continue
        try:
            store.delete_secret(ref)
            removed.append(ref)
        except Exception:
            continue
    return removed
