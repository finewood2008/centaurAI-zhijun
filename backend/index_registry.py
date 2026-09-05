"""索引代际注册表（阶段 B：索引健康闸门的持久化状态；阶段 C1 将扩展为 base/delta 物理代际路由）。

权威与职责：
- `index_generations`：每个物理索引代际一行。阶段 B 下为单个 legacy 代际
  （指向当前 `CHROMA_DATA_DIR`），记录其健康状态（unknown/healthy/corrupted/
  rebuilding 等）与错误码；阶段 C1 将在此基础上记录 base/delta 的物理目录与
  生命周期。
- `index_routing`：单行记录当前活跃 base/delta 与 `routing_epoch`。阶段 B 下
  base 即 legacy 单代，delta 为空；阶段 C1 的切换栅栏将在此更新 epoch 与指针。
- 本 SQLite 是物理路径、代际状态与当前写目标的唯一权威；`active.json` 仅作为
  快速启动缓存（阶段 B 尚未引入，阶段 C1 引入）。

对外只读接口（如 `storage_status`）刻意不返回内部绝对路径，避免泄露宿主机目录。
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
import uuid
import shutil
from pathlib import Path

from runtime_paths import (
    CHROMA_DATA_DIR,
    INDEX_REGISTRY_DB_PATH,
    INDEXES_GENERATIONS_DIR,
)

logger = logging.getLogger(__name__)

# 代际角色与状态（阶段 C1 将引入 building/retired/abandoned 等扩展状态）
ROLE_BASE = "base"
ROLE_DELTA = "delta"

STATUS_UNKNOWN = "unknown"
STATUS_HEALTHY = "healthy"
STATUS_CORRUPTED = "corrupted"
STATUS_REBUILDING = "rebuilding"
STATUS_BUILDING = "building"
STATUS_RETIRED = "retired"

# 路由状态
ROUTING_HEALTHY = "healthy"
ROUTING_SWITCHING = "switching"
ROUTING_RECOVERING = "recovering"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_generations (
    generation_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    path TEXT NOT NULL,
    schema_version TEXT,
    status TEXT NOT NULL,
    rebuild_session_id TEXT,
    routing_epoch INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    validated_at REAL,
    activated_at REAL,
    error_code TEXT,
    error_detail TEXT
);
CREATE TABLE IF NOT EXISTS index_routing (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    base_generation_id TEXT,
    delta_generation_id TEXT,
    routing_epoch INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'healthy',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS index_tombstones (
    source_path TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
"""

_INITIALIZED = False
_LOCK = threading.Lock()
_DB_PATH = INDEX_REGISTRY_DB_PATH
_DEFAULT_DB_PATH = INDEX_REGISTRY_DB_PATH
_LEGACY_PATH = CHROMA_DATA_DIR


def legacy_generation_id() -> str:
    """当前 legacy 单代（阶段 B）的稳定 generation_id，由物理目录路径派生。"""
    digest = hashlib.sha1(str(_LEGACY_PATH).encode("utf-8", "replace")).hexdigest()[:10]
    return f"legacy-{digest}"


def _ensure() -> None:
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
            conn.commit()
        finally:
            conn.close()
        _INITIALIZED = True


def _connect() -> sqlite3.Connection:
    _ensure()
    conn = sqlite3.connect(str(_DB_PATH), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _routing_row(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT base_generation_id, delta_generation_id, routing_epoch, status, updated_at "
        "FROM index_routing WHERE id=1"
    ).fetchone()
    if not row:
        return None
    return {
        "base_generation_id": row[0],
        "delta_generation_id": row[1],
        "routing_epoch": int(row[2]),
        "status": row[3],
        "updated_at": row[4],
    }


def _ensure_legacy_generation(conn: sqlite3.Connection) -> str:
    """幂等注册 legacy 单代并保证路由行存在；返回 base generation_id。"""
    gen_id = legacy_generation_id()
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO index_generations"
        "(generation_id, role, path, schema_version, status, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (gen_id, ROLE_BASE, str(_LEGACY_PATH), None, STATUS_UNKNOWN, now),
    )
    row = conn.execute("SELECT 1 FROM index_routing WHERE id=1").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO index_routing(id, base_generation_id, delta_generation_id,"
            " routing_epoch, status, updated_at) VALUES(1,?,NULL,0,?,?)",
            (gen_id, ROUTING_HEALTHY, now),
        )
    return gen_id


def ensure_registry(path: Path | None = None) -> dict:
    """确保注册表与路由就绪（幂等）。首次调用把当前 CHROMA_DATA_DIR 注册为 legacy 单代。

    Args:
        path: 测试注入的 legacy 物理目录（缺省用 CHROMA_DATA_DIR）。
    """
    global _LEGACY_PATH
    if path is not None:
        _LEGACY_PATH = Path(path)
    conn = _connect()
    try:
        base_id = _ensure_legacy_generation(conn)
        conn.commit()
        routing = _routing_row(conn) or {}
    finally:
        conn.close()
    return routing


def get_routing() -> dict | None:
    """读取当前路由（无记录时返回 None；调用方可先 ensure_registry）。"""
    try:
        conn = _connect()
        try:
            return _routing_row(conn)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("index_routing 读取失败: %s", type(e).__name__)
        return None


def new_generation_id(role: str) -> str:
    """为新建代际生成稳定唯一 id（物理目录名）。"""
    return f"{role}-{uuid.uuid4().hex[:12]}"


def create_delta(force_new: bool = True) -> dict:
    """C1：创建首个/下一个可写 delta 代际并更新路由（幂等：已有 healthy delta 则复用）。

    返回 {"ok": True, "delta_generation_id", "delta_path", "routing_epoch"}；
    失败返回 {"ok": False, "error": ...}。base 代际保持只读不变。
    """
    conn = _connect()
    try:
        base_id = _ensure_legacy_generation(conn)
        routing = _routing_row(conn) or {}
        existing_delta = routing.get("delta_generation_id")
        if existing_delta and not force_new:
            row = conn.execute(
                "SELECT path, status FROM index_generations WHERE generation_id=?",
                (existing_delta,),
            ).fetchone()
            if row and row[1] == STATUS_HEALTHY and Path(row[0]).is_dir():
                return {
                    "ok": True,
                    "delta_generation_id": existing_delta,
                    "delta_path": str(row[0]),
                    "routing_epoch": int(routing.get("routing_epoch", 0)),
                }
        gen_id = new_generation_id(ROLE_DELTA)
        delta_dir = INDEXES_GENERATIONS_DIR / gen_id
        try:
            delta_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("无法创建 delta 代际目录 %s: %s", delta_dir, exc)
            return {"ok": False, "error": f"mkdir_failed:{type(exc).__name__}"}
        now = time.time()
        conn.execute(
            "INSERT INTO index_generations"
            "(generation_id, role, path, schema_version, status, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (gen_id, ROLE_DELTA, str(delta_dir), None, STATUS_HEALTHY, now),
        )
        # 更新路由：指向新 delta，并递增 routing_epoch（切换栅栏的围栏基础）。
        conn.execute(
            "UPDATE index_routing SET delta_generation_id=?, routing_epoch=routing_epoch+1,"
            " status=?, updated_at=? WHERE id=1",
            (gen_id, ROUTING_HEALTHY, now),
        )
        conn.commit()
        routing = _routing_row(conn) or {}
        return {
            "ok": True,
            "delta_generation_id": gen_id,
            "delta_path": str(delta_dir),
            "routing_epoch": int(routing.get("routing_epoch", 0)),
        }
    except Exception as e:
        logger.error("创建 delta 代际失败: %s", type(e).__name__)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


def ensure_delta() -> dict:
    """确保存在可写 delta 代际；不存在则创建（启动/首次写前调用）。"""
    routing = get_routing()
    if routing and routing.get("delta_generation_id"):
        row = get_generation(routing["delta_generation_id"])
        if row and row.get("status") == STATUS_HEALTHY and Path(row["path"]).is_dir():
            return {"ok": True, "delta_generation_id": routing["delta_generation_id"],
                    "delta_path": row["path"], "routing_epoch": routing.get("routing_epoch", 0)}
    return create_delta(force_new=True)


def active_generation_paths() -> tuple[Path | None, Path | None]:
    """返回 (base_path, delta_path)；不存在或不可用为 None。"""
    routing = get_routing()
    if not routing:
        return None, None
    base = delta = None
    if routing.get("base_generation_id"):
        g = get_generation(routing["base_generation_id"])
        if g and g.get("path"):
            base = Path(g["path"])
    if routing.get("delta_generation_id"):
        g = get_generation(routing["delta_generation_id"])
        if g and g.get("path"):
            delta = Path(g["path"])
    return base, delta


# ---- 最小模型 tombstone（C1；C2 并入 job_store source_version 权威时迁移） ----

def set_tombstone(source_path: str) -> None:
    """删除材料时记录 tombstone，读取 union 时挡住 base 里的旧 chunk（防复活）。"""
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO index_tombstones(source_path, created_at) VALUES(?,?)",
                (source_path, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("记录 tombstone 失败 %s: %s", source_path, type(e).__name__)


def clear_tombstone(source_path: str) -> None:
    """重新写入该材料时清除 tombstone（覆盖删除语义）。"""
    try:
        conn = _connect()
        try:
            conn.execute("DELETE FROM index_tombstones WHERE source_path=?", (source_path,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("清除 tombstone 失败 %s: %s", source_path, type(e).__name__)


def is_tombstoned(source_path: str) -> bool:
    try:
        conn = _connect()
        try:
            return conn.execute(
                "SELECT 1 FROM index_tombstones WHERE source_path=?", (source_path,)
            ).fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("tombstone 读取失败 %s: %s", source_path, type(e).__name__)
        return False


def list_tombstones() -> set[str]:
    try:
        conn = _connect()
        try:
            rows = conn.execute("SELECT source_path FROM index_tombstones").fetchall()
            return {r[0] for r in rows}
        finally:
            conn.close()
    except Exception as e:
        logger.warning("tombstone 清单读取失败: %s", type(e).__name__)
        return set()


def get_generation(generation_id: str) -> dict | None:
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT generation_id, role, path, schema_version, status,"
                " rebuild_session_id, routing_epoch, created_at, validated_at,"
                " activated_at, error_code, error_detail "
                "FROM index_generations WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            if not row:
                return None
            keys = ("generation_id", "role", "path", "schema_version", "status",
                    "rebuild_session_id", "routing_epoch", "created_at", "validated_at",
                    "activated_at", "error_code", "error_detail")
            return dict(zip(keys, row))
        finally:
            conn.close()
    except Exception as e:
        logger.warning("index_generations 读取失败 %s: %s", generation_id, type(e).__name__)
        return None


def set_generation_status(
    generation_id: str,
    status: str,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    """更新某代际的健康状态；healthy 记 validated_at/activated_at，corrupted 记错误码。"""
    now = time.time()
    try:
        conn = _connect()
        try:
            if status == STATUS_HEALTHY:
                conn.execute(
                    "UPDATE index_generations SET status=?, validated_at=?, activated_at=?,"
                    " error_code=NULL, error_detail=NULL WHERE generation_id=?",
                    (status, now, now, generation_id),
                )
            else:
                conn.execute(
                    "UPDATE index_generations SET status=?, error_code=?, error_detail=?,"
                    " validated_at=COALESCE(validated_at,?) WHERE generation_id=?",
                    (status, error_code, error_detail, now, generation_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("index_generations 状态更新失败 %s: %s", generation_id, type(e).__name__)


def mark_active_corrupted(error_code: str | None = None, error_detail: str | None = None,
                          role: str = ROLE_BASE) -> None:
    """标记活跃 base 或 delta；delta 损坏不得连带禁用健康 base。"""
    routing = get_routing()
    generation_id = (routing or {}).get("base_generation_id" if role == ROLE_BASE else "delta_generation_id")
    if not generation_id:
        return
    set_generation_status(
        generation_id, STATUS_CORRUPTED,
        error_code=error_code, error_detail=error_detail,
    )


def create_building_base(rebuild_session_id: str | None = None) -> dict:
    """创建目录级 building base，未切换前绝不影响活跃路由。"""
    conn = _connect()
    try:
        _ensure_legacy_generation(conn)
        gen_id = new_generation_id(ROLE_BASE)
        path = INDEXES_GENERATIONS_DIR / gen_id
        path.mkdir(parents=True, exist_ok=False)
        now = time.time()
        conn.execute(
            "INSERT INTO index_generations(generation_id,role,path,status,rebuild_session_id,created_at) VALUES(?,?,?,?,?,?)",
            (gen_id, ROLE_BASE, str(path), STATUS_BUILDING, rebuild_session_id, now),
        )
        conn.commit()
        return {"ok": True, "generation_id": gen_id, "path": str(path)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def activate_base(base_generation_id: str, delta_generation_id: str) -> bool:
    """同一事务切换 base/delta 路由，并把旧活跃代际退役。"""
    conn = _connect()
    try:
        _ensure_legacy_generation(conn)
        # _ensure_legacy_generation 的幂等 INSERT 已开启隐式事务；先提交，再开原子切换事务，
        # 否则 BEGIN IMMEDIATE 会抛 "cannot start a transaction within a transaction"。
        try:
            conn.execute("COMMIT")
        except sqlite3.OperationalError:
            pass
        old = _routing_row(conn) or {}
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE index_generations SET status=?,validated_at=?,activated_at=? WHERE generation_id=?",
                     (STATUS_HEALTHY, now, now, base_generation_id))
        for generation_id in (old.get("base_generation_id"), old.get("delta_generation_id")):
            if generation_id and generation_id not in (base_generation_id, delta_generation_id):
                conn.execute("UPDATE index_generations SET status=? WHERE generation_id=?", (STATUS_RETIRED, generation_id))
        conn.execute("UPDATE index_routing SET base_generation_id=?,delta_generation_id=?,routing_epoch=routing_epoch+1,status=?,updated_at=? WHERE id=1",
                     (base_generation_id, delta_generation_id, ROUTING_HEALTHY, now))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def building_base() -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT generation_id, path, rebuild_session_id FROM index_generations "
            "WHERE role=? AND status=? ORDER BY created_at DESC LIMIT 1",
            (ROLE_BASE, STATUS_BUILDING),
        ).fetchone()
        return {"generation_id": row[0], "path": row[1], "rebuild_session_id": row[2]} if row else None
    finally:
        conn.close()


def set_routing_status(status: str) -> None:
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO index_routing(id, status, updated_at)"
                " VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
                (status, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("index_routing 状态更新失败: %s", type(e).__name__)


def storage_status() -> dict:
    """管理接口安全视图：不返回内部绝对路径、密钥或 HNSW 异常正文。"""
    routing = get_routing()
    base_id = (routing or {}).get("base_generation_id")
    delta_id = (routing or {}).get("delta_generation_id")
    base = get_generation(base_id) if base_id else None
    delta = get_generation(delta_id) if delta_id else None
    generations = list_generations()
    return {
        "index_generations": True,
        "base_generation_id": base_id,
        "delta_generation_id": delta_id,
        "routing_epoch": (routing or {}).get("routing_epoch", 0),
        "routing_status": (routing or {}).get("status", ROUTING_HEALTHY),
        "base_status": (base or {}).get("status", STATUS_UNKNOWN),
        "delta_status": (delta or {}).get("status") if delta else None,
        "base_error_code": (base or {}).get("error_code"),
        "delta_error_code": (delta or {}).get("error_code") if delta else None,
        "generations": generations,
    }


def _directory_bytes(path: Path) -> int:
    try:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0


def list_generations() -> list[dict]:
    """代际容量视图，不暴露绝对路径。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT generation_id, role, status, created_at, activated_at, path FROM index_generations ORDER BY created_at DESC"
        ).fetchall()
        return [{"generation_id": row[0], "role": row[1], "status": row[2],
                 "created_at": row[3], "activated_at": row[4], "bytes": _directory_bytes(Path(row[5]))}
                for row in rows]
    finally:
        conn.close()


def cleanup_retired(keep: int = 1) -> dict:
    """删除超过保留数的 retired 物理目录及注册行，活跃/building 永不触碰。"""
    keep = max(1, int(keep))
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT generation_id, path FROM index_generations WHERE status=? ORDER BY created_at DESC", (STATUS_RETIRED,)
        ).fetchall()
    finally:
        conn.close()
    removed: list[str] = []
    for generation_id, raw_path in rows[keep:]:
        path = Path(raw_path)
        try:
            if path.is_dir() and path.parent == INDEXES_GENERATIONS_DIR:
                shutil.rmtree(path)
            conn = _connect()
            try:
                conn.execute("DELETE FROM index_generations WHERE generation_id=? AND status=?", (generation_id, STATUS_RETIRED))
                conn.commit()
            finally:
                conn.close()
            removed.append(generation_id)
        except OSError as exc:
            logger.warning("清理 retired 索引代际失败 %s: %s", generation_id, type(exc).__name__)
    return {"removed": removed, "retained": min(keep, len(rows))}


def reset_for_tests(db_path: Path | None = None, legacy_path: Path | None = None) -> None:
    """测试用：切换到独立 DB 与 legacy 目录并重置全局状态。"""
    global _INITIALIZED, _DB_PATH, _LEGACY_PATH
    _INITIALIZED = False
    if db_path is None:
        _DB_PATH = _DEFAULT_DB_PATH
    else:
        _DB_PATH = Path(db_path)
    if legacy_path is not None:
        _LEGACY_PATH = Path(legacy_path)
    elif db_path is None:
        _LEGACY_PATH = CHROMA_DATA_DIR
