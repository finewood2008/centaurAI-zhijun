"""Phase D observability and controlled legacy material RAG cleanup.

The cleanup path is deliberately administrator-only at the route layer.  It is
never called during startup, never migrates records, and refuses to delete a
collection that contains data not owned by a known MindOS material.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from config import CHROMA_COLLECTION, IMAGE_COLLECTION
from runtime_paths import (
    DB_ROOT,
    STAGE_D_BACKUPS_DIR,
    STAGE_D_MAINTENANCE_DB_PATH,
    WIKI_DIR,
)

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_PLAN_TTL_SECONDS = 24 * 60 * 60
_LEGACY_COLLECTIONS = (CHROMA_COLLECTION, IMAGE_COLLECTION)
_LEGACY_READ_AUDITED = False


def legacy_read_enabled() -> bool:
    global _LEGACY_READ_AUDITED
    value = os.environ.get("MATERIAL_RAG_LEGACY_READ_ENABLED", "false").strip().lower()
    enabled = value in {"1", "true", "yes", "on"}
    if enabled and not _LEGACY_READ_AUDITED:
        logger.warning("AUDIT legacy material RAG read compatibility is enabled")
        _LEGACY_READ_AUDITED = True
    return enabled


def _connect() -> sqlite3.Connection:
    STAGE_D_MAINTENANCE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STAGE_D_MAINTENANCE_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""CREATE TABLE IF NOT EXISTS legacy_rag_cleanup_plans (
        token TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        preflight_json TEXT NOT NULL,
        backup_id TEXT,
        error_code TEXT,
        error_detail TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""")
    return conn


def _public_plan(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    preflight = json.loads(data.pop("preflight_json") or "{}")
    return {
        "cleanupToken": data["token"],
        "state": data["state"],
        "preflight": preflight,
        "backupId": data.get("backup_id"),
        "errorCode": data.get("error_code"),
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
    }


def _generation_clients() -> list[tuple[str, object]]:
    import chromadb
    import index_registry
    from chromadb.config import Settings

    routing = index_registry.ensure_registry()
    result: list[tuple[str, object]] = []
    seen: set[str] = set()
    for role, generation_id in (("base", (routing or {}).get("base_generation_id")),
                                ("delta", (routing or {}).get("delta_generation_id"))):
        if not generation_id:
            continue
        generation = index_registry.get_generation(generation_id)
        path = Path(str((generation or {}).get("path") or ""))
        if not path.is_dir() or str(path) in seen:
            continue
        seen.add(str(path))
        result.append((role, chromadb.PersistentClient(
            path=str(path), settings=Settings(anonymized_telemetry=False),
        )))
    return result


def _material_source_paths() -> set[str]:
    from .services import ingestion

    return {
        str(record.get("source_path") or "")
        for record in ingestion.JobStore.instance().list()
        if str(record.get("source_path") or "")
    }


def preflight_legacy_cleanup() -> dict:
    """Inventory legacy collections and reject mixed/unknown data fail-closed."""
    known_sources = _material_source_paths()
    collections: list[dict] = []
    unsafe: list[str] = []
    fingerprint_rows: list[tuple[str, str, int, str]] = []
    for role, client in _generation_clients():
        names = {
            item if isinstance(item, str) else str(getattr(item, "name", item))
            for item in client.list_collections()
        }
        for name in _LEGACY_COLLECTIONS:
            if name not in names:
                collections.append({"role": role, "collection": name, "exists": False, "chunks": 0})
                continue
            col = client.get_collection(name=name)
            records = col.get(include=["metadatas"])
            metas = records.get("metadatas") or []
            unknown = 0
            for meta in metas:
                source = str((meta or {}).get("source_path") or (meta or {}).get("file_path") or "")
                if not source or source not in known_sources:
                    unknown += 1
            if unknown:
                unsafe.append(f"{role}:{name}:{unknown}")
            chunks = len(records.get("ids") or [])
            collections.append({
                "role": role, "collection": name, "exists": True,
                "chunks": chunks, "unknownChunks": unknown,
            })
            fingerprint_rows.append((role, name, chunks, str(unknown)))
    fingerprint = hashlib.sha256(json.dumps(fingerprint_rows, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "legacyReadEnabled": legacy_read_enabled(),
        "collections": collections,
        "safeToCleanup": not unsafe,
        "blockers": unsafe,
        "fingerprint": fingerprint,
        "rollbackWindow": "backup retained until an administrator removes it",
    }


def create_legacy_cleanup_plan() -> dict:
    preflight = preflight_legacy_cleanup()
    if preflight["legacyReadEnabled"]:
        raise ValueError("legacy_read_enabled")
    if not preflight["safeToCleanup"]:
        raise ValueError("legacy_collection_contains_unknown_data")
    now = time.time()
    token = f"lrc_{secrets.token_urlsafe(24)}"
    with _LOCK, closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO legacy_rag_cleanup_plans VALUES(?,?,?,?,?,?,?,?)", (
            token, "prepared", json.dumps(preflight, ensure_ascii=False), None, None, None, now, now,
        ))
    logger.warning("AUDIT legacy material RAG cleanup plan prepared token=%s", token[:12])
    return _public_plan({"token": token, "state": "prepared", "preflight_json": json.dumps(preflight),
                         "backup_id": None, "error_code": None, "created_at": now, "updated_at": now})


def _backup_databases(destination: Path) -> None:
    """Create WAL-safe SQLite snapshots with the online backup API."""
    destination.mkdir(parents=True, exist_ok=True)
    for source in sorted(DB_ROOT.glob("*.db")) + sorted(DB_ROOT.glob("*.sqlite3")):
        if source.is_file():
            target = destination / source.name
            with closing(sqlite3.connect(str(source), timeout=30)) as source_conn:
                source_conn.execute("PRAGMA busy_timeout=30000")
                with closing(sqlite3.connect(str(target), timeout=30)) as target_conn:
                    source_conn.backup(target_conn)
                    result = target_conn.execute("PRAGMA quick_check").fetchone()
                    if result is None or str(result[0]).lower() != "ok":
                        raise RuntimeError(f"backup_quick_check_failed:{source.name}")


def _backup_wiki(destination: Path) -> None:
    if WIKI_DIR.is_dir():
        shutil.copytree(WIKI_DIR, destination)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_backup_manifest(destination: Path, *, backup_id: str) -> None:
    import index_registry

    routing = index_registry.get_routing() or index_registry.ensure_registry()
    files = []
    for path in sorted(destination.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files.append({
                "path": path.relative_to(destination).as_posix(),
                "size": path.stat().st_size,
                "sha256": _file_sha256(path),
            })
    manifest = {
        "formatVersion": 1,
        "backupId": backup_id,
        "createdAt": time.time(),
        "routingEpoch": int(routing.get("routing_epoch") or 0),
        "baseGenerationId": routing.get("base_generation_id"),
        "deltaGenerationId": routing.get("delta_generation_id"),
        "files": files,
        "complete": True,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _backup_generations(destination: Path) -> None:
    import index_registry
    routing = index_registry.ensure_registry()
    for role, generation_id in (("base", (routing or {}).get("base_generation_id")),
                                ("delta", (routing or {}).get("delta_generation_id"))):
        generation = index_registry.get_generation(generation_id) if generation_id else None
        source = Path(str((generation or {}).get("path") or ""))
        if source.is_dir():
            shutil.copytree(source, destination / role)


def execute_legacy_cleanup(token: str) -> dict:
    """Backup then delete verified legacy material collections from active generations."""
    with _LOCK, closing(_connect()) as conn, conn:
        row = conn.execute("SELECT * FROM legacy_rag_cleanup_plans WHERE token=?", (token,)).fetchone()
        if row is None:
            raise KeyError("cleanup_plan_not_found")
        if row["state"] != "prepared":
            raise ValueError("cleanup_plan_not_prepared")
        if time.time() - float(row["created_at"]) > _PLAN_TTL_SECONDS:
            conn.execute("UPDATE legacy_rag_cleanup_plans SET state='expired',updated_at=? WHERE token=?", (time.time(), token))
            raise ValueError("cleanup_plan_expired")
        expected = json.loads(row["preflight_json"])

    backup_id = f"legacy-rag-{int(time.time())}-{token[-8:]}"
    destination = STAGE_D_BACKUPS_DIR / backup_id
    try:
        from vector_store import index_maintenance, release_chroma
        with index_maintenance():
            # The second preflight runs after ordinary vector operations have
            # drained, so the fingerprint matches the backup and deletion set.
            current = preflight_legacy_cleanup()
            if (current["legacyReadEnabled"] or not current["safeToCleanup"]
                    or current["fingerprint"] != expected.get("fingerprint")):
                raise ValueError("cleanup_preflight_changed")
            with _LOCK, closing(_connect()) as conn, conn:
                conn.execute("UPDATE legacy_rag_cleanup_plans SET state='backing_up',updated_at=? WHERE token=?", (time.time(), token))
            if not release_chroma():
                raise RuntimeError("active_vector_operations")
            _backup_databases(destination / "databases")
            _backup_wiki(destination / "wiki")
            _backup_generations(destination / "generations")
            _write_backup_manifest(destination, backup_id=backup_id)
            deleted: list[dict] = []
            for role, client in _generation_clients():
                names = {
                    item if isinstance(item, str) else str(getattr(item, "name", item))
                    for item in client.list_collections()
                }
                for name in _LEGACY_COLLECTIONS:
                    if name in names:
                        client.delete_collection(name)
                        deleted.append({"role": role, "collection": name})
        with _LOCK, closing(_connect()) as conn, conn:
            conn.execute("UPDATE legacy_rag_cleanup_plans SET state='completed',backup_id=?,updated_at=? WHERE token=?",
                         (backup_id, time.time(), token))
        logger.warning("AUDIT legacy material RAG cleanup completed token=%s backup=%s deleted=%s",
                       token[:12], backup_id, deleted)
        return {"cleanupToken": token, "state": "completed", "backupId": backup_id, "deleted": deleted}
    except Exception as exc:
        with _LOCK, closing(_connect()) as conn, conn:
            conn.execute("UPDATE legacy_rag_cleanup_plans SET state='failed',backup_id=?,error_code=?,error_detail=?,updated_at=? WHERE token=?",
                         (backup_id, type(exc).__name__, str(exc)[:500], time.time(), token))
        logger.exception("legacy material RAG cleanup failed token=%s", token[:12])
        raise


def monitoring_status() -> dict:
    """Phase D operational dashboard payload.  No source paths, contents or secrets."""
    from .ollama_material_scheduler import scheduler_status
    from .stores import card_ledger_store
    from .stores.material_pipeline_store import MaterialPipelineStore
    from vector_store import index_health_state

    vector_counts: dict[str, int] = {}
    for job in card_ledger_store.list_vector_jobs(limit=500):
        state = str(job.get("state") or "unknown")
        vector_counts[state] = vector_counts.get(state, 0) + 1
    with closing(_connect()) as conn:
        history = conn.execute("SELECT state,COUNT(*) AS count FROM legacy_rag_cleanup_plans GROUP BY state").fetchall()
    return {
        "materialQueue": MaterialPipelineStore.instance().queue_summary(),
        "ollamaScheduler": scheduler_status(),
        "cardIndexQueue": {"states": vector_counts, "total": sum(vector_counts.values())},
        "indexHealth": index_health_state(),
        "legacyMaterialRag": {
            "legacyReadEnabled": legacy_read_enabled(),
            "cleanupPlans": {str(row["state"]): int(row["count"]) for row in history},
        },
    }
