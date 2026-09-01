"""全量重建会话的持久化进度（P1-2 中断恢复）。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from runtime_paths import DB_ROOT

_DB_PATH = DB_ROOT / "rebuild_progress.db"
_LOCK = threading.Lock()
_READY = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rebuild_sessions (
    session_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    states_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    global _READY
    with _LOCK:
        if not _READY:
            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(_DB_PATH), timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
            _READY = True
    conn = sqlite3.connect(str(_DB_PATH), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def start(session_id: str, mode: str, manifest: dict[str, str]) -> None:
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO rebuild_sessions "
            "(session_id,mode,status,manifest_json,states_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "mode=excluded.mode, status='running', manifest_json=excluded.manifest_json, "
            "updated_at=excluded.updated_at",
            (session_id, mode, "running", json.dumps(manifest, ensure_ascii=False), "{}", now, now),
        )
        conn.commit()
    finally:
        conn.close()


def update_states(session_id: str, states: dict[str, str]) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE rebuild_sessions SET states_json=?, updated_at=? WHERE session_id=? AND status='running'",
            (json.dumps(states, ensure_ascii=False), time.time(), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_path_state(session_id: str, source_path: str, state: str) -> None:
    """合并单个材料终态；索引线程每次 done/failed 都调用，保证可恢复进度。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT states_json FROM rebuild_sessions WHERE session_id=? AND status='running'",
            (session_id,),
        ).fetchone()
        if row is None:
            return
        try:
            states = json.loads(row[0])
        except ValueError:
            states = {}
        if not isinstance(states, dict):
            states = {}
        states[source_path] = state
        conn.execute(
            "UPDATE rebuild_sessions SET states_json=?, updated_at=? WHERE session_id=? AND status='running'",
            (json.dumps(states, ensure_ascii=False), time.time(), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def finish(session_id: str, status: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE rebuild_sessions SET status=?, updated_at=? WHERE session_id=?",
            (status, time.time(), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def active() -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT session_id,mode,manifest_json,states_json,created_at,updated_at "
            "FROM rebuild_sessions WHERE status='running' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        manifest = json.loads(row[2])
        states = json.loads(row[3])
    except ValueError:
        return None
    return {
        "session_id": row[0], "mode": row[1],
        "manifest": manifest if isinstance(manifest, dict) else {},
        "states": states if isinstance(states, dict) else {},
        "created_at": row[4], "updated_at": row[5],
    }


def reset_for_tests(db_path: Path | None = None) -> None:
    global _DB_PATH, _READY
    _DB_PATH = db_path or (DB_ROOT / "rebuild_progress.db")
    _READY = False
