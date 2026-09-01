"""Versioned vector index for MindOS knowledge cards."""
from __future__ import annotations

import logging
import hashlib
from typing import Iterable

from config import TEXT_MODEL_ID
import index_registry
from embedder import embed_query
from vector_store import (ensure_index_readable, ensure_index_writable,
                          get_or_create_collection, get_union_collection_records,
                          query_union_collection, record_index_operation_failure,
                          routed_operation)
from .stores import card_ledger_store

logger = logging.getLogger(__name__)


class CardIndexError(RuntimeError):
    """Classified card-index failure exposed to the outbox consumer."""

    def __init__(self, code: str, transient: bool):
        super().__init__(code)
        self.code = code
        self.transient = transient


def _is_transient_index_error(exc: Exception) -> bool:
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _index_error_code(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "index_timeout"
    if isinstance(exc, ConnectionError):
        return "index_connection_error"
    if isinstance(exc, OSError):
        return "index_storage_error"
    if isinstance(exc, RuntimeError):
        return "index_write_failed"
    return "index_unexpected_error"

# v1 IDs were not versioned and cannot coexist during a safe replacement.
COLLECTION_NAME = "mindos_knowledge_cards_v2"
_CHUNK_SIZE = 900
_CHUNK_OVERLAP = 120


def _collection():
    return get_or_create_collection(COLLECTION_NAME)


def _chunks(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    result: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + _CHUNK_SIZE)
        if end < len(text):
            boundary = text.rfind("\n", start + _CHUNK_SIZE // 2, end)
            if boundary > start:
                end = boundary
        part = text[start:end].strip()
        if part:
            result.append(part)
        if end >= len(text):
            break
        start = max(end - _CHUNK_OVERLAP, start + 1)
    return result


def _id(knowledge_id: str, vector_version: int, index: int) -> str:
    return f"{knowledge_id}::v{vector_version}::{index}"


def expected_chunk_count(body: str) -> int:
    return len(_chunks(body))


def _ids_hash(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def remove_card(knowledge_id: str, reason: str = "delete") -> None:
    """Logically hide a card; never mutate active base HNSW in place."""
    try:
        card_ledger_store.mark_visibility(knowledge_id, "purged", reason)
    except Exception as exc:
        logger.warning("知识卡片向量 tombstone 写入失败 %s: %s", knowledge_id, type(exc).__name__)


def count_card_chunks(knowledge_id: str) -> int:
    """Count only chunks belonging to the current active ledger version."""
    version = card_ledger_store.active_vector_version(knowledge_id)
    if version is None:
        return 0
    try:
        ensure_index_readable()
        records = get_union_collection_records(COLLECTION_NAME, include=["metadatas"])
        return sum(1 for meta in records.get("metadatas") or []
                   if isinstance(meta, dict)
                   and str(meta.get("knowledge_id") or "") == knowledge_id
                   and int(meta.get("vector_version") or -1) == version)
    except Exception as exc:
        record_index_operation_failure(exc, "knowledge_card_count")
        return 0


def index_card(knowledge_id: str, title: str, body: str, tags: Iterable[str] = (), *,
               content_revision: str, rel_path: str, folder_id: int | None = None,
               raise_transient: bool = False, activate: bool = True,
               vector_version: int | None = None) -> bool:
    """Write, verify and activate the desired version without deleting old data."""
    # Only the confirmation outbox is allowed to establish a card revision.
    # A consumer must never turn a draft into an indexable card by calling ensure().
    state = card_ledger_store.get(knowledge_id)
    is_pending_update = not activate and vector_version is not None and card_ledger_store.pending_update_can_index(
        knowledge_id, content_revision, vector_version,
    )
    if not is_pending_update and not card_ledger_store.can_index(state, content_revision):
        return False
    version = int(vector_version if vector_version is not None else state.get("desired_vector_version") or 1)
    body_chunks = _chunks(body)
    if not body_chunks:
        return True
    try:
        documents = [f"{title}\n{part}" for part in body_chunks]
        # Generate all vectors before touching Chroma, retaining prior active data on failure.
        embeddings = [embed_query(document) for document in documents]
        if not all(embeddings):
            raise RuntimeError("嵌入模型未返回有效向量")
        ensure_index_writable()
        safe_tags = ",".join(str(tag).strip() for tag in tags if str(tag).strip())[:500]
        metadatas = [{
            "knowledge_id": knowledge_id, "title": title[:500], "chunk_index": index,
            "vector_version": version, "content_revision": content_revision,
            # card_revision is the read-side contract. Keep content_revision for
            # compatibility with chunks written before the field was introduced.
            "card_revision": content_revision,
            "folder_id": int(folder_id or 0), "source_type": "knowledge",
            "tags": safe_tags, "visibility_at_write": "active",
        } for index in range(len(documents))]
        ids = [_id(knowledge_id, version, i) for i in range(len(documents))]
        # Keep route selection, upsert and verification in one tracked operation.
        # Stable versioned IDs make a replay after interruption idempotent.
        with routed_operation():
            col = _collection()
            col.upsert(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)
            written = col.get(ids=ids, include=["metadatas"])
            written_ids = list(written.get("ids") or [])
            written_meta = list(written.get("metadatas") or [])
            if set(written_ids) != set(ids) or len(written_meta) != len(ids):
                raise RuntimeError("知识卡片向量写入校验失败")
            for meta in written_meta:
                if (not isinstance(meta, dict)
                        or str(meta.get("card_revision") or meta.get("content_revision") or "") != content_revision
                        or int(meta.get("vector_version") or -1) != version):
                    raise RuntimeError("知识卡片向量 metadata 校验失败")
        routing = index_registry.get_routing() or index_registry.ensure_registry()
        card_ledger_store.record_vector_manifest(
            knowledge_id, version, content_revision, len(ids), _ids_hash(ids),
            body_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            embedding_model_id=TEXT_MODEL_ID,
            embedding_dimension=len(embeddings[0]) if embeddings else None,
            routing_epoch=int(routing.get("routing_epoch") or 0),
        )
        return True if not activate else card_ledger_store.activate_vector(knowledge_id, version)
    except Exception as exc:
        record_index_operation_failure(exc, "knowledge_card_write")
        logger.warning("知识卡片向量写入失败 %s: %s", knowledge_id, type(exc).__name__)
        code = _index_error_code(exc)
        if raise_transient:
            raise CardIndexError(code, _is_transient_index_error(exc)) from exc
        card_ledger_store.mark_vector_failed(knowledge_id, code)
        return False


def verify_card_vector(
    knowledge_id: str, vector_version: int, content_revision: str, *, allow_manifest_backfill: bool = True,
) -> bool:
    """Verify one ledger version against the currently routed base+delta union."""
    try:
        ensure_index_readable()
        records = get_union_collection_records(COLLECTION_NAME, include=["metadatas", "documents"])
    except Exception as exc:
        record_index_operation_failure(exc, "knowledge_card_verify")
        return False
    return _verify_card_vector_records(
        records, knowledge_id, vector_version, content_revision,
        allow_manifest_backfill=allow_manifest_backfill,
    )


def _verify_card_vector_records(
    records: dict, knowledge_id: str, vector_version: int, content_revision: str, *,
    allow_manifest_backfill: bool = True,
) -> bool:
    """Verify one card against an already loaded routed collection snapshot."""
    found: list[tuple[str, dict]] = []
    for item_id, meta in zip(records.get("ids") or [], records.get("metadatas") or []):
        if not isinstance(meta, dict):
            continue
        try:
            version = int(meta.get("vector_version"))
        except (TypeError, ValueError):
            continue
        revision = str(meta.get("card_revision") or meta.get("content_revision") or "")
        if str(meta.get("knowledge_id") or "") == knowledge_id and version == int(vector_version):
            if revision != content_revision:
                return False
            found.append((str(item_id), meta))
    if not found:
        return False
    manifest = card_ledger_store.get_vector_manifest(knowledge_id, vector_version)
    indices: list[int] = []
    for _, meta in found:
        try:
            indices.append(int(meta.get("chunk_index")))
        except (TypeError, ValueError):
            return False
    if sorted(indices) != list(range(len(found))):
        return False
    ids = [item_id for item_id, _ in found]
    if manifest:
        return bool(
            manifest.get("state") == "verified"
            and str(manifest.get("content_revision") or "") == content_revision
            and int(manifest.get("expected_chunk_count") or -1) == len(found)
            and str(manifest.get("chunk_ids_hash") or "") == _ids_hash(ids)
        )
    if allow_manifest_backfill:
        routing = index_registry.get_routing() or index_registry.ensure_registry()
        card_ledger_store.record_vector_manifest(
            knowledge_id, vector_version, content_revision, len(found), _ids_hash(ids),
            routing_epoch=int(routing.get("routing_epoch") or 0),
        )
        return True
    return False


def verify_card_vectors(cards: Iterable[tuple[str, int, str]]) -> dict[str, bool]:
    """Verify many active cards from one base+delta snapshot.

    Startup reconciliation used to load the complete knowledge collection once
    per card.  Loading it once also gives every card in this audit the same
    routing snapshot, which avoids mixed results while the collection grows.
    """
    requested = [(str(card_id), int(version), str(revision))
                 for card_id, version, revision in cards]
    if not requested:
        return {}
    try:
        ensure_index_readable()
        records = get_union_collection_records(COLLECTION_NAME, include=["metadatas", "documents"])
    except Exception as exc:
        record_index_operation_failure(exc, "knowledge_cards_verify")
        return {card_id: False for card_id, _, _ in requested}
    return {
        card_id: _verify_card_vector_records(records, card_id, version, revision)
        for card_id, version, revision in requested
    }


def search_cards(query: str, limit: int = 20, device_scope: str = "global") -> list[dict]:
    """Query base+delta and expose only active ledger versions.

    阶段 2：按 device_scope 过滤账本准入——只召回当前设备/账号作用域内的卡片，
    跨设备向量命中一律丢弃（默认 global 作用域，保持既有调用与调试模式兼容）。
    """
    if not query.strip() or limit <= 0:
        return []
    try:
        ensure_index_readable()
        query_vector = embed_query(query)
        if not query_vector:
            return []
        result = query_union_collection(COLLECTION_NAME, query_vector, max(limit * 4, limit))
    except Exception as exc:
        record_index_operation_failure(exc, "knowledge_card_query")
        logger.debug("知识卡片向量检索不可用: %s", type(exc).__name__)
        return []
    best: dict[str, dict] = {}
    for i, _ in enumerate((result.get("ids") or [[]])[0]):
        meta = ((result.get("metadatas") or [[]])[0][i] or {})
        card_id = str(meta.get("knowledge_id") or "")
        try:
            version = int(meta.get("vector_version"))
        except (TypeError, ValueError):
            continue
        revision = str(meta.get("card_revision") or meta.get("content_revision") or "")
        try:
            active = card_ledger_store.is_rag_eligible(
                card_ledger_store.get(card_id, device_scope=device_scope), revision, version,
            )
        except Exception as exc:
            # Ledger is the visibility authority.  Never leak unfiltered
            # chunks, but preserve the caller's lexical-search degradation.
            logger.warning("知识卡片账本不可读，跳过语义召回: %s", type(exc).__name__)
            return []
        if not card_id or not active:
            continue
        document = str(((result.get("documents") or [[]])[0][i] or "")).strip()
        title = str(meta.get("title") or "未命名知识卡片")
        snippet = document[len(title):].lstrip("\n") if document.startswith(title) else document
        distance = float(((result.get("distances") or [[]])[0][i] or 0.0))
        item = {"knowledgeId": card_id, "title": title, "snippet": snippet, "score": 1.0 - distance}
        if card_id not in best or item["score"] > best[card_id]["score"]:
            best[card_id] = item
    return sorted(best.values(), key=lambda item: item["score"], reverse=True)[:limit]
