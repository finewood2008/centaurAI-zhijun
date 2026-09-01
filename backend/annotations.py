"""文件中心元数据存储。

人工标注、分组、操作审计、回收站记录和单文件 RAG 策略统一保存在 SQLite，
与 Chroma 向量集合解耦。首次启动会无损导入旧 annotations.json / groups.json；
旧文件保留作回退，不再作为运行时数据源。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from runtime_paths import FILE_CENTER_DB_PATH, LEGACY_ANNOTATIONS_PATH, LEGACY_GROUPS_PATH

logger = logging.getLogger(__name__)

_DB_PATH = FILE_CENTER_DB_PATH
_LEGACY_ANNOTATIONS_PATH = LEGACY_ANNOTATIONS_PATH
_LEGACY_GROUPS_PATH = LEGACY_GROUPS_PATH
_LOCK = threading.RLock()
_INITIALIZED_PATH: str | None = None

_MAX_TAGS = 32
_MAX_TAG_LEN = 64
_MAX_TEXT_LEN = 4000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _empty() -> dict:
    return {"tags": [], "importance": 0, "pinned": False, "note": "", "caption": "", "group": ""}


def _normalize_tags(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tag in raw:
        value = str(tag).strip()[:_MAX_TAG_LEN]
        if value and value not in seen:
            seen.add(value)
            out.append(value)
        if len(out) >= _MAX_TAGS:
            break
    return out


def _normalize(ann: dict | None) -> dict:
    ann = ann if isinstance(ann, dict) else {}
    out = _empty()
    out["tags"] = _normalize_tags(ann.get("tags"))
    try:
        importance = int(ann.get("importance", 0) or 0)
    except (TypeError, ValueError):
        importance = 0
    out["importance"] = max(0, min(5, importance))
    out["pinned"] = bool(ann.get("pinned", False))
    out["note"] = str(ann.get("note", "") or "")[:_MAX_TEXT_LEN]
    out["caption"] = str(ann.get("caption", "") or "").strip()[:_MAX_TEXT_LEN]
    out["group"] = str(ann.get("group", "") or "").strip()[:_MAX_TAG_LEN]
    return out


def _is_default(ann: dict) -> bool:
    return not any((ann["tags"], ann["importance"], ann["pinned"], ann["note"], ann["caption"], ann["group"]))


def _raw_connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _read_legacy(path: Path, fallback):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("旧元数据读取失败 %s: %s", path, exc)
    return fallback


def _ensure_db() -> None:
    global _INITIALIZED_PATH
    db_key = str(_DB_PATH.resolve())
    if _INITIALIZED_PATH == db_key and _DB_PATH.exists():
        return
    with _LOCK:
        if _INITIALIZED_PATH == db_key and _DB_PATH.exists():
            return
        conn = _raw_connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS annotations (
                    source_path TEXT PRIMARY KEY,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    importance INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    caption TEXT NOT NULL DEFAULT '',
                    group_name TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_annotations_group ON annotations(group_name);
                CREATE TABLE IF NOT EXISTS groups_registry (
                    name TEXT PRIMARY KEY, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    targets_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    undo_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    undone_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(id DESC);
                CREATE TABLE IF NOT EXISTS trash (
                    id TEXT PRIMARY KEY,
                    original_path TEXT NOT NULL,
                    trash_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    annotation_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    deleted_at TEXT NOT NULL,
                    restored_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trash_status ON trash(status, deleted_at DESC);
                CREATE TABLE IF NOT EXISTS rag_overrides (
                    source_path TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            migrated = conn.execute("SELECT value FROM schema_meta WHERE key='legacy_json_migrated'").fetchone()
            if not migrated:
                legacy_annotations = _read_legacy(_LEGACY_ANNOTATIONS_PATH, {})
                legacy_groups = _read_legacy(_LEGACY_GROUPS_PATH, [])
                with conn:
                    if isinstance(legacy_annotations, dict):
                        for source_path, raw in legacy_annotations.items():
                            if isinstance(raw, dict):
                                _write_annotation(conn, str(source_path), _normalize(raw))
                    if isinstance(legacy_groups, list):
                        for name in legacy_groups:
                            clean = str(name).strip()[:_MAX_TAG_LEN]
                            if clean:
                                conn.execute(
                                    "INSERT OR IGNORE INTO groups_registry(name, created_at) VALUES (?, ?)",
                                    (clean, _now()),
                                )
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('legacy_json_migrated', ?)",
                        (_now(),),
                    )
                logger.info("文件中心元数据已迁移到 SQLite: %s", _DB_PATH)
        finally:
            conn.close()
        try:
            os.chmod(_DB_PATH, 0o600)
        except OSError:
            pass
        _INITIALIZED_PATH = db_key


def _connect() -> sqlite3.Connection:
    _ensure_db()
    return _raw_connect()


def _row_annotation(row: sqlite3.Row | None) -> dict:
    if row is None:
        return _empty()
    try:
        tags = json.loads(row["tags_json"] or "[]")
    except Exception:
        tags = []
    return _normalize(
        {
            "tags": tags,
            "importance": row["importance"],
            "pinned": bool(row["pinned"]),
            "note": row["note"],
            "caption": row["caption"],
            "group": row["group_name"],
        }
    )


def _write_annotation(conn: sqlite3.Connection, source_path: str, ann: dict) -> None:
    ann = _normalize(ann)
    if _is_default(ann):
        conn.execute("DELETE FROM annotations WHERE source_path=?", (source_path,))
        return
    conn.execute(
        """INSERT INTO annotations
           (source_path, tags_json, importance, pinned, note, caption, group_name, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_path) DO UPDATE SET
             tags_json=excluded.tags_json, importance=excluded.importance,
             pinned=excluded.pinned, note=excluded.note, caption=excluded.caption,
             group_name=excluded.group_name, updated_at=excluded.updated_at""",
        (
            source_path, _json(ann["tags"]), ann["importance"], int(ann["pinned"]),
            ann["note"], ann["caption"], ann["group"], _now(),
        ),
    )


def _insert_audit(
    conn: sqlite3.Connection,
    action: str,
    targets: Iterable[str] = (),
    payload: dict | None = None,
    undo: dict | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO audit_log(action, targets_json, payload_json, undo_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (action, _json(list(targets)), _json(payload or {}), _json(undo or {}), _now()),
    )
    return int(cursor.lastrowid)


def add_audit(action: str, targets: Iterable[str] = (), payload: dict | None = None, undo: dict | None = None) -> int:
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                return _insert_audit(conn, action, targets, payload, undo)
        finally:
            conn.close()


def get(source_path: str) -> dict:
    conn = _connect()
    try:
        return _row_annotation(conn.execute("SELECT * FROM annotations WHERE source_path=?", (source_path,)).fetchone())
    finally:
        conn.close()


def get_all() -> dict:
    conn = _connect()
    try:
        return {row["source_path"]: _row_annotation(row) for row in conn.execute("SELECT * FROM annotations")}
    finally:
        conn.close()


def get_map_for(source_paths: set[str]) -> dict:
    if not source_paths:
        return {}
    conn = _connect()
    try:
        out: dict[str, dict] = {}
        paths = list(source_paths)
        for start in range(0, len(paths), 800):
            batch = paths[start:start + 800]
            placeholders = ",".join("?" for _ in batch)
            for row in conn.execute(f"SELECT * FROM annotations WHERE source_path IN ({placeholders})", batch):
                out[row["source_path"]] = _row_annotation(row)
        return out
    finally:
        conn.close()


def set_annotation(source_path: str, patch: dict, merge: bool = True) -> tuple[dict, bool]:
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                row = conn.execute("SELECT * FROM annotations WHERE source_path=?", (source_path,)).fetchone()
                old = _row_annotation(row)
                base = dict(old) if merge else _empty()
                for key in ("tags", "importance", "pinned", "note", "caption", "group"):
                    if key in patch and patch[key] is not None:
                        base[key] = patch[key]
                new = _normalize(base)
                _write_annotation(conn, source_path, new)
            return new, new["caption"] != old["caption"]
        finally:
            conn.close()


def batch_set_annotations(
    source_paths: Iterable[str],
    patch: dict,
    tags_mode: str = "replace",
    note_mode: str = "replace",
    dry_run: bool = False,
) -> dict:
    paths = list(dict.fromkeys(str(path) for path in source_paths if path))
    if not paths:
        return {"updated": 0, "items": [], "caption_changed": [], "audit_id": None, "dry_run": dry_run}
    if tags_mode not in {"replace", "add", "remove"}:
        raise ValueError("tags_mode 必须是 replace/add/remove")
    if note_mode not in {"replace", "append", "clear"}:
        raise ValueError("note_mode 必须是 replace/append/clear")
    with _LOCK:
        conn = _connect()
        try:
            old_map: dict[str, dict] = {}
            items: list[dict] = []
            caption_changed: list[str] = []
            with conn:
                for source_path in paths:
                    old = _row_annotation(
                        conn.execute("SELECT * FROM annotations WHERE source_path=?", (source_path,)).fetchone()
                    )
                    old_map[source_path] = old
                    merged = dict(old)
                    if "tags" in patch and patch["tags"] is not None:
                        incoming = _normalize_tags(patch["tags"])
                        if tags_mode == "add":
                            merged["tags"] = _normalize_tags(old["tags"] + incoming)
                        elif tags_mode == "remove":
                            remove = set(incoming)
                            merged["tags"] = [tag for tag in old["tags"] if tag not in remove]
                        else:
                            merged["tags"] = incoming
                    if "note" in patch and patch["note"] is not None:
                        note = str(patch["note"] or "")[:_MAX_TEXT_LEN]
                        if note_mode == "append":
                            merged["note"] = (old["note"] + ("\n" if old["note"] and note else "") + note)[:_MAX_TEXT_LEN]
                        elif note_mode == "clear":
                            merged["note"] = ""
                        else:
                            merged["note"] = note
                    for key in ("importance", "pinned", "caption", "group"):
                        if key in patch and patch[key] is not None:
                            merged[key] = patch[key]
                    new = _normalize(merged)
                    items.append({"source_path": source_path, "annotation": new})
                    if new["caption"] != old["caption"]:
                        caption_changed.append(source_path)
                    if not dry_run:
                        _write_annotation(conn, source_path, new)
                audit_id = None
                if not dry_run:
                    audit_id = _insert_audit(
                        conn, "batch_annotation", paths,
                        {"patch": patch, "tags_mode": tags_mode, "note_mode": note_mode},
                        {"annotations": old_map},
                    )
            return {
                "updated": len(items), "items": items, "caption_changed": caption_changed,
                "audit_id": audit_id, "dry_run": dry_run,
            }
        finally:
            conn.close()


def delete(source_path: str) -> bool:
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                old = _row_annotation(conn.execute("SELECT * FROM annotations WHERE source_path=?", (source_path,)).fetchone())
                conn.execute("DELETE FROM annotations WHERE source_path=?", (source_path,))
            return bool(old["caption"])
        finally:
            conn.close()


def rename(old_path: str, new_path: str) -> bool:
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                old = conn.execute("SELECT * FROM annotations WHERE source_path=?", (old_path,)).fetchone()
                exists = conn.execute("SELECT 1 FROM annotations WHERE source_path=?", (new_path,)).fetchone()
                if old is None or exists:
                    return False
                _write_annotation(conn, new_path, _row_annotation(old))
                conn.execute("DELETE FROM annotations WHERE source_path=?", (old_path,))
                conn.execute("UPDATE rag_overrides SET source_path=? WHERE source_path=?", (new_path, old_path))
                return True
        finally:
            conn.close()


def caption_of(source_path: str) -> str:
    return get(source_path).get("caption", "")


def list_groups() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT name, COALESCE(c.cnt, 0) AS count
               FROM (SELECT name FROM groups_registry UNION SELECT group_name FROM annotations WHERE group_name != '') g
               LEFT JOIN (SELECT group_name, COUNT(*) cnt FROM annotations WHERE group_name != '' GROUP BY group_name) c
                 ON c.group_name=g.name ORDER BY name COLLATE NOCASE"""
        ).fetchall()
        return [{"name": row["name"], "count": int(row["count"])} for row in rows]
    finally:
        conn.close()


def create_group(name: str) -> bool:
    clean = (name or "").strip()[:_MAX_TAG_LEN]
    if not clean:
        return False
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                cur = conn.execute("INSERT OR IGNORE INTO groups_registry(name, created_at) VALUES (?, ?)", (clean, _now()))
                return cur.rowcount > 0
        finally:
            conn.close()


def delete_group(name: str) -> int:
    clean = (name or "").strip()
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                conn.execute("DELETE FROM groups_registry WHERE name=?", (clean,))
                cur = conn.execute("UPDATE annotations SET group_name='', updated_at=? WHERE group_name=?", (_now(), clean))
                conn.execute(
                    "DELETE FROM annotations WHERE tags_json='[]' AND importance=0 AND pinned=0 AND note='' AND caption='' AND group_name=''"
                )
                return max(0, cur.rowcount)
        finally:
            conn.close()


def rename_group(old: str, new: str) -> bool:
    old_name = (old or "").strip()
    new_name = (new or "").strip()[:_MAX_TAG_LEN]
    if not old_name or not new_name or old_name == new_name:
        return False
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                conn.execute("INSERT OR IGNORE INTO groups_registry(name, created_at) VALUES (?, ?)", (new_name, _now()))
                conn.execute("DELETE FROM groups_registry WHERE name=?", (old_name,))
                conn.execute("UPDATE annotations SET group_name=?, updated_at=? WHERE group_name=?", (new_name, _now(), old_name))
                return True
        finally:
            conn.close()


def list_audit(limit: int = 100) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, min(500, limit)),)).fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "id": row["id"], "action": row["action"], "status": row["status"],
                    "targets": json.loads(row["targets_json"]), "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"], "undone_at": row["undone_at"],
                }
            )
        return out
    finally:
        conn.close()


def undo_audit(audit_id: int) -> dict:
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                row = conn.execute("SELECT * FROM audit_log WHERE id=?", (audit_id,)).fetchone()
                if row is None:
                    raise ValueError("操作记录不存在")
                if row["status"] != "active":
                    raise ValueError("该操作已撤销或不可撤销")
                undo = json.loads(row["undo_json"] or "{}")
                if row["action"] != "batch_annotation" or not isinstance(undo.get("annotations"), dict):
                    raise ValueError("该操作不支持自动撤销")
                restored = 0
                caption_changed: list[str] = []
                for source_path, old in undo["annotations"].items():
                    current = _row_annotation(conn.execute("SELECT * FROM annotations WHERE source_path=?", (source_path,)).fetchone())
                    normalized = _normalize(old)
                    if current["caption"] != normalized["caption"]:
                        caption_changed.append(source_path)
                    _write_annotation(conn, source_path, normalized)
                    restored += 1
                conn.execute("UPDATE audit_log SET status='undone', undone_at=? WHERE id=?", (_now(), audit_id))
                _insert_audit(conn, "undo", [], {"audit_id": audit_id, "restored": restored})
                return {"restored": restored, "caption_changed": caption_changed}
        finally:
            conn.close()


def record_trash(record: dict) -> int:
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO trash
                       (id, original_path, trash_path, file_name, size, annotation_json, metadata_json, status, deleted_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                    (
                        record["id"], record["original_path"], record["trash_path"], record["file_name"],
                        int(record.get("size") or 0), _json(record.get("annotation") or {}),
                        _json(record.get("metadata") or {}), record.get("deleted_at") or _now(),
                    ),
                )
                return _insert_audit(conn, "trash", [record["original_path"]], {"trash_id": record["id"]})
        finally:
            conn.close()


def _trash_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"], "original_path": row["original_path"], "trash_path": row["trash_path"],
        "file_name": row["file_name"], "size": int(row["size"]),
        "annotation": _normalize(json.loads(row["annotation_json"] or "{}")),
        "metadata": json.loads(row["metadata_json"] or "{}"), "status": row["status"],
        "deleted_at": row["deleted_at"], "restored_at": row["restored_at"],
    }


def list_trash(include_inactive: bool = False, limit: int = 500) -> list[dict]:
    conn = _connect()
    try:
        where = "" if include_inactive else "WHERE status='active'"
        rows = conn.execute(f"SELECT * FROM trash {where} ORDER BY deleted_at DESC LIMIT ?", (max(1, min(2000, limit)),)).fetchall()
        return [_trash_row(row) for row in rows]
    finally:
        conn.close()


def get_trash(trash_id: str) -> dict | None:
    conn = _connect()
    try:
        return _trash_row(conn.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone())
    finally:
        conn.close()


def mark_trash_restored(trash_id: str, restored_path: str) -> None:
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                conn.execute("UPDATE trash SET status='restored', restored_at=? WHERE id=?", (_now(), trash_id))
                _insert_audit(conn, "restore", [restored_path], {"trash_id": trash_id})
        finally:
            conn.close()


def delete_trash_record(trash_id: str) -> None:
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                conn.execute("DELETE FROM trash WHERE id=?", (trash_id,))
                _insert_audit(conn, "purge_trash", [], {"trash_id": trash_id})
        finally:
            conn.close()


def set_rag_override(source_path: str, strategy_id: str | None) -> None:
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                if strategy_id:
                    conn.execute(
                        """INSERT INTO rag_overrides(source_path, strategy_id, updated_at) VALUES (?, ?, ?)
                           ON CONFLICT(source_path) DO UPDATE SET strategy_id=excluded.strategy_id, updated_at=excluded.updated_at""",
                        (source_path, strategy_id, _now()),
                    )
                else:
                    conn.execute("DELETE FROM rag_overrides WHERE source_path=?", (source_path,))
        finally:
            conn.close()


def get_rag_override(source_path: str) -> str | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT strategy_id FROM rag_overrides WHERE source_path=?", (source_path,)).fetchone()
        return row["strategy_id"] if row else None
    finally:
        conn.close()
