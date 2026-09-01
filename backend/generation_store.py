"""P0-2 原子替换：generation 注册表（SQLite）。

记录每个 (集合, source_path) 当前有效的 generation，把「先删旧索引再写新索引」
改为「写新代 → 校验 → 切注册表 → 删旧代」：任一步失败旧索引仍在、仍可检索
（MindOS索引可靠性问题分析与改进方案.md §5 P0-2 / §7.1）。

语义约定：
- generation 0 = 尚未启用原子替换的存量数据（Chroma 记录无 generation 字段）。
  读取口径与改造前完全一致（仅按 source_path 过滤），存量数据零迁移兼容。
- generation >= 1 = 原子替换写入的代数，每次成功切换 +1；写入 id 携带代数
  （{source_path}::g{gen}::{i}），同源新旧代记录在集合内短暂共存。
- 文本集合与图片集合各自独立计数（视频流程分两次写），collection 用逻辑名
  （text/image）而非 Chroma collection 名，与配置解耦。

失败策略（对齐 P0-3 三态契约精神）：
- 读接口失败：返回 0（= 旧式全量口径），读取路径降级为不过滤，行为不劣于改造前；
- set_generation 失败：向上抛异常，调用方（add_*）整体判失败、保留旧索引；
  已写入的新代记录成为残留，由下次写入前的 purge 清理，不影响正确性。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

from runtime_paths import GENERATION_REGISTRY_DB_PATH

logger = logging.getLogger(__name__)

# 集合逻辑名（与 Chroma collection 名解耦）
COLLECTION_TEXT = "text"
COLLECTION_IMAGE = "image"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS generation_registry (
    collection TEXT NOT NULL,
    source_path TEXT NOT NULL,
    generation INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (collection, source_path)
);
"""

_INITIALIZED = False
_LOCK = threading.Lock()
_DB_PATH = GENERATION_REGISTRY_DB_PATH
_DEFAULT_DB_PATH = GENERATION_REGISTRY_DB_PATH


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


def current_generation(collection: str, source_path: str) -> int:
    """读取某 (集合, 源文件) 的当前有效代；无记录或读取失败返回 0（旧式口径）。"""
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT generation FROM generation_registry WHERE collection=? AND source_path=?",
                (collection, source_path),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception as e:
        logger.warning("generation 注册表读取失败（降级为旧式全量口径）%s/%s: %s",
                       collection, source_path, type(e).__name__)
        return 0


def next_generation(collection: str, source_path: str) -> int:
    """下一次写入应使用的新代数（当前 + 1，首次为 1）。只读，不落库。"""
    return current_generation(collection, source_path) + 1


def set_generation(collection: str, source_path: str, generation: int) -> None:
    """切换当前有效代（新代写入并校验通过后调用）。失败向上抛，调用方判写入失败。"""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO generation_registry(collection, source_path, generation, updated_at) "
            "VALUES(?,?,?,?)",
            (collection, source_path, int(generation), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def clear_generation(collection: str, source_path: str) -> None:
    """删除文件/清空某集合记录时，同步清掉注册行（下次读取回到旧式口径）。"""
    try:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM generation_registry WHERE collection=? AND source_path=?",
                (collection, source_path),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("generation 注册表清理失败 %s/%s: %s",
                       collection, source_path, type(e).__name__)


def clear_source(source_path: str) -> None:
    """源文件被彻底删除时，清掉其全部集合的注册行。"""
    for collection in (COLLECTION_TEXT, COLLECTION_IMAGE):
        clear_generation(collection, source_path)


def clear_all() -> None:
    """集合整体重建（recreate_collection）时清空整个注册表。"""
    try:
        conn = _connect()
        try:
            conn.execute("DELETE FROM generation_registry")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("generation 注册表全清失败: %s", type(e).__name__)


def rename_collection(old_token: str, new_token: str) -> None:
    """P1-2：重建提交后把隔离命名空间的代数并回正式 token（重建值为新）。

    INSERT OR REPLACE 语义：重建命名空间的值覆盖旧 token 同名源（新索引为准），
    再清除隔离命名空间的残留行。失败不阻断（注册表为空＝回到旧式全量口径）。
    """
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO generation_registry(collection, source_path, generation, updated_at) "
                "SELECT ?, source_path, generation, updated_at FROM generation_registry WHERE collection=?",
                (new_token, old_token),
            )
            conn.execute("DELETE FROM generation_registry WHERE collection=?", (old_token,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("generation 注册表重命名失败 %s->%s: %s",
                       old_token, new_token, type(e).__name__)


def clear_collection_token(token: str) -> None:
    """P1-2：重建中止后清空某隔离命名空间的代数残留。"""
    try:
        conn = _connect()
        try:
            conn.execute("DELETE FROM generation_registry WHERE collection=?", (token,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("generation 注册表清理 token 失败 %s: %s", token, type(e).__name__)


def current_generations(collection: str) -> dict[str, int]:
    """批量读取某集合全部 {source_path: generation}（list_documents/search 过滤用）。

    读取失败返回空 dict：调用方 get(sp, 0) 回落 0（旧式口径），不劣于改造前。
    """
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT source_path, generation FROM generation_registry WHERE collection=?",
                (collection,),
            ).fetchall()
            return {r[0]: int(r[1]) for r in rows}
        finally:
            conn.close()
    except Exception as e:
        logger.warning("generation 注册表批量读取失败: %s", type(e).__name__)
        return {}


def reset_for_tests(db_path=None) -> None:
    """测试用：切换到独立 DB 并重置全局状态；无参数时恢复默认路径。"""
    global _INITIALIZED, _DB_PATH
    _INITIALIZED = False
    if db_path is None:
        _DB_PATH = _DEFAULT_DB_PATH
    else:
        from pathlib import Path
        _DB_PATH = Path(db_path)
