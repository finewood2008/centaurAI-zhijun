"""知君成长闭环的本地持久化。

当前 MVP 固定服务于 ``DB_ROOT`` 对应的单盒、单 PersonalVault，不跨 Vault 混读。
该存储只保存用户明确填写的人生章程、判断、结果与复盘，不读取也不改写
Material / Evidence / Entity / Claim 等个人本体权威对象。关联 ID 和 EvidenceRef
在这里均作为不透明引用保存，由未来的权威服务负责解析。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime_paths import GROWTH_DB_PATH

STATUS_OPEN = "open"
STATUS_OUTCOME_RECORDED = "outcome_recorded"
STATUS_REVIEWED = "reviewed"
DECISION_STATUSES = {STATUS_OPEN, STATUS_OUTCOME_RECORDED, STATUS_REVIEWED}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS growth_charters (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE,
    vision TEXT NOT NULL,
    roles_json TEXT NOT NULL,
    principles_json TEXT NOT NULL,
    boundaries_json TEXT NOT NULL,
    goals_json TEXT NOT NULL,
    challenge_style TEXT NOT NULL,
    quiet_domains_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS growth_decisions (
    id TEXT PRIMARY KEY,
    charter_id TEXT,
    charter_version INTEGER,
    title TEXT NOT NULL,
    context TEXT NOT NULL,
    options_json TEXT NOT NULL,
    choice TEXT NOT NULL,
    rationale TEXT NOT NULL,
    confidence INTEGER NOT NULL CHECK(confidence >= 0 AND confidence <= 100),
    expected_outcome TEXT NOT NULL,
    review_at TEXT,
    related_entity_ids_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'outcome_recorded', 'reviewed')),
    outcome_result TEXT,
    outcome_notes TEXT,
    outcome_evidence_refs_json TEXT,
    outcome_recorded_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT,
    FOREIGN KEY(charter_id) REFERENCES growth_charters(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_growth_decision_status
    ON growth_decisions(status, review_at, created_at);

CREATE TABLE IF NOT EXISTS growth_reviews (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    reflection TEXT NOT NULL,
    lessons_json TEXT NOT NULL,
    next_action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(decision_id) REFERENCES growth_decisions(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_growth_review_created
    ON growth_reviews(created_at DESC);
"""

_DECISION_SELECT = """
SELECT d.*,
       r.id AS review_id,
       r.reflection AS review_reflection,
       r.lessons_json AS review_lessons_json,
       r.next_action AS review_next_action,
       r.created_at AS review_created_at
FROM growth_decisions AS d
LEFT JOIN growth_reviews AS r ON r.decision_id = d.id
"""


def utc_now() -> str:
    """返回稳定、可排序且明确为 UTC 的 ISO-8601 时间。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_utc_iso(value: str | datetime | None) -> str | None:
    """拒绝无时区时间，并把所有合法输入规范化为 UTC ``Z``。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("reviewAt 必须是合法 ISO-8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reviewAt 必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_list(value: list[str]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_list(value: str | None) -> list[str]:
    loaded = json.loads(value or "[]")
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


class GrowthConflictError(ValueError):
    """请求违反成长状态机或同一判断已经存在复盘。"""

    def __init__(self, message: str, *, current_status: str | None = None) -> None:
        super().__init__(message)
        self.current_status = current_status


class GrowthStore:
    _instance: "GrowthStore | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else GROWTH_DB_PATH
        self._ready = False
        self._lock = threading.RLock()
        self._ensure()

    @classmethod
    def instance(cls) -> "GrowthStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _ensure(self) -> None:
        if self._ready and self._db_path.is_file():
            return
        with self._lock:
            if self._ready and self._db_path.is_file():
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                if "metadata_json" not in {r[1] for r in conn.execute("PRAGMA table_info(growth_charters)")}:
                    conn.execute("ALTER TABLE growth_charters ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
                columns = {r[1] for r in conn.execute("PRAGMA table_info(growth_charters)")}
                if "document" not in columns:
                    conn.execute("ALTER TABLE growth_charters ADD COLUMN document TEXT NOT NULL DEFAULT ''")
                if "clauses_json" not in columns:
                    conn.execute("ALTER TABLE growth_charters ADD COLUMN clauses_json TEXT NOT NULL DEFAULT '[]'")
                conn.execute("CREATE TABLE IF NOT EXISTS charter_writes (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, charter_id TEXT NOT NULL)")
                conn.commit()
            finally:
                conn.close()
            self._ready = True

    def _connect(self) -> sqlite3.Connection:
        self._ensure()
        from .sqlite_connection import ClosingConnection
        conn = sqlite3.connect(str(self._db_path), timeout=30, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @staticmethod
    def _charter(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "version": int(row["version"]),
            "vision": row["vision"],
            "roles": _load_list(row["roles_json"]),
            "principles": _load_list(row["principles_json"]),
            "boundaries": _load_list(row["boundaries_json"]),
            "goals": _load_list(row["goals_json"]),
            "challengeStyle": row["challenge_style"],
            "quietDomains": _load_list(row["quiet_domains_json"]),
            "createdAt": row["created_at"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "document": row["document"],
            "clauses": json.loads(row["clauses_json"] or "[]"),
        }

    @staticmethod
    def _decision(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        keys = set(row.keys())
        outcome = None
        if row["outcome_recorded_at"]:
            outcome = {
                "result": row["outcome_result"] or "",
                "notes": row["outcome_notes"] or "",
                "evidenceRefs": _load_list(row["outcome_evidence_refs_json"]),
                "recordedAt": row["outcome_recorded_at"],
            }
        review = None
        if "review_id" in keys and row["review_id"]:
            review = {
                "id": row["review_id"],
                "decisionId": row["id"],
                "reflection": row["review_reflection"],
                "lessons": _load_list(row["review_lessons_json"]),
                "nextAction": row["review_next_action"],
                "createdAt": row["review_created_at"],
            }
        return {
            "id": row["id"],
            "status": row["status"],
            "charterId": row["charter_id"],
            "charterVersion": (
                int(row["charter_version"])
                if row["charter_version"] is not None
                else None
            ),
            "title": row["title"],
            "context": row["context"],
            "options": _load_list(row["options_json"]),
            "choice": row["choice"],
            "rationale": row["rationale"],
            "confidence": int(row["confidence"]),
            "expectedOutcome": row["expected_outcome"],
            "reviewAt": row["review_at"],
            "relatedEntityIds": _load_list(row["related_entity_ids_json"]),
            "evidenceRefs": _load_list(row["evidence_refs_json"]),
            "outcome": outcome,
            "review": review,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _review(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "decisionId": row["decision_id"],
            "reflection": row["reflection"],
            "lessons": _load_list(row["lessons_json"]),
            "nextAction": row["next_action"],
            "createdAt": row["created_at"],
        }

    # ---- 人生章程 -------------------------------------------------

    def create_charter(self, payload: dict) -> dict:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                saved = self._insert_charter(conn, payload)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return saved

    def _insert_charter(self, conn, payload):
        """Caller holds one write transaction, including draft acceptance/idempotency."""
        from .alignment_store import digest
        request_id = payload.get("requestId")
        fingerprint = digest(payload)
        if request_id:
            previous = conn.execute("SELECT * FROM charter_writes WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise GrowthConflictError("重复请求的内容不同，请重新核对")
                return self._charter(conn.execute("SELECT * FROM growth_charters WHERE id=?", (previous["charter_id"],)).fetchone())
        latest_version = conn.execute("SELECT COALESCE(MAX(version),0) FROM growth_charters").fetchone()[0]
        scope = (payload.get("metadata") or {}).get("scope", "global")
        previous = self._current_charter(conn, scope)
        current = (previous or {}).get("version", 0)
        if payload.get("expectedVersion") is not None and payload["expectedVersion"] != current:
            raise GrowthConflictError("章程已更新，请基于最新版本重新核对；你的修改仍保留")
        clauses = payload.get("clauses", [])
        document = payload.get("document", "")
        if (previous or {}).get("document") and not payload.get("workspaceId"):
            raise GrowthConflictError("请主动打开章程工作稿修改并确认，旧版表单不能覆盖全文章程")
        if document or clauses:
            from .charter_draft_store import validate_clauses, render_document
            markdown = (payload.get("metadata") or {}).get("documentFormat") == "markdown"
            clauses = validate_clauses(clauses, limit=128 if markdown else 80)
            if (not payload.get("workspaceId") or not clauses or not isinstance(document, str)
                    or not document.strip() or len(document) > 30000
                    or (not markdown and document != render_document(clauses))):
                raise ValueError("全文章程必须通过工作稿确认，正文必须与选中的条款一致")
        fields = ("vision", "roles", "principles", "boundaries", "goals", "challengeStyle", "quietDomains")
        if not clauses and not any(payload.get(f) for f in fields):
            raise ValueError("至少确认一项内容，未填写的部分可以留空")
        ident = f"charter_{uuid.uuid4().hex}"
        previous_meta = (previous or {}).get("metadata") or {}
        # Editing a derived charter in a form must not launder its ancestry.
        metadata = payload.get("metadata") or {"origin": "manual", "scope": previous_meta.get("scope", "global"), "fields": {
            f: {"state": "confirmed" if payload.get(f) else "pending", "sources": previous_meta.get("fields", {}).get(f, {}).get("sources", [])} for f in fields}}
        conn.execute("""INSERT INTO growth_charters
            (id,version,vision,roles_json,principles_json,boundaries_json,goals_json,challenge_style,quiet_domains_json,created_at,metadata_json,document,clauses_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (ident, latest_version + 1, payload.get("vision", ""),
            _json_list(payload.get("roles", [])), _json_list(payload.get("principles", [])),
            _json_list(payload.get("boundaries", [])), _json_list(payload.get("goals", [])),
            payload.get("challengeStyle", ""), _json_list(payload.get("quietDomains", [])), utc_now(), json.dumps(metadata, ensure_ascii=False),
            document, json.dumps(clauses, ensure_ascii=False)))
        if request_id:
            conn.execute("INSERT INTO charter_writes VALUES (?,?,?)", (request_id, fingerprint, ident))
        return self._charter(conn.execute("SELECT * FROM growth_charters WHERE id=?", (ident,)).fetchone())

    def get_charter(self, ident):
        with self._connect() as conn:
            return self._charter(conn.execute("SELECT * FROM growth_charters WHERE id=?", (ident,)).fetchone())

    def _current_charter(self, conn, scope=None):
        where = " WHERE COALESCE(json_extract(metadata_json,'$.scope'),'global')=?" if scope is not None else ""
        return self._charter(conn.execute("SELECT * FROM growth_charters" + where + " ORDER BY version DESC LIMIT 1",
                                         (scope,) if scope is not None else ()).fetchone())

    def current_charter(self, scope=None) -> dict | None:
        with self._connect() as conn:
            return self._current_charter(conn, scope)

    def list_charters(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM growth_charters ORDER BY version DESC"
            ).fetchall()
        return [item for row in rows if (item := self._charter(row)) is not None]

    def charter_history(self, scope=None) -> dict:
        """在同一只读快照中返回当前章程与完整版本历史。"""
        with self._connect() as conn:
            conn.execute("BEGIN")
            where = " WHERE COALESCE(json_extract(metadata_json,'$.scope'),'global')=?" if scope is not None else ""
            rows = conn.execute("SELECT * FROM growth_charters" + where + " ORDER BY version DESC",
                                (scope,) if scope is not None else ()).fetchall()
            conn.commit()
        versions = [
            item for row in rows if (item := self._charter(row)) is not None
        ]
        return {
            "currentCharter": versions[0] if versions else None,
            "versions": versions,
        }

    # ---- 判断 -----------------------------------------------------

    def create_decision(self, payload: dict) -> dict:
        decision_id = f"decision_{uuid.uuid4().hex}"
        review_at = normalize_utc_iso(payload.get("reviewAt"))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                scope = payload.get("scope", "global")
                if "charterBasis" in payload:
                    basis = payload["charterBasis"]
                    if basis is not None and (not isinstance(basis, dict) or "charterId" not in basis or
                            type(basis.get("version")) is not int or basis["version"] < 0 or
                            (basis["charterId"] is not None and not isinstance(basis["charterId"], str))):
                        raise GrowthConflictError("判断章程依据格式不正确")
                    if basis and basis.get("scope", scope) != scope:
                        raise GrowthConflictError("判断使用的章程不属于当前设备")
                    if basis and basis.get("charterId") is None and basis.get("version", 0) == 0:
                        basis = None
                    charter_row = conn.execute("SELECT * FROM growth_charters WHERE id=?", ((basis or {}).get("charterId"),)).fetchone() if basis else None
                    if basis and (not charter_row or int(charter_row["version"]) != basis.get("version") or
                                  json.loads(charter_row["metadata_json"] or "{}").get("scope", "global") != scope or
                                  basis.get("scope", scope) != scope):
                        raise GrowthConflictError("判断使用的章程版本不存在或不属于当前设备")
                else:
                    charter_row = self._current_charter(conn, scope)
                charter_id = charter_row["id"] if charter_row else None
                charter_version = int(charter_row["version"]) if charter_row else None
                conn.execute(
                    """INSERT INTO growth_decisions
                       (id, charter_id, charter_version, title, context, options_json,
                        choice, rationale, confidence, expected_outcome, review_at,
                        related_entity_ids_json, evidence_refs_json, status, created_at,
                        updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                    (
                        decision_id,
                        charter_id,
                        charter_version,
                        payload["title"],
                        payload["context"],
                        _json_list(payload["options"]),
                        payload["choice"],
                        payload["rationale"],
                        int(payload["confidence"]),
                        payload["expectedOutcome"],
                        review_at,
                        _json_list(payload["relatedEntityIds"]),
                        _json_list(payload["evidenceRefs"]),
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    _DECISION_SELECT + " WHERE d.id=?", (decision_id,)
                ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._decision(row) or {}

    def get_decision(self, decision_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                _DECISION_SELECT + " WHERE d.id=?", (decision_id,)
            ).fetchone()
        return self._decision(row)

    def list_decisions(self, status: str | None = None) -> list[dict]:
        if status is not None and status not in DECISION_STATUSES:
            raise ValueError("invalid decision status")
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    _DECISION_SELECT + " ORDER BY d.created_at DESC, d.id DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    _DECISION_SELECT + " WHERE d.status=? "
                    "ORDER BY d.created_at DESC, d.id DESC",
                    (status,),
                ).fetchall()
        return [item for row in rows if (item := self._decision(row)) is not None]

    def record_outcome(self, decision_id: str, payload: dict) -> dict | None:
        """将 open 判断原子推进到 outcome_recorded；非 open 状态拒绝覆盖。"""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM growth_decisions WHERE id=?", (decision_id,)
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                if row["status"] != STATUS_OPEN:
                    conn.rollback()
                    raise GrowthConflictError(
                        "只有 open 判断可以记录结果",
                        current_status=str(row["status"]),
                    )
                now = utc_now()
                cursor = conn.execute(
                    """UPDATE growth_decisions
                       SET status='outcome_recorded', outcome_result=?, outcome_notes=?,
                           outcome_evidence_refs_json=?, outcome_recorded_at=?, updated_at=?
                       WHERE id=? AND status='open'""",
                    (
                        payload["result"],
                        payload["notes"],
                        _json_list(payload["evidenceRefs"]),
                        now,
                        now,
                        decision_id,
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise GrowthConflictError("判断状态已变化")
                saved = conn.execute(
                    _DECISION_SELECT + " WHERE d.id=?", (decision_id,)
                ).fetchone()
                conn.commit()
            except GrowthConflictError:
                raise
            except Exception:
                conn.rollback()
                raise
        return self._decision(saved)

    # ---- 复盘 -----------------------------------------------------

    def create_review(self, payload: dict) -> dict | None:
        """写入复盘并原子把 outcome_recorded 判断推进到 reviewed。"""
        review_id = f"review_{uuid.uuid4().hex}"
        decision_id = payload["decisionId"]
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                decision_row = conn.execute(
                    "SELECT * FROM growth_decisions WHERE id=?", (decision_id,)
                ).fetchone()
                if decision_row is None:
                    conn.rollback()
                    return None
                if decision_row["status"] != STATUS_OUTCOME_RECORDED:
                    conn.rollback()
                    raise GrowthConflictError(
                        "只有 outcome_recorded 判断可以完成复盘",
                        current_status=str(decision_row["status"]),
                    )
                now = utc_now()
                conn.execute(
                    """INSERT INTO growth_reviews
                       (id, decision_id, reflection, lessons_json, next_action, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        review_id,
                        decision_id,
                        payload["reflection"],
                        _json_list(payload["lessons"]),
                        payload["nextAction"],
                        now,
                    ),
                )
                cursor = conn.execute(
                    """UPDATE growth_decisions
                       SET status='reviewed', reviewed_at=?, updated_at=?
                       WHERE id=? AND status='outcome_recorded'""",
                    (now, now, decision_id),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise GrowthConflictError("判断状态已变化")
                review_row = conn.execute(
                    "SELECT * FROM growth_reviews WHERE id=?", (review_id,)
                ).fetchone()
                saved_decision = conn.execute(
                    _DECISION_SELECT + " WHERE d.id=?", (decision_id,)
                ).fetchone()
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise GrowthConflictError("该判断已经完成复盘") from exc
            except GrowthConflictError:
                raise
            except Exception:
                conn.rollback()
                raise
        return {
            "review": self._review(review_row),
            "decision": self._decision(saved_decision),
        }

    def latest_review(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM growth_reviews ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return self._review(row)

    # ---- 今日 -----------------------------------------------------

    def today(self, *, now: datetime | None = None, due_days: int = 7) -> dict:
        supplied_now = now or datetime.now(timezone.utc)
        if supplied_now.tzinfo is None or supplied_now.utcoffset() is None:
            raise ValueError("today.now 必须包含时区")
        current = supplied_now.astimezone(timezone.utc)
        current_iso = current.isoformat().replace("+00:00", "Z")
        horizon_iso = (current + timedelta(days=due_days)).isoformat().replace("+00:00", "Z")

        # 一次只读事务提供相互一致的今日聚合快照。
        with self._connect() as conn:
            conn.execute("BEGIN")
            charter_row = conn.execute(
                "SELECT * FROM growth_charters ORDER BY version DESC LIMIT 1"
            ).fetchone()
            due_rows = conn.execute(
                _DECISION_SELECT + """
                   WHERE d.status='open' AND d.review_at IS NOT NULL
                     AND julianday(d.review_at) <= julianday(?)
                   ORDER BY CASE
                                WHEN julianday(d.review_at) < julianday(?) THEN 0
                                ELSE 1
                            END,
                            julianday(d.review_at) ASC, d.created_at ASC""",
                (horizon_iso, current_iso),
            ).fetchall()
            pending_rows = conn.execute(
                _DECISION_SELECT + """
                   WHERE d.status='outcome_recorded'
                   ORDER BY d.outcome_recorded_at ASC, d.created_at ASC"""
            ).fetchall()
            latest_review_row = conn.execute(
                "SELECT * FROM growth_reviews ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM growth_decisions GROUP BY status"
            ).fetchall()
            review_count = int(
                conn.execute("SELECT COUNT(*) FROM growth_reviews").fetchone()[0]
            )
            conn.commit()

        all_due_decisions: list[dict] = []
        overdue = 0
        for row in due_rows:
            item = self._decision(row)
            if item is None:
                continue
            review_at = datetime.fromisoformat(
                str(item["reviewAt"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            due_state = "overdue" if review_at < current else "due_soon"
            if due_state == "overdue":
                overdue += 1
            item["dueState"] = due_state
            all_due_decisions.append(item)

        counts = {str(row["status"]): int(row["count"]) for row in status_rows}
        total = sum(counts.values())
        charter = self._charter(charter_row)
        pending = [
            item for row in pending_rows if (item := self._decision(row)) is not None
        ]

        # 今日页最多三个主要事项。优先级固定为：逾期判断 → 待复盘结果 →
        # 未来七天到期判断；统计仍基于完整集合，不因展示上限失真。
        ranked: list[tuple[int, int, str, dict]] = []
        for index, item in enumerate(all_due_decisions):
            rank = 0 if item["dueState"] == "overdue" else 2
            ranked.append((rank, index, str(item.get("reviewAt") or ""), item))
        for index, item in enumerate(pending):
            recorded_at = str((item.get("outcome") or {}).get("recordedAt") or "")
            ranked.append((1, index, recorded_at, item))
        selected = [entry[3] for entry in sorted(ranked, key=lambda entry: entry[:3])[:3]]
        selected_ids = {str(item["id"]) for item in selected}
        today_items = []
        for item in selected:
            is_pending_review = item["status"] == STATUS_OUTCOME_RECORDED
            today_items.append({
                "type": "pending_review" if is_pending_review else "decision_due",
                "urgency": (
                    "pending_review" if is_pending_review else item["dueState"]
                ),
                "decision": item,
            })
        due_decisions = [
            item for item in all_due_decisions if str(item["id"]) in selected_ids
        ]
        pending_reviews = [
            item for item in pending if str(item["id"]) in selected_ids
        ]
        return {
            "generatedAt": current_iso,
            "currentCharter": charter,
            "todayItems": today_items,
            "dueDecisions": due_decisions,
            "pendingReviews": pending_reviews,
            "latestReview": self._review(latest_review_row),
            "stats": {
                "charterVersion": charter["version"] if charter else None,
                "totalDecisions": total,
                "openDecisions": counts.get(STATUS_OPEN, 0),
                "dueSoonDecisions": len(all_due_decisions) - overdue,
                "overdueDecisions": overdue,
                "pendingReviews": counts.get(STATUS_OUTCOME_RECORDED, 0),
                "reviewedDecisions": counts.get(STATUS_REVIEWED, 0),
                "totalReviews": review_count,
            },
        }


def reset_for_tests(db_path: str | Path | None = None) -> GrowthStore:
    """切换单例到隔离数据库；生产代码不调用。"""
    with GrowthStore._instance_lock:
        GrowthStore._instance = GrowthStore(db_path)
        return GrowthStore._instance
