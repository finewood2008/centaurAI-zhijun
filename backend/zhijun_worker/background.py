"""Persist user execution provenance before a domain task can consume capabilities."""
from contextlib import contextmanager
import json
import os
import re

from .capabilities import execution, require, CapabilityError


class BackgroundEnqueueError(CapabilityError):
    """A durable failed task, not a failed chat or permission to run it."""
    def __init__(self, job_id, cause):
        code = getattr(cause, 'code', '')
        code = code if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', code) else 'BACKGROUND_ENQUEUE_FAILED'
        status = getattr(cause, 'status', 503)
        status = status if isinstance(status, int) and 400 <= status <= 599 else 503
        self.job_id = job_id
        super().__init__(code, status)


def _store():
    from mindos.stores.ontology_store import OntologyStore
    store = OntologyStore.instance()
    with store._lock, store._connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS workspace_background_origins (id TEXT PRIMARY KEY, origin_json TEXT NOT NULL)")
    return store


def register(ident, purpose):
    if not os.environ.get("ZHIJUN_WORKSPACE_ID"):
        return
    origin = execution.get()
    if not origin or not origin.get("requestId") or not origin.get("operationId"):
        raise CapabilityError("BACKGROUND_ORIGIN_REQUIRED", 403)
    require().call("domain.background.register", {"id": ident, "purpose": purpose})
    store = _store()
    with store._lock, store._connect() as db:
        db.execute("INSERT OR REPLACE INTO workspace_background_origins VALUES(?,?)", (ident, json.dumps(origin)))


@contextmanager
def activated(ident):
    if not os.environ.get("ZHIJUN_WORKSPACE_ID"):
        yield
        return
    store = _store()
    with store._connect() as db:
        row = db.execute("SELECT origin_json FROM workspace_background_origins WHERE id=?", (ident,)).fetchone()
    if not row:
        raise CapabilityError("BACKGROUND_ORIGIN_REQUIRED", 403)
    token = execution.set(json.loads(row[0]))
    try:
        yield
    finally:
        execution.reset(token)


def finish(ident):
    if not os.environ.get("ZHIJUN_WORKSPACE_ID"):
        return
    require().call("domain.background.finish", {"id": ident})
    store = _store()
    with store._lock, store._connect() as db:
        db.execute("DELETE FROM workspace_background_origins WHERE id=?", (ident,))
