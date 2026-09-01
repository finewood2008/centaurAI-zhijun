"""MindOS 派生内容 SQLite 持久化存储（P14）。

与 JobStore 相同的 SQLite sidecar 风格。document_parts 保存 DOCX/PDF 的可展示、
可定位的结构化部分（段落 / 表格 / 页面 / 内嵌图片），以 material_id + input_hash
幂等替换保存：内容哈希未变时不重复写库；重试 / 文件更新时先删除旧 part 再写新 part。

内嵌图片（P14-02）：part_type=image，图片字节按内容 hash 去重落盘到受控派生目录
（DERIVED_IMAGES_DIR/material_id/），document_parts 仅保存 artifact_key 与元数据；
同一图片多页/多处引用共享同一 artifact 文件、各来源各存一行（保留多个位置）。
重处理 / 清理时同步删除孤儿图片文件，旧派生图片不会泄漏或继续通过接口访问。

derived_records（摘要 / 关键词 / 实体 / 生成草稿）与 corrections（纠错本）将在后续
P14 阶段并入本库。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from runtime_paths import DERIVED_STORE_DB_PATH, DERIVED_IMAGES_DIR
from .job_store import JobStore

_INITIALIZED = False
_LOCK = threading.Lock()
_DB_PATH = DERIVED_STORE_DB_PATH
_DEFAULT_DB_PATH = DERIVED_STORE_DB_PATH

_MIME_EXTS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/tiff": "tif",
}


def _mime_ext(mime: str) -> str:
    return _MIME_EXTS.get(mime.strip().lower(), "img")


class DerivedStore:
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._ensure()

    @classmethod
    def instance(cls) -> "DerivedStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = DerivedStore()
            return cls._instance

    # ---- SQLite helpers ----

    def _connect(self) -> sqlite3.Connection:
        self._ensure()
        conn = sqlite3.connect(str(_DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
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
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS document_parts (
                        id TEXT PRIMARY KEY,
                        material_id TEXT NOT NULL,
                        part_type TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        text TEXT NOT NULL DEFAULT '',
                        location_json TEXT NOT NULL DEFAULT '{}',
                        artifact_key TEXT,
                        input_hash TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_document_parts_material
                        ON document_parts(material_id, ordinal);
                    CREATE TABLE IF NOT EXISTS derived_records (
                        id TEXT PRIMARY KEY,
                        owner_type TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        content_json TEXT NOT NULL DEFAULT '{}',
                        input_hash TEXT NOT NULL DEFAULT '',
                        generator TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_derived_owner
                        ON derived_records(owner_type, owner_id, kind);
                    CREATE TABLE IF NOT EXISTS corrections (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        incorrect_claim TEXT NOT NULL,
                        corrected_claim TEXT NOT NULL,
                        keywords_json TEXT NOT NULL DEFAULT '[]',
                        source_ids_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_corrections_status
                        ON corrections(status);
                    """
                )
                # P14-02：图片 part 附加元数据（mime / 尺寸 / OCR 状态）；幂等迁移
                try:
                    conn.execute(
                        "ALTER TABLE document_parts ADD COLUMN image_meta_json TEXT NOT NULL DEFAULT '{}'"
                    )
                except sqlite3.OperationalError:
                    pass  # 列已存在
            finally:
                conn.close()
            _INITIALIZED = True

    @staticmethod
    def _row_to_part(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "material_id": row["material_id"],
            "part_type": row["part_type"],
            "ordinal": row["ordinal"],
            "text": row["text"] or "",
            "location": json.loads(row["location_json"] or "{}"),
            "location_json": row["location_json"] or "{}",
            "artifact_key": row["artifact_key"],
            "image_meta": json.loads(row["image_meta_json"] or "{}"),
            "input_hash": row["input_hash"] or "",
        }

    # ---- 图片落盘 ----

    def _persist_image_file(self, material_id: str, image_meta: dict) -> str:
        """按内容 hash 去重落盘图片字节，返回内部 artifact_key（不含物理路径）。"""
        blob = image_meta.get("blob")
        if not blob:
            return ""
        digest = hashlib.sha256(blob).hexdigest()[:16]
        ext = _mime_ext(str(image_meta.get("mime") or ""))
        artifact_key = f"{digest}.{ext}"
        target_dir = DERIVED_IMAGES_DIR / material_id
        target = target_dir / artifact_key
        if not target.is_file():
            target_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        return artifact_key

    def _cleanup_orphan_images(self, material_id: str, keep_keys: set[str]) -> None:
        """删除该资料派生目录中不再被任何 part 引用的图片文件。"""
        target_dir = DERIVED_IMAGES_DIR / material_id
        if not target_dir.is_dir():
            return
        for entry in target_dir.iterdir():
            if entry.name not in keep_keys:
                try:
                    entry.unlink()
                except OSError:
                    pass
        try:
            target_dir.rmdir()
        except OSError:
            pass

    def image_file_path(self, material_id: str, artifact_key: str):
        """把 artifact_key 解析为受控派生目录内的落盘路径；越界（含路径穿越）返回 None。"""
        if not artifact_key or "/" in artifact_key or "\\" in artifact_key:
            return None
        root = DERIVED_IMAGES_DIR.resolve()
        expected_parent = (root / material_id).resolve()
        target = (root / material_id / artifact_key).resolve()
        if target.parent != expected_parent:
            return None
        return target

    # ---- Public API ----

    def upsert_document_parts(
        self, material_id: str, input_hash: str, parts: list[dict]
    ) -> list[dict]:
        """按 material_id + input_hash 幂等替换保存结构化 part。

        返回带 id 的 part 记录列表：
        - 内容哈希未变：返回既有记录，不重复写库；
        - 内容已变（含首次）：先删除该资料旧 part，再写入新 part；
        - 图片 part：字节先按内容 hash 落盘，再记录 artifact_key 与元数据；
          完成后清理不再被引用的孤儿图片文件。
        """
        if not parts:
            self.delete_for_material(material_id)
            return []
        conn = self._connect()
        new_keys: set[str] = set()
        try:
            with conn:
                existing_hash = conn.execute(
                    "SELECT input_hash FROM document_parts WHERE material_id=? LIMIT 1",
                    (material_id,),
                ).fetchone()
                if existing_hash is not None and existing_hash["input_hash"] == input_hash:
                    return self.parts_for_material(material_id)
                for part in parts:
                    if part.get("part_type") == "image" and part.get("image", {}).get("blob"):
                        artifact_key = self._persist_image_file(material_id, part["image"])
                        part["artifact_key"] = artifact_key
                        if artifact_key:
                            new_keys.add(artifact_key)
                conn.execute(
                    "DELETE FROM document_parts WHERE material_id=?", (material_id,)
                )
                now = time.time()
                for part in parts:
                    part_id = f"{material_id}::{part.get('part_type', 'part')}::{part.get('ordinal', 0)}"
                    location_json = json.dumps(
                        part.get("location") or {}, ensure_ascii=False
                    )
                    image_meta = {}
                    if part.get("part_type") == "image":
                        img = part.get("image") or {}
                        image_meta = {
                            "mime": img.get("mime") or "",
                            "width": img.get("width"),
                            "height": img.get("height"),
                            "ocr_status": img.get("ocr_status", "empty"),
                        }
                    conn.execute(
                        """INSERT INTO document_parts
                           (id, material_id, part_type, ordinal, text, location_json,
                            artifact_key, image_meta_json, input_hash, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            part_id,
                            material_id,
                            part.get("part_type", "part"),
                            int(part.get("ordinal", 0)),
                            part.get("text") or "",
                            location_json,
                            part.get("artifact_key"),
                            json.dumps(image_meta, ensure_ascii=False),
                            input_hash,
                            now,
                        ),
                    )
        finally:
            conn.close()
        self._cleanup_orphan_images(material_id, new_keys)
        return self.parts_for_material(material_id)

    def parts_for_material(self, material_id: str) -> list[dict]:
        """返回某资料的全部结构化 part，按文档顺序（ordinal）排列。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM document_parts WHERE material_id=? ORDER BY ordinal",
                (material_id,),
            ).fetchall()
            return [self._row_to_part(r) for r in rows]
        finally:
            conn.close()

    def get_part(self, material_id: str, part_id: str) -> dict | None:
        """返回单条 part；不存在返回 None。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM document_parts WHERE material_id=? AND id=?",
                (material_id, part_id),
            ).fetchone()
            return self._row_to_part(row) if row else None
        finally:
            conn.close()

    def table_count_for_material(self, material_id: str) -> int:
        """统计某资料的表格 part 数量。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM document_parts "
                "WHERE material_id=? AND part_type='table'",
                (material_id,),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    def set_image_ocr(
        self, material_id: str, part_id: str, ocr_text: str, ocr_status: str
    ) -> None:
        """回写图片 part 的 OCR 文本与状态（不改变其它 part 字段）。"""
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    "SELECT image_meta_json FROM document_parts WHERE material_id=? AND id=?",
                    (material_id, part_id),
                ).fetchone()
                if row is None:
                    return
                meta = json.loads(row["image_meta_json"] or "{}")
                meta["ocr_status"] = ocr_status
                conn.execute(
                    "UPDATE document_parts SET text=?, image_meta_json=? "
                    "WHERE material_id=? AND id=?",
                    (ocr_text, json.dumps(meta, ensure_ascii=False), material_id, part_id),
                )
        finally:
            conn.close()

    def delete_for_material(self, material_id: str) -> int:
        """删除某资料的全部派生 part 及其图片目录，返回受影响行数（归档 / 清理用）。"""
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM document_parts WHERE material_id=?", (material_id,)
                )
                count = cursor.rowcount
        finally:
            conn.close()
        shutil.rmtree(DERIVED_IMAGES_DIR / material_id, ignore_errors=True)
        return count

    # ---- 派生记录（摘要 / 关键词 / 实体 / 生成草稿）----

    @staticmethod
    def _row_to_derived(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "owner_type": row["owner_type"],
            "owner_id": row["owner_id"],
            "kind": row["kind"],
            "status": row["status"],
            "content": json.loads(row["content_json"] or "{}"),
            "input_hash": row["input_hash"] or "",
            "generator": row["generator"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_derived_record(self, owner_type: str, owner_id: str, kind: str) -> dict | None:
        """返回一条派生记录（如 SUMMARY）；不存在返回 None。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM derived_records WHERE owner_type=? AND owner_id=? AND kind=?",
                (owner_type, owner_id, kind),
            ).fetchone()
            return self._row_to_derived(row) if row else None
        finally:
            conn.close()

    def set_derived_record(
        self,
        owner_type: str,
        owner_id: str,
        kind: str,
        status: str,
        content: dict,
        input_hash: str,
        generator: str,
    ) -> dict:
        """创建/更新一条派生记录（幂等 upsert，id 由 owner+kind 派生）。"""
        record_id = f"{owner_type}:{owner_id}:{kind}"
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                existing = conn.execute(
                    "SELECT created_at FROM derived_records WHERE id=?", (record_id,)
                ).fetchone()
                created_at = existing["created_at"] if existing else now
                conn.execute(
                    """INSERT INTO derived_records
                       (id, owner_type, owner_id, kind, status, content_json,
                        input_hash, generator, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         status=excluded.status,
                         content_json=excluded.content_json,
                         input_hash=excluded.input_hash,
                         generator=excluded.generator,
                         updated_at=excluded.updated_at""",
                    (
                        record_id,
                        owner_type,
                        owner_id,
                        kind,
                        status,
                        json.dumps(content or {}, ensure_ascii=False),
                        input_hash,
                        generator,
                        created_at,
                        now,
                    ),
                )
        finally:
            conn.close()
        record = self.get_derived_record(owner_type, owner_id, kind)
        return record if record is not None else {
            "id": record_id, "owner_type": owner_type, "owner_id": owner_id, "kind": kind,
            "status": status, "content": content or {}, "input_hash": input_hash,
            "generator": generator, "created_at": created_at, "updated_at": now,
        }

    def save_material_draft_cas(
        self,
        material_id: str,
        expected_revision: str,
        content: dict,
        input_hash: str,
        generator: str,
        status: str = "ok",
    ) -> dict:
        """按草稿 revision 原子保存 material-owned GENERATED_DRAFT。

        SQLite 事务内读取并比较 revision，避免两个详情页编辑会话相互覆盖。调用方须
        在 content 中提供新 revision；冲突时抛出 DraftRevisionConflict 并携带当前记录。
        """
        kind = "GENERATED_DRAFT"
        record_id = f"material:{material_id}:{kind}"
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM derived_records WHERE id=?", (record_id,)).fetchone()
            current = self._row_to_derived(row) if row else None
            current_revision = str((current or {}).get("content", {}).get("revision") or "")
            if current is not None and expected_revision != current_revision:
                conn.rollback()
                raise DraftRevisionConflict(current)
            created_at = current["created_at"] if current else now
            conn.execute(
                """INSERT INTO derived_records
                   (id, owner_type, owner_id, kind, status, content_json, input_hash, generator, created_at, updated_at)
                   VALUES (?, 'material', ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status, content_json=excluded.content_json,
                     input_hash=excluded.input_hash, generator=excluded.generator, updated_at=excluded.updated_at""",
                (record_id, material_id, kind, status,
                 json.dumps(content, ensure_ascii=False), input_hash, generator, created_at, now),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_derived_record("material", material_id, kind)  # type: ignore[return-value]


    def list_derived_records(
        self, owner_type: str | None = None, kind: str | None = None,
    ) -> list[dict]:
        """枚举派生记录，供生命周期影响预览读取草稿来源。"""
        clauses: list[str] = []
        params: list[str] = []
        if owner_type:
            clauses.append("owner_type=?")
            params.append(owner_type)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM derived_records{where} ORDER BY updated_at DESC", params
            ).fetchall()
            return [self._row_to_derived(row) for row in rows]
        finally:
            conn.close()

    def derived_records_for_owners(
        self, owner_type: str, owner_ids: list[str] | set[str], kind: str,
    ) -> list[dict]:
        """批量读取同类属主的派生产物，供列表投影避免逐行查询。"""
        ids = sorted({str(owner_id) for owner_id in owner_ids if str(owner_id)})
        if not ids:
            return []
        records: list[dict] = []
        # SQLite 的参数上限通常为 999；分批保持大型资料库可用。
        for start in range(0, len(ids), 900):
            batch = ids[start:start + 900]
            placeholders = ",".join("?" for _ in batch)
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM derived_records WHERE owner_type=? AND kind=? "
                    f"AND owner_id IN ({placeholders})",
                    [owner_type, kind, *batch],
                ).fetchall()
                records.extend(self._row_to_derived(row) for row in rows)
            finally:
                conn.close()
        return records

    # ---- P15-05：永久清除时清理派生数据（摘要 / 标签候选 / 实体 / 草稿） ----

    def delete_derived_records_for_material(self, material_id: str) -> int:
        """删除某原材料的全部派生记录（摘要/标签候选/实体），返回受影响行数。"""
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM derived_records WHERE owner_type='material' AND owner_id=?",
                    (material_id,),
                )
                return cursor.rowcount
        finally:
            conn.close()

    def count_parts_for_material(self, material_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM document_parts WHERE material_id=?",
                (material_id,),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    def count_image_parts_for_material(self, material_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM document_parts "
                "WHERE material_id=? AND part_type='image'",
                (material_id,),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    def count_derived_records_for_material(self, material_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM derived_records "
                "WHERE owner_type='material' AND owner_id=?",
                (material_id,),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    def discard_draft(self, draft_id: str, kind: str = "GENERATED_DRAFT") -> bool:
        """把某待审生成草稿标记为已丢弃（永久清除依赖处理；草稿保留为历史记录）。"""
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE derived_records SET status='discarded', updated_at=? "
                    "WHERE owner_type='generation' AND owner_id=? AND kind=?",
                    (time.time(), draft_id, kind),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()

    def set_derived_status(self, owner_type: str, owner_id: str, kind: str, status: str) -> bool:
        """生命周期补偿用：恢复已被本次操作修改的派生记录状态。"""
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE derived_records SET status=?, updated_at=? "
                    "WHERE owner_type=? AND owner_id=? AND kind=?",
                    (status, time.time(), owner_type, owner_id, kind),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()

    # ---- 纠错本（P14-12）：用户确认过的"错误观点 → 已纠正观点"，只允许 active/archived ----

    @staticmethod
    def _row_to_correction(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "title": row["title"],
            "incorrectClaim": row["incorrect_claim"],
            "correctedClaim": row["corrected_claim"],
            "keywords": json.loads(row["keywords_json"] or "[]"),
            "sourceIds": json.loads(row["source_ids_json"] or "[]"),
            "status": row["status"],
            "createdAt": datetime.fromtimestamp(row["created_at"], tz=timezone.utc).isoformat(),
            "updatedAt": datetime.fromtimestamp(row["updated_at"], tz=timezone.utc).isoformat(),
        }

    def create_correction(
        self, title: str, incorrect_claim: str, corrected_claim: str,
        keywords: list[str], source_ids: list[str],
    ) -> dict:
        """创建 active 纠错记录（不物理删除；只允许 active/archived 状态）。"""
        corr_id = "corr_" + uuid.uuid4().hex[:12]
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO corrections
                       (id, title, incorrect_claim, corrected_claim, keywords_json,
                        source_ids_json, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                    (corr_id, title, incorrect_claim, corrected_claim,
                     json.dumps(keywords, ensure_ascii=False),
                     json.dumps(source_ids, ensure_ascii=False), now, now),
                )
        finally:
            conn.close()
        return self.get_correction(corr_id)

    def list_corrections(self, status: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if status in ("active", "archived"):
                rows = conn.execute(
                    "SELECT * FROM corrections WHERE status=? ORDER BY updated_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM corrections ORDER BY updated_at DESC"
                ).fetchall()
            return [self._row_to_correction(r) for r in rows]
        finally:
            conn.close()

    def get_correction(self, corr_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM corrections WHERE id=?", (corr_id,)
            ).fetchone()
            return self._row_to_correction(row) if row else None
        finally:
            conn.close()

    def update_correction(
        self, corr_id: str, title: str, incorrect_claim: str, corrected_claim: str,
        keywords: list[str], source_ids: list[str],
    ) -> dict | None:
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    """UPDATE corrections SET title=?, incorrect_claim=?, corrected_claim=?,
                       keywords_json=?, source_ids_json=?, updated_at=?
                       WHERE id=?""",
                    (title, incorrect_claim, corrected_claim,
                     json.dumps(keywords, ensure_ascii=False),
                     json.dumps(source_ids, ensure_ascii=False), time.time(), corr_id),
                )
                if cursor.rowcount == 0:
                    return None
        finally:
            conn.close()
        return self.get_correction(corr_id)

    def archive_correction(self, corr_id: str) -> dict | None:
        """归档纠错记录（软删除：状态置 archived，不物理删除）。"""
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE corrections SET status='archived', updated_at=? "
                    "WHERE id=? AND status='active'",
                    (time.time(), corr_id),
                )
                if cursor.rowcount == 0:
                    return None
        finally:
            conn.close()
        return self.get_correction(corr_id)

    def set_correction_status(self, corr_id: str, status: str) -> dict | None:
        """生命周期补偿用：仅恢复本次归档前为 active 的纠错记录。"""
        if status not in ("active", "archived"):
            return None
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE corrections SET status=?, updated_at=? WHERE id=?",
                    (status, time.time(), corr_id),
                )
                if cursor.rowcount == 0:
                    return None
        finally:
            conn.close()
        return self.get_correction(corr_id)

    def active_corrections(self) -> list[dict]:
        """返回全部 active 纠错记录（供问答命中检索）。"""
        return self.list_corrections(status="active")


class DraftRevisionConflict(RuntimeError):
    def __init__(self, current: dict | None):
        super().__init__("draft revision conflict")
        self.current = current or {}


def material_id_for_source(source_path: str) -> str | None:
    """把内部索引源路径解析回 MindOS 资料 ID（无则 None）。"""
    for record in JobStore.instance().list():
        if record.get("source_path") == source_path:
            return record.get("material_id")
    return None


def reset_for_tests(db_path=None) -> DerivedStore:
    """测试用：切换到独立 DB 并清空全局实例；无参数时恢复默认派生库路径。"""
    global _INITIALIZED, _DB_PATH, DerivedStore
    _INITIALIZED = False
    if db_path is None:
        _DB_PATH = _DEFAULT_DB_PATH
    else:
        from pathlib import Path
        _DB_PATH = Path(db_path)
    DerivedStore._instance = None
    return DerivedStore.instance()
