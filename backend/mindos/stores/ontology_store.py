"""知君个人本体的本地持久化：实体、理解（Claim）、证据、复核事件与本体后台任务。

设计边界：
- 本体是「关于用户的理解」，不是资料索引。实体 / 理解 / 证据 / 复核事件同库同事务域，
  与 growth.db（用户显式填写的判断）和 job_store（材料索引）互不引用，避免形成第二事实源。
- 信任状态机的唯一入口是 ``OntologyStore.transition``；任何自动流程都不能绕过它改 trust_state。
- 撤回 / 替代只打墓碑不删行，保证「被纠正的理解不再回到回答里」这一约束可被检索与验证。
- 检索用 jieba 词面重叠 + 时间衰减（个人本体量级 ≤ 1e4 条，不需要向量库）；缺 jieba 时退化为
  CJK 字符二元组，保证测试环境与最小安装也可运行。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime_paths import ONTOLOGY_DB_PATH

try:  # pragma: no cover - 依赖存在与否由环境决定
    import jieba  # type: ignore

    jieba.setLogLevel(60)
except Exception:  # noqa: BLE001
    jieba = None  # type: ignore


ME_ENTITY_ID = "ent_me"
ME_ENTITY_NAME = "我"

SECTIONS = ("who", "people", "matters", "principles", "ways", "direction")
LAYERS = ("observed", "self_declared", "aspirational", "hypothesis")
TRUST_STATES = ("working", "confirmed", "retracted", "superseded")
TRUST_ORIGINS = ("utterance", "user_confirm", "user_edit", "user_created", "material", "model")
ENTITY_TYPES = ("me", "person", "organization", "project", "place", "topic", "event", "term")
PRIVACY_LEVELS = ("public", "private", "sensitive", "restricted")
SCOPES = ("long_term", "context_only")
EVIDENCE_KINDS = ("conversation_turn", "material_span", "user_edit", "decision", "review")
STANCES = ("supports", "contradicts", "background")
REVIEW_ACTIONS = (
    "confirm",
    "partial",
    "context_only",
    "reject",
    "defer",
    "retract",
    "reaffirm",
    "create",
)
SURFACES = ("conversation", "ontology_page", "onboarding", "decision_panel", "import", "system")
JOB_KINDS = ("extract_turn", "extract_material", "summarize_conversation", "consolidate", "project", "nudge_scan", "draft_turn", "first_observation")
JOB_STATES = ("queued", "running", "done", "failed")

# 受控谓词词表：抽取器只能在分区对应的词表内选，越界整条丢弃。
PREDICATES: dict[str, tuple[str, ...]] = {
    "who": ("is", "has_trait", "background", "role"),
    "people": ("knows", "works_with", "relationship", "attitude_toward"),
    "matters": ("working_on", "committed_to", "happened", "owns"),
    "principles": ("holds_principle", "boundary"),
    "ways": ("prefers", "tends_to", "decides_by"),
    "direction": ("wants_to", "goal", "avoids"),
}
DEFAULT_PREDICATE = {
    "who": "is",
    "people": "knows",
    "matters": "working_on",
    "principles": "holds_principle",
    "ways": "tends_to",
    "direction": "wants_to",
}

SECTION_TITLES = {
    "who": "我是谁",
    "people": "我的人",
    "matters": "我的事",
    "principles": "我的原则",
    "ways": "我的做法",
    "direction": "我的方向",
}
LAYER_TITLES = {
    "self_declared": "你告诉我的",
    "observed": "资料里看到的",
    "hypothesis": "我推测的",
    "aspirational": "你想成为的",
}

DEFER_DAYS = 14

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('me','person','organization','project','place','topic','event','term')),
    canonical_name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','merged','retracted')),
    merged_into_id TEXT,
    privacy_level TEXT NOT NULL DEFAULT 'private',
    device_scope TEXT NOT NULL DEFAULT 'global',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_name_norm ON entities(name_norm);

CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id TEXT NOT NULL REFERENCES entities(id),
    alias TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('canonical','extracted','user','merge')),
    created_at TEXT NOT NULL,
    PRIMARY KEY(entity_id, alias_norm)
);
CREATE INDEX IF NOT EXISTS idx_alias_norm ON entity_aliases(alias_norm);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    subject_entity_id TEXT NOT NULL REFERENCES entities(id),
    predicate TEXT NOT NULL,
    object_entity_id TEXT REFERENCES entities(id),
    content TEXT NOT NULL,
    section TEXT NOT NULL CHECK(section IN ('who','people','matters','principles','ways','direction')),
    self_model_layer TEXT NOT NULL CHECK(self_model_layer IN ('observed','self_declared','aspirational','hypothesis')),
    trust_state TEXT NOT NULL CHECK(trust_state IN ('working','confirmed','retracted','superseded')),
    trust_origin TEXT NOT NULL CHECK(trust_origin IN ('utterance','user_confirm','user_edit','user_created','material','model')),
    confidence REAL NOT NULL DEFAULT 0.5,
    scope TEXT NOT NULL DEFAULT 'long_term' CHECK(scope IN ('long_term','context_only')),
    context_ref TEXT,
    privacy_level TEXT NOT NULL DEFAULT 'private' CHECK(privacy_level IN ('public','private','sensitive','restricted')),
    export_allowed INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT,
    valid_to TEXT,
    challenged INTEGER NOT NULL DEFAULT 0,
    challenge_note TEXT,
    deferred_until TEXT,
    first_seen TEXT NOT NULL,
    last_reaffirmed TEXT NOT NULL,
    supersedes_id TEXT,
    superseded_by_id TEXT,
    retracted_at TEXT,
    retraction_reason TEXT,
    content_hash TEXT NOT NULL,
    device_scope TEXT NOT NULL DEFAULT 'global',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_subject ON claims(subject_entity_id, trust_state);
CREATE INDEX IF NOT EXISTS idx_claims_state ON claims(trust_state, section, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_claims_hash ON claims(content_hash);
CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_active_hash
    ON claims(content_hash) WHERE trust_state IN ('working','confirmed');

CREATE TABLE IF NOT EXISTS claim_evidence (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    kind TEXT NOT NULL CHECK(kind IN ('conversation_turn','material_span','user_edit','decision','review')),
    stance TEXT NOT NULL DEFAULT 'supports' CHECK(stance IN ('supports','contradicts','background')),
    conversation_id TEXT,
    message_id TEXT,
    material_id TEXT,
    chunk_key TEXT,
    locator_json TEXT,
    decision_id TEXT,
    quote TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_claim ON claim_evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_evidence_material ON claim_evidence(material_id);
CREATE INDEX IF NOT EXISTS idx_evidence_conversation ON claim_evidence(conversation_id);

CREATE TABLE IF NOT EXISTS review_events (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('claim','entity')),
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'user',
    surface TEXT NOT NULL,
    conversation_id TEXT,
    message_id TEXT,
    before_json TEXT,
    after_json TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_target ON review_events(target_type, target_id, created_at);

CREATE TABLE IF NOT EXISTS ontology_jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','done','failed')),
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until REAL,
    failure_class TEXT,
    error_code TEXT,
    error_detail TEXT,
    input_hash TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ontology_jobs_active
    ON ontology_jobs(kind, owner_id) WHERE state IN ('queued','running');
CREATE INDEX IF NOT EXISTS idx_ontology_jobs_state ON ontology_jobs(state, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS ontology_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_merge_proposals (
    id TEXT PRIMARY KEY,
    from_entity_id TEXT NOT NULL REFERENCES entities(id),
    into_entity_id TEXT NOT NULL REFERENCES entities(id),
    reason TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_merge_pair ON entity_merge_proposals(from_entity_id, into_entity_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS claim_conflicts (
    id TEXT PRIMARY KEY,
    claim_a_id TEXT NOT NULL REFERENCES claims(id),
    claim_b_id TEXT NOT NULL REFERENCES claims(id),
    kind TEXT NOT NULL CHECK(kind IN ('contradiction','tension')),
    verdict_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved','dismissed')),
    resolution TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_conflict_pair ON claim_conflicts(claim_a_id, claim_b_id) WHERE status = 'pending';
"""

_CLAIM_SELECT = """
SELECT c.*,
       s.canonical_name AS subject_name,
       o.canonical_name AS object_name
FROM claims AS c
LEFT JOIN entities AS s ON s.id = c.subject_entity_id
LEFT JOIN entities AS o ON o.id = c.object_entity_id
"""

_PUNCT_RE = re.compile(r"[\s，。！？；：、,.!?;:\"'“”‘’()（）\[\]【】<>《》\-—…·/\\|]+")
_CJK_RE = re.compile(r"[一-鿿]+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalize_text(text: str) -> str:
    """用于哈希与别名匹配的归一化：小写、去空白与标点、全角数字字母转半角。"""
    text = (text or "").strip().lower()
    out = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        out.append(ch)
    return _PUNCT_RE.sub("", "".join(out))


def content_hash(subject_entity_id: str, predicate: str, content: str) -> str:
    raw = f"{subject_entity_id}|{predicate}|{normalize_text(content)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def tokenize(text: str) -> set[str]:
    """检索分词：jieba 词（长度 ≥2）+ ASCII 词；无 jieba 时退化为 CJK 二元组。"""
    text = (text or "").strip()
    if not text:
        return set()
    tokens: set[str] = set()
    for word in _ASCII_WORD_RE.findall(text.lower()):
        tokens.add(word)
    if jieba is not None:
        try:
            for word in jieba.lcut(text):
                word = word.strip()
                if len(word) >= 2 and not _PUNCT_RE.fullmatch(word) and not _ASCII_WORD_RE.fullmatch(word):
                    tokens.add(word)
            return tokens
        except Exception:  # noqa: BLE001 - 退化到二元组
            pass
    for run in _CJK_RE.findall(text):
        if len(run) == 1:
            tokens.add(run)
        for i in range(len(run) - 1):
            tokens.add(run[i : i + 2])
    return tokens


def lexical_similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / math.sqrt(len(a) * len(b))


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class OntologyError(ValueError):
    """本体请求不合法（字段越界等），API 层映射为 400。"""


class OntologyNotFoundError(OntologyError):
    """目标不存在，API 层映射为 404。"""


class OntologyConflictError(OntologyError):
    """违反信任状态机或活跃理解重复，API 层映射为 409。"""

    def __init__(self, message: str, *, current_state: str | None = None) -> None:
        super().__init__(message)
        self.current_state = current_state


class OntologyStore:
    _instance: "OntologyStore | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else ONTOLOGY_DB_PATH
        self._ready = False
        self._lock = threading.RLock()
        self._ensure()

    @classmethod
    def instance(cls) -> "OntologyStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ------------------------------------------------------------------ 连接
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
                now = utc_now()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO entities
                        (id, type, canonical_name, name_norm, description, status, created_at, updated_at)
                    VALUES (?, 'me', ?, ?, '用户本人', 'active', ?, ?)
                    """,
                    (ME_ENTITY_ID, ME_ENTITY_NAME, normalize_text(ME_ENTITY_NAME), now, now),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO entity_aliases (entity_id, alias, alias_norm, source, created_at)
                    VALUES (?, ?, ?, 'canonical', ?)
                    """,
                    (ME_ENTITY_ID, ME_ENTITY_NAME, normalize_text(ME_ENTITY_NAME), now),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO ontology_meta (key, value) VALUES ('schema_version', '1')"
                )
                # P3：多来源晋升标记（旧库补列）。
                columns = {row[1] for row in conn.execute("PRAGMA table_info(claims)")}
                if "promotion_ready" not in columns:
                    conn.execute("ALTER TABLE claims ADD COLUMN promotion_ready INTEGER NOT NULL DEFAULT 0")
                conn.commit()
            finally:
                conn.close()
            self._ready = True

    def _connect(self) -> sqlite3.Connection:
        self._ensure()
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    # ------------------------------------------------------------------ meta
    def meta_get(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM ontology_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO ontology_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def _bump_revision(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO ontology_meta (key, value) VALUES ('revision', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
        )

    # ------------------------------------------------------------------ 实体
    @staticmethod
    def _entity(row: sqlite3.Row | None, aliases: list[str] | None = None, claim_count: int = 0) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "type": row["type"],
            "canonicalName": row["canonical_name"],
            "aliases": aliases or [],
            "description": row["description"] or "",
            "status": row["status"],
            "mergedIntoId": row["merged_into_id"],
            "claimCount": int(claim_count),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _aliases(self, conn: sqlite3.Connection, entity_id: str) -> list[str]:
        rows = conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY created_at", (entity_id,)
        ).fetchall()
        return [r["alias"] for r in rows]

    def _claim_count(self, conn: sqlite3.Connection, entity_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM claims WHERE trust_state IN ('working','confirmed') "
            "AND (subject_entity_id = ? OR object_entity_id = ?)",
            (entity_id, entity_id),
        ).fetchone()
        return int(row["n"]) if row else 0

    def get_entity(self, entity_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
            if row is None:
                return None
            return self._entity(row, self._aliases(conn, entity_id), self._claim_count(conn, entity_id))

    def find_entity(self, name: str, entity_type: str | None = None) -> dict | None:
        norm = normalize_text(name)
        if not norm:
            return None
        with self._connect() as conn:
            query = (
                "SELECT e.* FROM entities e "
                "LEFT JOIN entity_aliases a ON a.entity_id = e.id "
                "WHERE e.status = 'active' AND (e.name_norm = ? OR a.alias_norm = ?)"
            )
            params: list = [norm, norm]
            if entity_type:
                query += " AND e.type = ?"
                params.append(entity_type)
            row = conn.execute(query + " LIMIT 1", params).fetchone()
            if row is None:
                return None
            return self._entity(row, self._aliases(conn, row["id"]), self._claim_count(conn, row["id"]))

    def upsert_entity(
        self,
        name: str,
        entity_type: str = "person",
        *,
        aliases: list[str] | tuple[str, ...] = (),
        description: str = "",
        alias_source: str = "extracted",
    ) -> dict:
        name = (name or "").strip()
        if not name:
            raise OntologyError("实体名称不能为空")
        if entity_type not in ENTITY_TYPES:
            raise OntologyError(f"实体类型不合法：{entity_type}")
        if len(name) > 80:
            raise OntologyError("实体名称过长")
        if entity_type == "me" or normalize_text(name) == normalize_text(ME_ENTITY_NAME):
            return self.get_entity(ME_ENTITY_ID)  # type: ignore[return-value]
        norm = normalize_text(name)
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT e.* FROM entities e LEFT JOIN entity_aliases a ON a.entity_id = e.id "
                    "WHERE e.status = 'active' AND (e.name_norm = ? OR a.alias_norm = ?) LIMIT 1",
                    (norm, norm),
                ).fetchone()
                if row is None:
                    entity_id = f"ent_{uuid.uuid4().hex[:12]}"
                    conn.execute(
                        """
                        INSERT INTO entities
                            (id, type, canonical_name, name_norm, description, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                        """,
                        (entity_id, entity_type, name, norm, description or "", now, now),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, alias_norm, source, created_at) "
                        "VALUES (?, ?, ?, 'canonical', ?)",
                        (entity_id, name, norm, now),
                    )
                else:
                    entity_id = row["id"]
                    if description and not row["description"]:
                        conn.execute(
                            "UPDATE entities SET description = ?, updated_at = ? WHERE id = ?",
                            (description, now, entity_id),
                        )
                for alias in aliases:
                    alias = (alias or "").strip()
                    alias_norm = normalize_text(alias)
                    if not alias_norm or alias_norm == norm or len(alias) > 80:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, alias_norm, source, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (entity_id, alias, alias_norm, alias_source, now),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
            return self._entity(row, self._aliases(conn, entity_id), self._claim_count(conn, entity_id))  # type: ignore[return-value]

    def list_entities(self, entity_type: str | None = None, *, limit: int = 500) -> list[dict]:
        with self._connect() as conn:
            query = "SELECT * FROM entities WHERE status = 'active'"
            params: list = []
            if entity_type:
                query += " AND type = ?"
                params.append(entity_type)
            query += " ORDER BY CASE WHEN type = 'me' THEN 0 ELSE 1 END, updated_at DESC LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(query, params).fetchall()
            return [
                self._entity(r, self._aliases(conn, r["id"]), self._claim_count(conn, r["id"]))  # type: ignore[misc]
                for r in rows
            ]

    def entity_names_for_conversation(self, conversation_id: str) -> list[str]:
        """本会话里已出现过的非「我」实体名（供抽取器做代词消解，不泄露全库人名）。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT e.canonical_name AS name
                FROM claim_evidence ce
                JOIN claims c ON c.id = ce.claim_id
                JOIN entities e ON e.id = c.subject_entity_id OR e.id = c.object_entity_id
                WHERE ce.conversation_id = ? AND e.type != 'me' AND e.status = 'active'
                ORDER BY e.canonical_name
                """,
                (conversation_id,),
            ).fetchall()
        return [r["name"] for r in rows]

    # ------------------------------------------------------------------ 理解
    @staticmethod
    def _evidence(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "stance": row["stance"],
            "conversationId": row["conversation_id"],
            "messageId": row["message_id"],
            "materialId": row["material_id"],
            "chunkKey": row["chunk_key"],
            "locator": _load(row["locator_json"], None),
            "decisionId": row["decision_id"],
            "quote": row["quote"] or "",
            "createdAt": row["created_at"],
        }

    @staticmethod
    def _claim(row: sqlite3.Row | None, evidence: list[dict] | None = None) -> dict | None:
        if row is None:
            return None
        keys = set(row.keys())
        return {
            "id": row["id"],
            "subjectEntityId": row["subject_entity_id"],
            "subjectName": row["subject_name"] if "subject_name" in keys else None,
            "predicate": row["predicate"],
            "objectEntityId": row["object_entity_id"],
            "objectName": row["object_name"] if "object_name" in keys else None,
            "content": row["content"],
            "section": row["section"],
            "layer": row["self_model_layer"],
            "trustState": row["trust_state"],
            "trustOrigin": row["trust_origin"],
            "confidence": float(row["confidence"]),
            "scope": row["scope"],
            "contextRef": row["context_ref"],
            "privacyLevel": row["privacy_level"],
            "exportAllowed": bool(row["export_allowed"]),
            "validFrom": row["valid_from"],
            "validTo": row["valid_to"],
            "challenged": bool(row["challenged"]),
            "challengeNote": row["challenge_note"],
            "promotionReady": bool(row["promotion_ready"]) if "promotion_ready" in keys else False,
            "deferredUntil": row["deferred_until"],
            "firstSeen": row["first_seen"],
            "lastReaffirmed": row["last_reaffirmed"],
            "supersedesId": row["supersedes_id"],
            "supersededById": row["superseded_by_id"],
            "retractedAt": row["retracted_at"],
            "retractionReason": row["retraction_reason"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "evidence": evidence or [],
        }

    def _evidence_for(self, conn: sqlite3.Connection, claim_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM claim_evidence WHERE claim_id = ? ORDER BY created_at", (claim_id,)
        ).fetchall()
        return [self._evidence(r) for r in rows]

    def _fetch_claim(self, conn: sqlite3.Connection, claim_id: str, with_evidence: bool = True) -> dict | None:
        row = conn.execute(_CLAIM_SELECT + " WHERE c.id = ?", (claim_id,)).fetchone()
        if row is None:
            return None
        return self._claim(row, self._evidence_for(conn, claim_id) if with_evidence else [])

    def get_claim(self, claim_id: str, *, with_evidence: bool = True) -> dict | None:
        with self._connect() as conn:
            return self._fetch_claim(conn, claim_id, with_evidence)

    @staticmethod
    def _validate_claim_payload(payload: dict) -> dict:
        section = payload.get("section")
        if section not in SECTIONS:
            raise OntologyError(f"section 不合法：{section}")
        layer = payload.get("layer") or payload.get("self_model_layer")
        if layer not in LAYERS:
            raise OntologyError(f"layer 不合法：{layer}")
        predicate = payload.get("predicate") or DEFAULT_PREDICATE[section]
        if predicate not in PREDICATES[section]:
            raise OntologyError(f"predicate 不在 {section} 的词表内：{predicate}")
        content = (payload.get("content") or "").strip()
        if not content:
            raise OntologyError("content 不能为空")
        if len(content) > 120:
            raise OntologyError("content 不能超过 120 字")
        scope = payload.get("scope") or "long_term"
        if scope not in SCOPES:
            raise OntologyError(f"scope 不合法：{scope}")
        privacy = payload.get("privacy_level") or payload.get("privacyLevel") or "private"
        if privacy not in PRIVACY_LEVELS:
            raise OntologyError(f"privacy_level 不合法：{privacy}")
        try:
            confidence = float(payload.get("confidence", 0.5))
        except (TypeError, ValueError) as exc:
            raise OntologyError("confidence 必须是数字") from exc
        confidence = max(0.0, min(1.0, confidence))
        return {
            "subject_entity_id": payload.get("subject_entity_id") or ME_ENTITY_ID,
            "predicate": predicate,
            "object_entity_id": payload.get("object_entity_id"),
            "content": content,
            "section": section,
            "layer": layer,
            "confidence": confidence,
            "scope": scope,
            "context_ref": payload.get("context_ref"),
            "privacy_level": privacy,
            "export_allowed": 1 if payload.get("export_allowed") else 0,
            "valid_from": payload.get("valid_from"),
            "valid_to": payload.get("valid_to"),
            "device_scope": payload.get("device_scope") or "global",
        }

    @staticmethod
    def _validate_evidence(item: dict) -> dict:
        kind = item.get("kind") or "conversation_turn"
        if kind not in EVIDENCE_KINDS:
            raise OntologyError(f"证据类型不合法：{kind}")
        stance = item.get("stance") or "supports"
        if stance not in STANCES:
            raise OntologyError(f"证据立场不合法：{stance}")
        quote = (item.get("quote") or "").strip()
        if len(quote) > 300:
            quote = quote[:300]
        locator = item.get("locator")
        return {
            "kind": kind,
            "stance": stance,
            "conversation_id": item.get("conversation_id") or item.get("conversationId"),
            "message_id": item.get("message_id") or item.get("messageId"),
            "material_id": item.get("material_id") or item.get("materialId"),
            "chunk_key": item.get("chunk_key") or item.get("chunkKey"),
            "locator_json": _json(locator) if locator is not None else None,
            "decision_id": item.get("decision_id") or item.get("decisionId"),
            "quote": quote,
        }

    def _insert_evidence(self, conn: sqlite3.Connection, claim_id: str, items: list[dict]) -> None:
        now = utc_now()
        for item in items:
            ev = self._validate_evidence(item)
            conn.execute(
                """
                INSERT INTO claim_evidence
                    (id, claim_id, kind, stance, conversation_id, message_id, material_id, chunk_key,
                     locator_json, decision_id, quote, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"ev_{uuid.uuid4().hex[:12]}",
                    claim_id,
                    ev["kind"],
                    ev["stance"],
                    ev["conversation_id"],
                    ev["message_id"],
                    ev["material_id"],
                    ev["chunk_key"],
                    ev["locator_json"],
                    ev["decision_id"],
                    ev["quote"],
                    now,
                ),
            )

    def _insert_review_event(
        self,
        conn: sqlite3.Connection,
        *,
        target_type: str,
        target_id: str,
        action: str,
        surface: str,
        actor: str = "user",
        conversation_id: str | None = None,
        message_id: str | None = None,
        before: dict | None = None,
        after: dict | None = None,
        note: str = "",
    ) -> str:
        event_id = f"rev_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO review_events
                (id, target_type, target_id, action, actor, surface, conversation_id, message_id,
                 before_json, after_json, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                target_type,
                target_id,
                action,
                actor,
                surface,
                conversation_id,
                message_id,
                _json(before) if before is not None else None,
                _json(after) if after is not None else None,
                note or "",
                utc_now(),
            ),
        )
        return event_id

    def create_claim(
        self,
        payload: dict,
        evidence: list[dict] | None = None,
        *,
        trust_state: str = "working",
        trust_origin: str = "model",
        surface: str = "conversation",
        actor: str = "user",
        conversation_id: str | None = None,
        message_id: str | None = None,
        supersedes_id: str | None = None,
        note: str = "",
    ) -> dict:
        """写入一条理解。信任状态由调用方按入口规则决定：
        - 用户手写 → confirmed / user_created；
        - 用户原话且抽取校验通过 → confirmed / utterance；
        - 其余（模型推测、资料观察）→ working。
        活跃理解（working/confirmed）按 content_hash 唯一；重复时抛 OntologyConflictError。
        """
        if trust_state not in ("working", "confirmed"):
            raise OntologyError("新理解只能是 working 或 confirmed")
        if trust_origin not in TRUST_ORIGINS:
            raise OntologyError(f"trust_origin 不合法：{trust_origin}")
        if surface not in SURFACES:
            raise OntologyError(f"surface 不合法：{surface}")
        fields = self._validate_claim_payload(payload)
        if trust_state == "confirmed" and fields["layer"] == "hypothesis":
            raise OntologyError("推测（hypothesis）不能直接写为已确认")
        claim_id = f"clm_{uuid.uuid4().hex[:12]}"
        now = utc_now()
        digest = content_hash(fields["subject_entity_id"], fields["predicate"], fields["content"])
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for entity_id in (fields["subject_entity_id"], fields["object_entity_id"]):
                    if entity_id and conn.execute("SELECT 1 FROM entities WHERE id = ?", (entity_id,)).fetchone() is None:
                        raise OntologyNotFoundError(f"实体不存在：{entity_id}")
                try:
                    conn.execute(
                        """
                        INSERT INTO claims
                            (id, subject_entity_id, predicate, object_entity_id, content, section,
                             self_model_layer, trust_state, trust_origin, confidence, scope, context_ref,
                             privacy_level, export_allowed, valid_from, valid_to, first_seen, last_reaffirmed,
                             supersedes_id, content_hash, device_scope, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            claim_id,
                            fields["subject_entity_id"],
                            fields["predicate"],
                            fields["object_entity_id"],
                            fields["content"],
                            fields["section"],
                            fields["layer"],
                            trust_state,
                            trust_origin,
                            fields["confidence"],
                            fields["scope"],
                            fields["context_ref"],
                            fields["privacy_level"],
                            fields["export_allowed"],
                            fields["valid_from"],
                            fields["valid_to"],
                            now,
                            now,
                            supersedes_id,
                            digest,
                            fields["device_scope"],
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise OntologyConflictError("已有一条相同的活跃理解") from exc
                self._insert_evidence(conn, claim_id, evidence or [])
                self._insert_review_event(
                    conn,
                    target_type="claim",
                    target_id=claim_id,
                    action="create",
                    actor=actor if trust_origin in ("utterance", "user_created", "user_edit") else "system",
                    surface=surface,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    after={"trustState": trust_state, "trustOrigin": trust_origin, "content": fields["content"]},
                    note=note,
                )
                self._bump_revision(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return self._fetch_claim(conn, claim_id)  # type: ignore[return-value]

    def add_evidence(self, claim_id: str, evidence: list[dict], *, reaffirm: bool = False) -> dict:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM claims WHERE id = ?", (claim_id,)).fetchone() is None:
                    raise OntologyNotFoundError("理解不存在")
                self._insert_evidence(conn, claim_id, evidence)
                now = utc_now()
                if reaffirm:
                    conn.execute(
                        "UPDATE claims SET last_reaffirmed = ?, updated_at = ? WHERE id = ?",
                        (now, now, claim_id),
                    )
                else:
                    conn.execute("UPDATE claims SET updated_at = ? WHERE id = ?", (now, claim_id))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return self._fetch_claim(conn, claim_id)  # type: ignore[return-value]

    def find_active_by_hash(self, subject_entity_id: str, predicate: str, content: str) -> dict | None:
        digest = content_hash(subject_entity_id, predicate, content)
        with self._connect() as conn:
            row = conn.execute(
                _CLAIM_SELECT + " WHERE c.content_hash = ? AND c.trust_state IN ('working','confirmed') LIMIT 1",
                (digest,),
            ).fetchone()
            return self._claim(row, self._evidence_for(conn, row["id"])) if row else None

    def find_tombstone_by_hash(self, subject_entity_id: str, predicate: str, content: str) -> dict | None:
        digest = content_hash(subject_entity_id, predicate, content)
        with self._connect() as conn:
            row = conn.execute(
                _CLAIM_SELECT
                + " WHERE c.content_hash = ? AND c.trust_state IN ('retracted','superseded') "
                "ORDER BY c.updated_at DESC LIMIT 1",
                (digest,),
            ).fetchone()
            return self._claim(row, []) if row else None

    def list_claims(
        self,
        *,
        section: str | None = None,
        trust_states: tuple[str, ...] | list[str] = ("confirmed",),
        limit: int = 200,
        include_hidden: bool = True,
        subject_entity_id: str | None = None,
    ) -> list[dict]:
        states = [s for s in trust_states if s in TRUST_STATES]
        if not states:
            return []
        if section is not None and section not in SECTIONS:
            raise OntologyError(f"section 不合法：{section}")
        with self._connect() as conn:
            query = _CLAIM_SELECT + " WHERE c.trust_state IN (%s)" % ",".join("?" for _ in states)
            params: list = list(states)
            if section:
                query += " AND c.section = ?"
                params.append(section)
            if subject_entity_id:
                query += " AND (c.subject_entity_id = ? OR c.object_entity_id = ?)"
                params.extend([subject_entity_id, subject_entity_id])
            if not include_hidden:
                query += " AND c.challenged = 0 AND (c.deferred_until IS NULL OR c.deferred_until <= ?)"
                params.append(utc_now())
            query += " ORDER BY c.promotion_ready DESC, c.last_reaffirmed DESC, c.created_at DESC LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(query, params).fetchall()
            return [self._claim(r, self._evidence_for(conn, r["id"])) for r in rows]  # type: ignore[misc]

    def inbox(self, *, limit: int = 20) -> list[dict]:
        """待确认的工作理解：working 且未被挑战、未被「先别存」推迟；最新在前。"""
        return self.list_claims(trust_states=("working",), limit=limit, include_hidden=False)

    def search_claims(
        self,
        query: str,
        *,
        k: int = 12,
        trust_states: tuple[str, ...] | list[str] = ("confirmed",),
        sections: tuple[str, ...] | list[str] | None = None,
        include_hidden: bool = False,
        min_score: float = 0.0,
    ) -> list[dict]:
        """词面相关性 + 时间衰减的轻量检索；query 为空时按最近重申排序。返回项带 ``score``。"""
        candidates = self.list_claims(trust_states=trust_states, limit=2000, include_hidden=include_hidden)
        if sections:
            allowed = set(sections)
            candidates = [c for c in candidates if c["section"] in allowed]
        q_tokens = tokenize(query)
        now = datetime.now(timezone.utc)
        scored: list[tuple[float, dict]] = []
        for claim in candidates:
            sim = lexical_similarity(q_tokens, tokenize(claim["content"])) if q_tokens else 0.0
            recency = 0.0
            seen = _parse_iso(claim.get("lastReaffirmed"))
            if seen is not None:
                days = max(0.0, (now - seen).total_seconds() / 86400.0)
                recency = 0.05 * math.exp(-days / 30.0)
            score = sim + recency
            if q_tokens and sim < min_score:
                continue
            item = dict(claim)
            item["score"] = round(score, 4)
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[: max(0, int(k))]]

    def find_similar_active(self, content: str, *, threshold: float = 0.9, section: str | None = None) -> dict | None:
        """近似重复（词面余弦 ≥ threshold）的活跃理解，供抽取去重。"""
        q_tokens = tokenize(content)
        if not q_tokens:
            return None
        for claim in self.list_claims(section=section, trust_states=("working", "confirmed"), limit=2000):
            if lexical_similarity(q_tokens, tokenize(claim["content"])) >= threshold:
                return claim
        return None

    # ------------------------------------------------------------------ 状态机
    def transition(
        self,
        claim_id: str,
        action: str,
        *,
        surface: str,
        conversation_id: str | None = None,
        message_id: str | None = None,
        edited_content: str | None = None,
        context_ref: str | None = None,
        note: str = "",
        actor: str = "user",
    ) -> dict:
        """信任状态机唯一入口。返回 ``{"claim": 变更后的理解, "replacedBy": 新理解或 None}``。"""
        if action not in REVIEW_ACTIONS or action == "create":
            raise OntologyError(f"action 不合法：{action}")
        if surface not in SURFACES:
            raise OntologyError(f"surface 不合法：{surface}")
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(_CLAIM_SELECT + " WHERE c.id = ?", (claim_id,)).fetchone()
                if row is None:
                    raise OntologyNotFoundError("理解不存在")
                state = row["trust_state"]
                before = {"trustState": state, "content": row["content"], "scope": row["scope"]}
                replaced_id: str | None = None

                def _require(*allowed: str) -> None:
                    if state not in allowed:
                        raise OntologyConflictError(
                            f"当前状态 {state} 不允许 {action}", current_state=state
                        )

                if action == "confirm":
                    _require("working")
                    conn.execute(
                        "UPDATE claims SET trust_state = 'confirmed', trust_origin = ?, last_reaffirmed = ?, "
                        "challenged = 0, challenge_note = NULL, deferred_until = NULL, updated_at = ? WHERE id = ?",
                        ("user_confirm" if row["trust_origin"] != "utterance" else "utterance", now, now, claim_id),
                    )
                elif action == "context_only":
                    _require("working")
                    ref = context_ref or conversation_id or row["context_ref"]
                    conn.execute(
                        "UPDATE claims SET trust_state = 'confirmed', trust_origin = 'user_confirm', "
                        "scope = 'context_only', context_ref = ?, last_reaffirmed = ?, challenged = 0, "
                        "deferred_until = NULL, updated_at = ? WHERE id = ?",
                        (ref, now, now, claim_id),
                    )
                elif action == "reject":
                    _require("working")
                    conn.execute(
                        "UPDATE claims SET trust_state = 'retracted', retracted_at = ?, "
                        "retraction_reason = 'user_rejected', updated_at = ? WHERE id = ?",
                        (now, now, claim_id),
                    )
                elif action == "defer":
                    _require("working")
                    until = (datetime.now(timezone.utc) + timedelta(days=DEFER_DAYS)).isoformat().replace(
                        "+00:00", "Z"
                    )
                    conn.execute(
                        "UPDATE claims SET deferred_until = ?, updated_at = ? WHERE id = ?",
                        (until, now, claim_id),
                    )
                elif action == "retract":
                    _require("confirmed")
                    conn.execute(
                        "UPDATE claims SET trust_state = 'retracted', retracted_at = ?, "
                        "retraction_reason = 'user_retracted', updated_at = ? WHERE id = ?",
                        (now, now, claim_id),
                    )
                elif action == "reaffirm":
                    _require("confirmed")
                    conn.execute(
                        "UPDATE claims SET last_reaffirmed = ?, updated_at = ? WHERE id = ?",
                        (now, now, claim_id),
                    )
                elif action == "partial":
                    _require("working", "confirmed")
                    edited = (edited_content or "").strip()
                    if not edited:
                        raise OntologyError("partial 需要提供修改后的内容")
                    if len(edited) > 120:
                        raise OntologyError("修改后的内容不能超过 120 字")
                    if normalize_text(edited) == normalize_text(row["content"]):
                        raise OntologyError("修改后的内容与原文相同，请直接确认")
                    replaced_id = f"clm_{uuid.uuid4().hex[:12]}"
                    digest = content_hash(row["subject_entity_id"], row["predicate"], edited)
                    try:
                        conn.execute(
                            """
                            INSERT INTO claims
                                (id, subject_entity_id, predicate, object_entity_id, content, section,
                                 self_model_layer, trust_state, trust_origin, confidence, scope, context_ref,
                                 privacy_level, export_allowed, valid_from, valid_to, first_seen, last_reaffirmed,
                                 supersedes_id, content_hash, device_scope, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', 'user_edit', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                replaced_id,
                                row["subject_entity_id"],
                                row["predicate"],
                                row["object_entity_id"],
                                edited,
                                row["section"],
                                row["self_model_layer"] if row["self_model_layer"] != "hypothesis" else "self_declared",
                                1.0,
                                row["scope"],
                                row["context_ref"],
                                row["privacy_level"],
                                row["export_allowed"],
                                row["valid_from"],
                                row["valid_to"],
                                row["first_seen"],
                                now,
                                claim_id,
                                digest,
                                row["device_scope"],
                                now,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise OntologyConflictError("修改后的内容与另一条活跃理解重复") from exc
                    # 旧证据复制到新理解，再补一条用户编辑证据；旧理解打 superseded 墓碑。
                    old_evidence = conn.execute(
                        "SELECT * FROM claim_evidence WHERE claim_id = ?", (claim_id,)
                    ).fetchall()
                    for ev in old_evidence:
                        conn.execute(
                            """
                            INSERT INTO claim_evidence
                                (id, claim_id, kind, stance, conversation_id, message_id, material_id, chunk_key,
                                 locator_json, decision_id, quote, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                f"ev_{uuid.uuid4().hex[:12]}",
                                replaced_id,
                                ev["kind"],
                                ev["stance"],
                                ev["conversation_id"],
                                ev["message_id"],
                                ev["material_id"],
                                ev["chunk_key"],
                                ev["locator_json"],
                                ev["decision_id"],
                                ev["quote"],
                                ev["created_at"],
                            ),
                        )
                    self._insert_evidence(
                        conn,
                        replaced_id,
                        [
                            {
                                "kind": "user_edit",
                                "conversation_id": conversation_id,
                                "message_id": message_id,
                                "quote": edited,
                            }
                        ],
                    )
                    conn.execute(
                        "UPDATE claims SET trust_state = 'superseded', superseded_by_id = ?, updated_at = ? WHERE id = ?",
                        (replaced_id, now, claim_id),
                    )
                else:  # pragma: no cover - 已在入口校验
                    raise OntologyError(f"action 不合法：{action}")

                after_row = conn.execute("SELECT trust_state, content, scope FROM claims WHERE id = ?", (claim_id,)).fetchone()
                after = {
                    "trustState": after_row["trust_state"],
                    "content": after_row["content"],
                    "scope": after_row["scope"],
                    "replacedBy": replaced_id,
                }
                self._insert_review_event(
                    conn,
                    target_type="claim",
                    target_id=claim_id,
                    action=action,
                    actor=actor,
                    surface=surface,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    before=before,
                    after=after,
                    note=note,
                )
                self._bump_revision(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return {
                "claim": self._fetch_claim(conn, claim_id),
                "replacedBy": self._fetch_claim(conn, replaced_id) if replaced_id else None,
            }

    def review_events(self, target_id: str | None = None, *, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            if target_id:
                rows = conn.execute(
                    "SELECT * FROM review_events WHERE target_id = ? ORDER BY created_at DESC LIMIT ?",
                    (target_id, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM review_events ORDER BY created_at DESC LIMIT ?", (int(limit),)
                ).fetchall()
        return [
            {
                "id": r["id"],
                "targetType": r["target_type"],
                "targetId": r["target_id"],
                "action": r["action"],
                "actor": r["actor"],
                "surface": r["surface"],
                "conversationId": r["conversation_id"],
                "messageId": r["message_id"],
                "before": _load(r["before_json"], None),
                "after": _load(r["after_json"], None),
                "note": r["note"],
                "createdAt": r["created_at"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ 统计
    def stats(self) -> dict:
        with self._connect() as conn:
            counts = {state: 0 for state in TRUST_STATES}
            for row in conn.execute("SELECT trust_state, COUNT(*) AS n FROM claims GROUP BY trust_state"):
                counts[row["trust_state"]] = int(row["n"])
            by_section = {s: {"confirmed": 0, "working": 0} for s in SECTIONS}
            for row in conn.execute(
                "SELECT section, trust_state, COUNT(*) AS n FROM claims "
                "WHERE trust_state IN ('confirmed','working') GROUP BY section, trust_state"
            ):
                by_section[row["section"]][row["trust_state"]] = int(row["n"])
            entities = conn.execute(
                "SELECT COUNT(*) AS n FROM entities WHERE status = 'active' AND type != 'me'"
            ).fetchone()["n"]
            now = utc_now()
            inbox = conn.execute(
                "SELECT COUNT(*) AS n FROM claims WHERE trust_state = 'working' AND challenged = 0 "
                "AND (deferred_until IS NULL OR deferred_until <= ?)",
                (now,),
            ).fetchone()["n"]
            revision = conn.execute("SELECT value FROM ontology_meta WHERE key = 'revision'").fetchone()
        return {
            "hasOntology": counts["confirmed"] > 0,
            "entities": int(entities),
            "claims": counts,
            "bySection": by_section,
            "inbox": int(inbox),
            "revision": int(revision["value"]) if revision else 0,
        }

    # ------------------------------------------------------------------ 后台任务
    def enqueue_job(
        self,
        kind: str,
        owner_id: str,
        *,
        payload: dict | None = None,
        priority: int = 0,
        input_hash: str = "",
    ) -> str | None:
        """入队一个后台任务；同 kind+owner 已有活跃任务时返回 None（幂等）。"""
        if kind not in JOB_KINDS:
            raise OntologyError(f"任务类型不合法：{kind}")
        job_id = f"ojob_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO ontology_jobs
                        (job_id, kind, owner_id, state, priority, attempts, input_hash, payload_json, created_at, updated_at)
                    VALUES (?, ?, ?, 'queued', ?, 0, ?, ?, ?, ?)
                    """,
                    (job_id, kind, owner_id, int(priority), input_hash or "", _json(payload or {}), now, now),
                )
            except sqlite3.IntegrityError:
                return None
        return job_id

    def claim_next_job(self, owner: str, lease_seconds: float = 120.0) -> dict | None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM ontology_jobs WHERE state = 'queued' ORDER BY priority DESC, created_at LIMIT 1"
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    "UPDATE ontology_jobs SET state = 'running', lease_owner = ?, lease_until = ?, "
                    "attempts = attempts + 1, updated_at = ? WHERE job_id = ?",
                    (owner, now + lease_seconds, now, row["job_id"]),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            row = conn.execute("SELECT * FROM ontology_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
            return self._job(row)

    @staticmethod
    def _job(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "jobId": row["job_id"],
            "kind": row["kind"],
            "ownerId": row["owner_id"],
            "state": row["state"],
            "priority": int(row["priority"]),
            "attempts": int(row["attempts"]),
            "leaseOwner": row["lease_owner"],
            "leaseUntil": row["lease_until"],
            "failureClass": row["failure_class"],
            "errorCode": row["error_code"],
            "errorDetail": row["error_detail"],
            "inputHash": row["input_hash"],
            "payload": _load(row["payload_json"], {}),
            "result": _load(row["result_json"], None),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "finishedAt": row["finished_at"],
        }

    def get_job(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            return self._job(conn.execute("SELECT * FROM ontology_jobs WHERE job_id = ?", (job_id,)).fetchone())

    def heartbeat_job(self, job_id: str, owner: str, lease_seconds: float = 120.0) -> bool:
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE ontology_jobs SET lease_until = ?, updated_at = ? WHERE job_id = ? AND lease_owner = ? AND state = 'running'",
                (now + lease_seconds, now, job_id, owner),
            )
            return cur.rowcount > 0

    def finish_job(self, job_id: str, owner: str, *, result: dict | None = None) -> bool:
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE ontology_jobs SET state = 'done', result_json = ?, updated_at = ?, finished_at = ?, lease_owner = NULL, lease_until = NULL "
                "WHERE job_id = ? AND lease_owner = ? AND state = 'running'",
                (_json(result) if result is not None else None, now, now, job_id, owner),
            )
            return cur.rowcount > 0

    def fail_job(
        self,
        job_id: str,
        owner: str,
        *,
        failure_class: str,
        error_code: str,
        error_detail: str = "",
        retry: bool = False,
        max_attempts: int = 3,
    ) -> bool:
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT attempts FROM ontology_jobs WHERE job_id = ? AND lease_owner = ?", (job_id, owner)).fetchone()
            if row is None:
                return False
            state = "queued" if retry and int(row["attempts"]) < max_attempts else "failed"
            cur = conn.execute(
                "UPDATE ontology_jobs SET state = ?, failure_class = ?, error_code = ?, error_detail = ?, "
                "updated_at = ?, finished_at = ?, lease_owner = NULL, lease_until = NULL "
                "WHERE job_id = ? AND lease_owner = ? AND state = 'running'",
                (state, failure_class, error_code, (error_detail or "")[:500], now, now if state == "failed" else None, job_id, owner),
            )
            return cur.rowcount > 0

    def recover_expired_jobs(self) -> int:
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE ontology_jobs SET state = 'queued', lease_owner = NULL, lease_until = NULL, updated_at = ? "
                "WHERE state = 'running' AND (lease_until IS NULL OR lease_until < ?)",
                (now, now),
            )
            return int(cur.rowcount)

    def pending_jobs(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM ontology_jobs WHERE state IN ('queued','running')").fetchone()
        return int(row["n"]) if row else 0


    # ------------------------------------------------------------------ P3：整合器用的系统级变更
    def set_challenged(self, claim_id: str, note: str, *, challenged: bool = True) -> dict | None:
        """整合器标记「被后续理解矛盾」；只影响 working 理解是否进入上下文与 inbox。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE claims SET challenged = ?, challenge_note = ?, updated_at = ? WHERE id = ? AND trust_state = 'working'",
                (1 if challenged else 0, (note or "")[:300] if challenged else None, utc_now(), claim_id),
            )
            return self._fetch_claim(conn, claim_id)

    def set_export_allowed(self, claim_id: str, allowed: bool, *, surface: str = "ontology_page") -> dict | None:
        """用户逐条决定哪些已确认理解可以带走给其他 Agent；敏感 / 受限的即使打开也不会出包。"""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT export_allowed, content FROM claims WHERE id = ?", (claim_id,)).fetchone()
                if row is None:
                    raise OntologyNotFoundError("理解不存在")
                conn.execute("UPDATE claims SET export_allowed = ?, updated_at = ? WHERE id = ?", (1 if allowed else 0, utc_now(), claim_id))
                self._insert_review_event(
                    conn, target_type="claim", target_id=claim_id, action="edit", surface=surface,
                    before={"exportAllowed": bool(row["export_allowed"])}, after={"exportAllowed": bool(allowed)}, note="导出开关",
                )
                self._bump_revision(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return self._fetch_claim(conn, claim_id)

    def set_promotion_ready(self, claim_id: str, ready: bool = True) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE claims SET promotion_ready = ?, updated_at = ? WHERE id = ?", (1 if ready else 0, utc_now(), claim_id))

    def system_retract(self, claim_id: str, reason: str, *, note: str = "") -> dict | None:
        """唯一允许的自动状态变化：只对 working 理解生效（挑战超期、证据被删）。"""
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT trust_state, content FROM claims WHERE id = ?", (claim_id,)).fetchone()
                if row is None or row["trust_state"] != "working":
                    conn.execute("ROLLBACK")
                    return None
                conn.execute(
                    "UPDATE claims SET trust_state = 'retracted', retracted_at = ?, retraction_reason = ?, updated_at = ? WHERE id = ?",
                    (now, reason, now, claim_id),
                )
                self._insert_review_event(
                    conn, target_type="claim", target_id=claim_id, action="retract", actor="system", surface="system",
                    before={"trustState": "working", "content": row["content"]}, after={"trustState": "retracted", "reason": reason}, note=note,
                )
                self._bump_revision(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return self._fetch_claim(conn, claim_id)

    def system_defer(self, claim_id: str, until: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE claims SET deferred_until = ?, updated_at = ? WHERE id = ? AND trust_state = 'working'",
                (until, utc_now(), claim_id),
            )

    def evidence_source_count(self, claim_id: str) -> int:
        """独立来源数：不同会话算不同来源，资料按 material_id 算，用户编辑算一个。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT kind, conversation_id, material_id, decision_id FROM claim_evidence WHERE claim_id = ?", (claim_id,)).fetchall()
        sources = set()
        for r in rows:
            if r["material_id"]:
                sources.add(f"m:{r['material_id']}")
            elif r["decision_id"]:
                sources.add(f"d:{r['decision_id']}")
            elif r["conversation_id"]:
                sources.add(f"c:{r['conversation_id']}")
            else:
                sources.add(f"k:{r['kind']}")
        return len(sources)

    # ---- 实体合并候选
    @staticmethod
    def _proposal(row: sqlite3.Row | None, from_name: str | None = None, into_name: str | None = None) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "fromEntityId": row["from_entity_id"],
            "intoEntityId": row["into_entity_id"],
            "fromName": from_name,
            "intoName": into_name,
            "reason": row["reason"],
            "score": float(row["score"]),
            "status": row["status"],
            "createdAt": row["created_at"],
            "resolvedAt": row["resolved_at"],
        }

    def create_merge_proposal(self, from_entity_id: str, into_entity_id: str, *, reason: str, score: float) -> dict | None:
        if from_entity_id == into_entity_id or ME_ENTITY_ID in (from_entity_id, into_entity_id):
            return None
        a, b = sorted([from_entity_id, into_entity_id])
        with self._lock, self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM entity_merge_proposals WHERE ((from_entity_id = ? AND into_entity_id = ?) OR (from_entity_id = ? AND into_entity_id = ?)) "
                "AND status != 'accepted' AND julianday(created_at) > julianday('now') - 30",
                (a, b, b, a),
            ).fetchone():
                return None
            pid = f"mrg_{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO entity_merge_proposals (id, from_entity_id, into_entity_id, reason, score, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (pid, from_entity_id, into_entity_id, reason[:200], float(score), utc_now()),
            )
        # 事务提交后再读（另一个连接看不到未提交的行）。
        return self.get_merge_proposal(pid)

    def get_merge_proposal(self, proposal_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT p.*, f.canonical_name AS from_name, i.canonical_name AS into_name FROM entity_merge_proposals p "
                "LEFT JOIN entities f ON f.id = p.from_entity_id LEFT JOIN entities i ON i.id = p.into_entity_id WHERE p.id = ?",
                (proposal_id,),
            ).fetchone()
            return self._proposal(row, row["from_name"], row["into_name"]) if row else None

    def list_merge_proposals(self, *, status: str = "pending", limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT p.*, f.canonical_name AS from_name, i.canonical_name AS into_name FROM entity_merge_proposals p "
                "LEFT JOIN entities f ON f.id = p.from_entity_id LEFT JOIN entities i ON i.id = p.into_entity_id "
                "WHERE p.status = ? ORDER BY p.created_at DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
            return [self._proposal(r, r["from_name"], r["into_name"]) for r in rows]  # type: ignore[misc]

    def resolve_merge_proposal(self, proposal_id: str, *, accept: bool, surface: str = "ontology_page") -> dict:
        """接受：from 实体并入 into（别名迁移、理解主宾改指、from 标 merged）。拒绝：只改状态。"""
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM entity_merge_proposals WHERE id = ?", (proposal_id,)).fetchone()
                if row is None:
                    raise OntologyNotFoundError("合并候选不存在")
                if row["status"] != "pending":
                    raise OntologyConflictError("合并候选已处理")
                if accept:
                    src, dst = row["from_entity_id"], row["into_entity_id"]
                    aliases = conn.execute("SELECT alias, alias_norm FROM entity_aliases WHERE entity_id = ?", (src,)).fetchall()
                    for al in aliases:
                        conn.execute(
                            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, alias_norm, source, created_at) VALUES (?, ?, ?, 'merge', ?)",
                            (dst, al["alias"], al["alias_norm"], now),
                        )
                    conn.execute("UPDATE claims SET subject_entity_id = ?, updated_at = ? WHERE subject_entity_id = ?", (dst, now, src))
                    conn.execute("UPDATE claims SET object_entity_id = ?, updated_at = ? WHERE object_entity_id = ?", (dst, now, src))
                    conn.execute("UPDATE entities SET status = 'merged', merged_into_id = ?, updated_at = ? WHERE id = ?", (dst, now, src))
                    self._insert_review_event(conn, target_type="entity", target_id=src, action="merge", surface=surface, after={"into": dst})
                conn.execute(
                    "UPDATE entity_merge_proposals SET status = ?, resolved_at = ? WHERE id = ?",
                    ("accepted" if accept else "rejected", now, proposal_id),
                )
                self._bump_revision(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_merge_proposal(proposal_id)  # type: ignore[return-value]

    # ---- 理解矛盾对
    def create_conflict(self, claim_a_id: str, claim_b_id: str, *, kind: str = "contradiction", verdict_by: str = "model", note: str = "") -> dict | None:
        if claim_a_id == claim_b_id or kind not in ("contradiction", "tension"):
            return None
        a, b = sorted([claim_a_id, claim_b_id])
        with self._lock, self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM claim_conflicts WHERE claim_a_id = ? AND claim_b_id = ? AND (status = 'pending' OR julianday(created_at) > julianday('now') - 30)",
                (a, b),
            ).fetchone():
                return None
            cid = f"cfl_{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO claim_conflicts (id, claim_a_id, claim_b_id, kind, verdict_by, note, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (cid, a, b, kind, verdict_by, (note or "")[:300], utc_now()),
            )
        return self.get_conflict(cid)

    def _conflict(self, conn: sqlite3.Connection, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "claimA": self._fetch_claim(conn, row["claim_a_id"], with_evidence=False),
            "claimB": self._fetch_claim(conn, row["claim_b_id"], with_evidence=False),
            "verdictBy": row["verdict_by"],
            "note": row["note"],
            "status": row["status"],
            "resolution": row["resolution"],
            "createdAt": row["created_at"],
            "resolvedAt": row["resolved_at"],
        }

    def get_conflict(self, conflict_id: str) -> dict | None:
        with self._connect() as conn:
            return self._conflict(conn, conn.execute("SELECT * FROM claim_conflicts WHERE id = ?", (conflict_id,)).fetchone())

    def list_conflicts(self, *, status: str = "pending", limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM claim_conflicts WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, int(limit))).fetchall()
            return [self._conflict(conn, r) for r in rows]  # type: ignore[misc]

    def resolve_conflict(self, conflict_id: str, *, keep: str, surface: str = "ontology_page") -> dict:
        """keep ∈ {a, b, both}：留 a 则 b 撤回（反之亦然）；both = 两条都对，标为已处理不再提示。"""
        if keep not in ("a", "b", "both"):
            raise OntologyError("keep 只能是 a / b / both")
        conflict = self.get_conflict(conflict_id)
        if conflict is None:
            raise OntologyNotFoundError("矛盾对不存在")
        if conflict["status"] != "pending":
            raise OntologyConflictError("矛盾对已处理")
        if keep in ("a", "b"):
            loser = conflict["claimB"] if keep == "a" else conflict["claimA"]
            if loser and loser["trustState"] in ("working", "confirmed"):
                action = "reject" if loser["trustState"] == "working" else "retract"
                self.transition(loser["id"], action, surface=surface, note=f"矛盾裁决：保留另一条（{conflict_id}）")
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE claim_conflicts SET status = 'resolved', resolution = ?, resolved_at = ? WHERE id = ?",
                (keep, utc_now(), conflict_id),
            )
        return self.get_conflict(conflict_id)  # type: ignore[return-value]

    # ---- 资料脱钩 / 导出 / 全量删除
    def detach_material(self, material_id: str) -> dict:
        """资料被永久删除：删除其证据；只靠该资料支撑的 working 理解撤回，confirmed 理解保留但记事件。"""
        with self._connect() as conn:
            claim_ids = [r["claim_id"] for r in conn.execute("SELECT DISTINCT claim_id FROM claim_evidence WHERE material_id = ?", (material_id,)).fetchall()]
        retracted, kept = [], []
        for claim_id in claim_ids:
            with self._connect() as conn:
                others = conn.execute(
                    "SELECT COUNT(*) AS n FROM claim_evidence WHERE claim_id = ? AND (material_id IS NULL OR material_id != ?)",
                    (claim_id, material_id),
                ).fetchone()["n"]
                state = conn.execute("SELECT trust_state FROM claims WHERE id = ?", (claim_id,)).fetchone()["trust_state"]
            if others == 0 and state == "working":
                self.system_retract(claim_id, "evidence_purged", note=f"资料 {material_id} 已删除")
                retracted.append(claim_id)
            else:
                kept.append(claim_id)
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM claim_evidence WHERE material_id = ?", (material_id,))
            self._bump_revision(conn)
        return {"materialId": material_id, "retracted": retracted, "kept": kept}

    def export_payload(self, *, sections: tuple[str, ...] | None = None, include_working: bool = False) -> dict:
        states = ("confirmed", "working") if include_working else ("confirmed",)
        claims = self.list_claims(trust_states=states, limit=5000)
        if sections:
            allowed = set(sections)
            claims = [c for c in claims if c["section"] in allowed]
        return {
            "exportedAt": utc_now(),
            "schemaVersion": self.meta_get("schema_version", "1"),
            "entities": self.list_entities(limit=5000),
            "claims": claims,
            "reviewEvents": self.review_events(limit=5000),
        }

    def purge_all(self) -> dict:
        """全量删除本体（不可恢复）：实体 / 理解 / 证据 / 复核事件 / 候选 / 任务；保留「我」实体。"""
        with self._lock, self._connect() as conn:
            counts = {
                "claims": conn.execute("SELECT COUNT(*) AS n FROM claims").fetchone()["n"],
                "entities": conn.execute("SELECT COUNT(*) AS n FROM entities WHERE type != 'me'").fetchone()["n"],
            }
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table in ("claim_conflicts", "entity_merge_proposals", "claim_evidence", "review_events", "claims", "ontology_jobs"):
                    conn.execute(f"DELETE FROM {table}")
                conn.execute("DELETE FROM entity_aliases WHERE entity_id != ?", (ME_ENTITY_ID,))
                conn.execute("DELETE FROM entities WHERE id != ?", (ME_ENTITY_ID,))
                conn.execute("INSERT INTO ontology_meta (key, value) VALUES ('purged_at', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (utc_now(),))
                self._bump_revision(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {"purged": True, **{k: int(v) for k, v in counts.items()}}


def reset_for_tests(db_path: str | Path | None = None) -> OntologyStore:
    """测试专用：替换进程内单例，指向独立库文件。"""
    with OntologyStore._instance_lock:
        OntologyStore._instance = OntologyStore(db_path)
        return OntologyStore._instance
