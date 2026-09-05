"""Conversation proposals in the existing growth database; never ontology writes."""
import json
import re
import uuid

from .alignment_store import digest
from .growth_store import GrowthStore, GrowthConflictError, utc_now

FIELDS = {"vision": "我想成为", "roles": "当前角色", "principles": "长期原则", "goals": "阶段目标",
          "challengeStyle": "希望怎样帮助我", "boundaries": "不交给 AI 决定", "quietDomains": "暂不主动触碰"}
TEXT_FIELDS = {"vision", "challengeStyle"}
CONTROLS = {"memory_manual", "no_proactive", "local_only", "confirm_decisions"}
KINDS = {"principle", "aspiration", "preference", "boundary"}


def unique_sources(refs):
    return list({digest(ref): ref for ref in refs}.values())


def validate_clauses(clauses, *, limit=80):
    """Validate the published shape; model/client cannot invent executable controls."""
    if not isinstance(clauses, list) or len(clauses) > limit:
        raise ValueError(f"章程最多 {limit} 条")
    result, seen = [], set()
    for raw in clauses:
        if not isinstance(raw, dict):
            raise ValueError("条款格式不正确")
        item = dict(raw)
        for key, limit in (("id", 100), ("section", 100), ("text", 2000)):
            if not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > limit:
                raise ValueError("每条章程需要标识、章节和不超过 2000 字的正文")
            item[key] = item[key].strip()
        if item["id"] in seen:
            raise ValueError("条款标识不能重复")
        seen.add(item["id"])
        if item.get("kind") not in KINDS or item.get("scope") not in ("global", "contextual"):
            raise ValueError("条款类型或适用范围不正确")
        if item.get("control") is not None and item["control"] not in CONTROLS:
            raise ValueError("不支持的可执行约定")
        if item.get("scope") == "contextual" and not str(item.get("context") or "").strip():
            raise ValueError("情境条款需要说明适用情境")
        if len(str(item.get("context") or "")) > 1000 or len(str(item.get("quote") or "")) > 4000:
            raise ValueError("条款情境或原话过长")
        if len(str(item.get("clarification") or "")) > 1000:
            raise ValueError("待澄清说明过长")
        if not isinstance(item.get("sources", []), list):
            raise ValueError("条款来源格式不正确")
        item["sources"] = unique_sources(item.get("sources", []))
        result.append(item)
    return result


def render_document(clauses):
    """Only selected clauses contribute text. No hidden/unselected draft prose."""
    sections = {}
    for c in clauses:
        text = c["text"]
        if c.get("scope") == "contextual":
            text += "（适用情境：" + c["context"] + "）"
        sections.setdefault(c["section"], []).append(text)
    return "\n\n".join("## " + section + "\n\n" + "\n\n".join(texts) for section, texts in sections.items())


def _section_contexts(document):
    """Structural Markdown context only; no guess at cancellation or intent."""
    result, headings, path, lines, fenced = {}, [], (), [], False
    for line in document.splitlines():
        if re.match(r"^\s*(?:```|~~~)", line):
            fenced = not fenced
        heading = None if fenced else re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            result[path] = result.get(path, "") + "\n" + "\n".join(lines).strip()
            lines = []
            depth = len(heading.group(1))
            while headings and headings[-1][0] >= depth:
                headings.pop()
            headings.append((depth, heading.group(2).strip()[:100]))
            path = tuple(title for _, title in headings)
        lines.append(line)
    result[path] = result.get(path, "") + "\n" + "\n".join(lines).strip()
    return {key or ("人生章程",): (result.get((), "").strip(),
            *(result.get(key[:i], "").strip() for i in range(1, len(key) + 1))) for key in result}


def control_changes(clauses, current):
    """Removing a standing automatic limit requires a distinct user decision."""
    active = {(c["id"], c.get("control")) for c in clauses
              if c.get("scope") == "global" and not c.get("context")}
    return [{"id": c["id"], "text": c["text"], "control": c["control"]}
            for c in (current or {}).get("clauses", [])
            if c.get("control") in CONTROLS and c.get("scope") == "global" and not c.get("context")
            and (c["id"], c["control"]) not in active]


def retain_formal_controls(clauses, current):
    """Markdown cannot silently activate executable flags proposed by a model."""
    standing = {c["id"]: c for c in (current or {}).get("clauses", [])}
    for clause in clauses:
        previous = standing.get(clause["id"], {})
        if clause.get("control") and any(clause.get(key) != previous.get(key)
                for key in ("control", "text", "scope", "context")):
            clause["control"] = None
    return clauses


def derive_document_clauses(document, previous, sources, *, previous_document=None):
    """Build hidden compatibility clauses without treating prose as controls.

    The Markdown remains canonical. Exact, standalone old statements retain
    their metadata; edited/new prose is ordinary model guidance only.
    """
    old_by_text = {}
    old_contexts = _section_contexts(previous_document if previous_document is not None else render_document(previous or []))
    new_contexts = _section_contexts(document)
    for item in previous or []:
        old_by_text.setdefault(item["text"].strip(), []).append(item)
        if item.get("scope") == "contextual" and item.get("context"):
            old_by_text.setdefault(item["text"].strip() + "（适用情境：" + item["context"] + "）", []).append(item)
    result, section, pending, in_fence, used_ids = [], "人生章程", [], False, set()
    headings = []

    def add(raw):
        text = "\n".join(raw).strip()
        if not text:
            return
        match = None
        if not text.startswith(("#", ">", "```", "~~~")):
            plain = text
            plain = re.sub(r"^(?:[-*+] |\d+[.)] )", "", plain).strip()
            path = [title for _, title in headings] or [section]
            candidates = [c for c in old_by_text.get(plain, [])
                          if c["id"] not in used_ids and c.get("section") == section
                          and c.get("documentHeadingPath", [c.get("section")]) == path]
            match = candidates.pop(0) if candidates else None
        if match:
            item = json.loads(json.dumps(match))
            item["documentHeadingPath"] = [title for _, title in headings] or [section]
            path = tuple(item["documentHeadingPath"])
            if item.get("control") and old_contexts.get(path) != new_contexts.get(path):
                item["control"] = None
            result.append(item)
            used_ids.add(item["id"])
        else:
            for offset in range(0, len(text), 1800):
                chunk = text[offset:offset + 1800]
                if result and result[-1].get("documentDerived") and len(result[-1]["text"]) + len(chunk) + 2 <= 2000:
                    result[-1]["text"] += "\n\n" + chunk
                    result[-1]["quote"] = result[-1]["text"]
                    result[-1]["id"] = "md_" + digest([len(result), result[-1]["text"]])[:24]
                else:
                    result.append({"id": "md_" + digest([len(result), section, chunk])[:24],
                        "section": section, "text": chunk, "kind": "preference",
                        "scope": "global", "context": None, "control": None,
                        "sources": unique_sources(sources), "quote": chunk,
                        "origin": "manual", "clarification": None, "documentDerived": True})

    for line in document.splitlines():
        if re.match(r"^\s*(?:```|~~~)", line):
            in_fence = not in_fence
            pending.append(line)
            continue
        heading = None if in_fence else re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            add(pending); pending = []
            depth = len(heading.group(1))
            while headings and headings[-1][0] >= depth:
                headings.pop()
            section = heading.group(2).strip()[:100]
            headings.append((depth, section))
            add([line])
        elif not line.strip() and not in_fence:
            add(pending); pending = []
        elif not in_fence and re.match(r"^\s*(?:[-*+] |\d+[.)] )", line):
            add(pending); pending = []
            add([line])
        else:
            pending.append(line)
    add(pending)
    if not result and document.strip():
        result = [{"id": "md_" + digest(document)[:24], "section": "人生章程",
                   "text": document, "kind": "preference", "scope": "global",
                   "context": None, "control": None, "sources": unique_sources(sources),
                   "quote": document, "origin": "manual", "clarification": None}]
    return validate_clauses(result, limit=128)


def publication_document(workspace, current):
    document = workspace.get("document", "")
    if not isinstance(document, str) or not document.strip():
        raise ValueError("人生章程正文不能为空")
    sources = unique_sources([*workspace.get("sources", []),
        *(s for c in workspace.get("clauses", []) for s in c.get("sources", []))])
    clauses = retain_formal_controls(derive_document_clauses(document, workspace.get("clauses", []), sources,
                                                            previous_document=document), current)
    if not clauses:
        raise ValueError("人生章程正文不能为空")
    if any(str(c.get("clarification") or "").strip() for c in clauses):
        raise ValueError("正文仍有待澄清的约定，请明确修改对应文字后再发布")
    return document, clauses, sources


def legacy_clauses(charter):
    if not charter:
        return []
    if charter.get("clauses"):
        clauses = json.loads(json.dumps(charter["clauses"]))
        for clause in clauses:
            clause["sources"] = unique_sources([*clause.get("sources", []),
                {"kind": "charter_clause", "id": charter["id"] + ":" + clause["id"]}])
        return clauses
    result = []
    for field, label in FIELDS.items():
        values = [charter[field]] if field in TEXT_FIELDS else charter.get(field, [])
        for i, text in enumerate(values):
            if text:
                result.append({"id": f"legacy_{field}_{i}", "section": label, "text": text,
                    "kind": "aspiration" if field in ("vision", "goals") else "principle" if field == "principles" else "boundary" if field in ("boundaries", "quietDomains") else "preference",
                    "scope": "global", "sources": [{"kind": "charter", "id": charter["id"] + ":" + field}], "quote": text})
    return result


def publication_clauses(workspace, current, selected_ids):
    """The same final set is used by source preflight and atomic publication."""
    selected_ids = selected_ids or []
    by_id = {c["id"]: c for c in workspace["clauses"]}
    if len(selected_ids) != len(set(selected_ids)) or any(i not in by_id for i in selected_ids):
        raise ValueError("请选择有效条款")
    if any(str(by_id[i].get("clarification") or "").strip() for i in selected_ids):
        raise ValueError("选中的条款仍有待澄清内容，请明确适用含义后再发布")
    base_clauses = legacy_clauses(current)
    base_ids = {c["id"] for c in base_clauses}
    deleted = set(workspace.get("deletedClauseIds", []))
    clauses = [by_id[old["id"]] if old["id"] in selected_ids else old
               for old in base_clauses if old["id"] not in deleted]
    clauses.extend(c for c in workspace["clauses"] if c["id"] in selected_ids and c["id"] not in base_ids)
    if not clauses:
        raise ValueError("请选择至少一条有效条款；不会创建空的正式版本")
    return clauses


class CharterDraftStore:
    def __init__(self, growth=None):
        self.growth = growth or GrowthStore.instance()
        with self.growth._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS charter_drafts (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, scope TEXT NOT NULL,
                    context_revision TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS charter_drafts_conversation ON charter_drafts(conversation_id,created_at);
                CREATE TABLE IF NOT EXISTS charter_draft_actions (
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS charter_workspaces (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, scope TEXT NOT NULL,
                    status TEXT NOT NULL, body_json TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS charter_workspace_active
                    ON charter_workspaces(scope) WHERE status='active';
                CREATE TABLE IF NOT EXISTS charter_workspace_revisions (
                    workspace_id TEXT NOT NULL, revision INTEGER NOT NULL, body_json TEXT NOT NULL,
                    PRIMARY KEY(workspace_id,revision));
                CREATE TABLE IF NOT EXISTS charter_workspace_actions (
                    scope TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    response_json TEXT NOT NULL, PRIMARY KEY(scope,request_id));
            """)

    def get(self, ident):
        with self.growth._connect() as db:
            row = db.execute("SELECT body_json FROM charter_drafts WHERE id=?", (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, cid, scope):
        with self.growth._connect() as db:
            rows = db.execute("SELECT body_json FROM charter_drafts WHERE conversation_id=? AND scope=? ORDER BY created_at", (cid, scope)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save(self, draft):
        with self.growth._lock, self.growth._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id,body_json FROM charter_drafts WHERE conversation_id=? AND scope=?", (draft["conversationId"], draft["scope"])).fetchall():
                old = json.loads(row["body_json"])
                changed = False
                for field, entry in old["fields"].items():
                    if entry["status"] == "pending" and old["baseVersion"] == draft["baseVersion"]:
                        # Collect the still-unconfirmed items into one reviewable batch.
                        draft["fields"].setdefault(field, dict(entry))
                    if field in draft["fields"] and entry["status"] == "pending":
                        entry["status"] = "superseded"
                        changed = True
                if changed:
                    old["revision"] += 1
                    db.execute("UPDATE charter_drafts SET body_json=? WHERE id=?", (json.dumps(old, ensure_ascii=False), row["id"]))
            draft["sources"] = list({digest(s): s for e in draft["fields"].values() for s in e["sources"]}.values())
            db.execute("INSERT OR IGNORE INTO charter_drafts VALUES (?,?,?,?,?,?)", (draft["id"], draft["conversationId"], draft["scope"],
                draft["contextRevision"], json.dumps(draft, ensure_ascii=False), utc_now()))
        return self.get(draft["id"])

    def act(self, ident, *, scope, cid, revision, selections, skip, request_id, replacements=None):
        replacements = replacements or {}
        fingerprint = digest([ident, scope, cid, revision, selections, skip, replacements])
        with self.growth._lock, self.growth._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM charter_draft_actions WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise GrowthConflictError("重复请求的内容不同")
                return json.loads(old["response_json"])
            row = db.execute("SELECT body_json FROM charter_drafts WHERE id=? AND conversation_id=? AND scope=?", (ident, cid, scope)).fetchone()
            if not row:
                raise GrowthConflictError("草稿不存在或不属于当前会话")
            draft = json.loads(row[0])
            if draft["revision"] != revision:
                raise GrowthConflictError("草稿已更新，请重新核对；你的输入仍保留")
            current = self.growth._current_charter(db, scope)
            if selections and (current or {}).get("version", 0) != draft["baseVersion"]:
                raise GrowthConflictError("正式章程已更新，请重新整理草稿；旧提议不能覆盖新版本")
            if set(selections) & set(skip) or len(set(skip)) != len(skip):
                raise ValueError("同一项不能同时保存和跳过")
            payload = {f: (current or {}).get(f, "" if f in TEXT_FIELDS else []) for f in FIELDS}
            meta_fields = dict(((current or {}).get("metadata") or {}).get("fields") or {})
            for field in set(selections) | set(skip):
                entry = draft["fields"].get(field)
                if not entry or entry["status"] != "pending":
                    raise GrowthConflictError("这项内容已经处理或不可用")
                if field in skip:
                    entry["status"] = "skipped"
                    continue
                text = selections[field].strip()
                if not text or len(text) > (2000 if field == "vision" else 1000 if field == "challengeStyle" else 500):
                    raise ValueError("确认内容不能为空或超过长度限制")
                # A list proposal adds one user-approved statement, rather than replacing unrelated entries.
                replace = replacements.get(field)
                if replace and (field in TEXT_FIELDS or replace not in payload[field]):
                    raise GrowthConflictError("要替换的原内容已变化，请重新核对")
                payload[field] = text if field in TEXT_FIELDS else list(dict.fromkeys([*(v for v in payload[field] if v != replace), text]))
                previous_sources = meta_fields.get(field, {}).get("sources", [])
                meta_fields[field] = {"state": "confirmed", "sources": list({digest(r): r for r in [*previous_sources, *entry["sources"]]}.values()),
                    "origin": "dialogue", "quote": entry["quote"], "edited": text != entry["text"], "confirmedAt": utc_now()}
                entry.update(status="accepted", acceptedText=text)
            if selections:
                for f in FIELDS:
                    meta_fields.setdefault(f, {"state": "confirmed" if payload[f] else "pending", "sources": []})
                payload.update(expectedVersion=draft["baseVersion"], metadata={"scope": scope, "origin": "dialogue", "fields": meta_fields})
                current = self.growth._insert_charter(db, payload)
                draft["baseVersion"] = current["version"]
            draft["revision"] += 1
            db.execute("UPDATE charter_drafts SET body_json=? WHERE id=?", (json.dumps(draft, ensure_ascii=False), ident))
            response = {"draft": draft, "charter": current}
            db.execute("INSERT INTO charter_draft_actions VALUES (?,?,?)", (request_id, fingerprint, json.dumps(response, ensure_ascii=False)))
            return response

    # Guided full-document editing is deliberately separate from legacy field drafts.
    # Every mutation and publication shares the growth DB transaction/CAS boundary.
    def get_workspace(self, ident):
        with self.growth._connect() as db:
            row = db.execute("SELECT body_json FROM charter_workspaces WHERE id=?", (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_workspace_revision(self, ident, revision):
        with self.growth._connect() as db:
            row = db.execute("SELECT body_json FROM charter_workspace_revisions WHERE workspace_id=? AND revision=?", (ident, revision)).fetchone()
        return json.loads(row[0]) if row else None

    def latest_workspace(self, scope):
        with self.growth._connect() as db:
            row = db.execute("SELECT body_json FROM charter_workspaces WHERE scope=? AND status IN ('active','paused') ORDER BY (status='active') DESC,updated_at DESC LIMIT 1", (scope,)).fetchone()
        return json.loads(row[0]) if row else None

    def active_workspace(self, cid, scope):
        with self.growth._connect() as db:
            row = db.execute("SELECT body_json FROM charter_workspaces WHERE conversation_id=? AND scope=? AND status='active'", (cid, scope)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save_workspace(db, workspace):
        if workspace.get("documentFormat") != "markdown":
            workspace["document"] = render_document(workspace["clauses"])
        elif not isinstance(workspace.get("document"), str) or len(workspace["document"]) > 30000:
            raise ValueError("人生章程正文不能超过 30000 字")
        workspace["updatedAt"] = utc_now()
        body = json.dumps(workspace, ensure_ascii=False)
        db.execute("INSERT INTO charter_workspaces VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,body_json=excluded.body_json,updated_at=excluded.updated_at",
            (workspace["id"], workspace["conversationId"], workspace["scope"], workspace["status"], body, workspace["updatedAt"]))
        db.execute("INSERT INTO charter_workspace_revisions VALUES (?,?,?)", (workspace["id"], workspace["revision"], body))

    @staticmethod
    def _cached_action(db, scope, request_id, fingerprint):
        old = db.execute("SELECT * FROM charter_workspace_actions WHERE scope=? AND request_id=?", (scope, request_id)).fetchone()
        if old:
            if old["fingerprint"] != fingerprint:
                raise GrowthConflictError("重复请求的内容不同，请重新核对")
            return json.loads(old["response_json"])

    @staticmethod
    def _record_action(db, scope, request_id, fingerprint, response):
        db.execute("INSERT INTO charter_workspace_actions VALUES (?,?,?,?)", (scope, request_id, fingerprint, json.dumps(response, ensure_ascii=False)))
        return response

    @staticmethod
    def _workspace_for_write(db, ident, scope, cid, revision=None, active=True):
        row = db.execute("SELECT body_json FROM charter_workspaces WHERE id=? AND scope=? AND conversation_id=?", (ident, scope, cid)).fetchone()
        if not row:
            raise GrowthConflictError("工作稿不存在或不属于当前会话")
        workspace = json.loads(row[0])
        if active and workspace["status"] != "active":
            raise GrowthConflictError("工作稿已结束或暂停，请主动开始修改")
        if revision is not None and workspace["revision"] != revision:
            raise GrowthConflictError("工作稿已更新；你的输入仍保留，请核对后再保存")
        return workspace

    def start_workspace(self, cid, scope, request_id, *, start_seq=0):
        fingerprint = digest(["start", cid, scope])
        with self.growth._lock, self.growth._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cached = self._cached_action(db, scope, request_id, fingerprint)
            if cached:
                return cached
            row = db.execute("SELECT body_json FROM charter_workspaces WHERE scope=? AND status='active'", (scope,)).fetchone()
            if row:
                workspace = json.loads(row[0])
            else:
                current = self.growth._current_charter(db, scope)
                paused = db.execute("SELECT body_json FROM charter_workspaces WHERE scope=? AND conversation_id=? AND status='paused' ORDER BY updated_at DESC LIMIT 1", (scope, cid)).fetchone()
                workspace = json.loads(paused[0]) if paused else None
                if workspace and workspace["baseVersion"] == (current or {}).get("version", 0):
                    workspace.update(status="active", revision=workspace["revision"] + 1, generation=workspace["generation"] + 1)
                else:
                    clauses = legacy_clauses(current)
                    original = None
                    previous_workspace_id = ((current or {}).get("metadata") or {}).get("workspaceId")
                    if previous_workspace_id:
                        prior = db.execute("SELECT body_json FROM charter_workspaces WHERE id=? AND scope=?", (previous_workspace_id, scope)).fetchone()
                        original = json.loads(prior[0]) if prior else None
                    workspace = {"id": "charter_ws_" + uuid.uuid4().hex, "conversationId": cid, "scope": scope,
                        "status": "active", "revision": 1, "generation": 1, "manualRevision": 0,
                        "baseVersion": (current or {}).get("version", 0), "sourceText": (original or {}).get("sourceText", ""), "clauses": clauses,
                        "baseClauseIds": [c["id"] for c in clauses],
                        "sources": unique_sources([s for c in clauses for s in c.get("sources", [])]),
                        "suggestions": [], "generationRequests": [], "startSeq": start_seq,
                        "deletedClauseIds": [], "controlChanges": [],
                        "createdAt": utc_now(), "lastContextRevision": None}
                    if ((current or {}).get("metadata") or {}).get("documentFormat") == "markdown":
                        workspace.update(document=current["document"], documentFormat="markdown", manualRevision=1)
                        workspace["sources"] = unique_sources([*workspace["sources"],
                            {"kind": "charter_document", "id": current["id"]}])
                self._save_workspace(db, workspace)
            return self._record_action(db, scope, request_id, fingerprint,
                {"workspace": workspace, "conversationId": workspace["conversationId"]})

    def edit_workspace(self, ident, *, scope, cid, revision, request_id, source_text=None, clauses=None, document=None):
        if source_text is None and clauses is None and document is None:
            raise ValueError("请提供正文、原文或条款")
        if source_text is not None and (not isinstance(source_text, str) or len(source_text) > 30000):
            raise ValueError("原文不能超过 30000 字")
        if document is not None and (not isinstance(document, str) or len(document) > 30000):
            raise ValueError("人生章程正文不能超过 30000 字")
        normalized = validate_clauses(clauses) if clauses is not None and document is None else None
        fingerprint = digest(["edit", ident, cid, revision, source_text, normalized] +
                             (["markdown", document] if document is not None else []))
        with self.growth._lock, self.growth._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cached = self._cached_action(db, scope, request_id, fingerprint)
            if cached:
                return cached
            workspace = self._workspace_for_write(db, ident, scope, cid, revision)
            if workspace.get("documentFormat") == "markdown" and normalized is not None:
                raise GrowthConflictError("当前工作稿以完整正文为准；旧版条款编辑不能覆盖它")
            # All manual derivatives retain prior provenance, including removed clauses.
            prior = unique_sources([*workspace["sources"], *(s for c in workspace["clauses"] for s in c.get("sources", []))])
            if source_text is not None:
                workspace["sourceText"] = source_text
            if document is not None:
                previous_document = workspace.get("document", render_document(workspace["clauses"]))
                workspace["document"] = document
                workspace["documentFormat"] = "markdown"
                workspace["clauses"] = derive_document_clauses(document, workspace["clauses"], prior,
                                                               previous_document=previous_document)
                current = self.growth._current_charter(db, scope)
                retain_formal_controls(workspace["clauses"], current)
                base_ids = {c["id"] for c in legacy_clauses(current)}
                workspace["deletedClauseIds"] = sorted(base_ids - {c["id"] for c in workspace["clauses"]})
                workspace["controlChanges"] = control_changes(workspace["clauses"], current)
            if normalized is not None:
                for c in normalized:
                    # Client source IDs are not authority to grant/read other records.
                    c["sources"] = prior
                    c["origin"] = "manual"
                workspace["clauses"] = normalized
                current = self.growth._current_charter(db, scope)
                base_ids = {c["id"] for c in legacy_clauses(current)}
                workspace["deletedClauseIds"] = sorted(base_ids - {c["id"] for c in normalized})
            workspace.update(sources=prior, revision=revision + 1, manualRevision=workspace["manualRevision"] + 1)
            self._save_workspace(db, workspace)
            return self._record_action(db, scope, request_id, fingerprint, {"workspace": workspace})

    def apply_generated(self, ident, *, scope, cid, generation, source_revision, manual_revision,
                        base_version, clauses, sources, context_revision, request_id, service=None,
                        document=None):
        clauses = validate_clauses(clauses)
        fingerprint = digest(["generated", ident, cid, generation, source_revision, context_revision] +
                             (["markdown", document] if document is not None else []))
        with self.growth._lock, self.growth._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cached = self._cached_action(db, scope, request_id, fingerprint)
            if cached:
                return cached
            workspace = self._workspace_for_write(db, ident, scope, cid)
            current = self.growth._current_charter(db, scope)
            if workspace["generation"] != generation or workspace["baseVersion"] != base_version or (current or {}).get("version", 0) != base_version:
                raise GrowthConflictError("章程编辑已结束或正式版本已变化，旧整理结果不会覆盖新内容")
            actual_sources = unique_sources(sources)
            for clause in clauses:
                clause["sources"] = actual_sources
                clause["origin"] = "ai_draft"
            if workspace["manualRevision"] or workspace["revision"] != source_revision or workspace["manualRevision"] != manual_revision:
                # Concurrent typing is never overwritten; explicit merge remains available.
                merged = {c["id"]: c for c in workspace["clauses"]}
                merged.update({c["id"]: c for c in clauses})
                proposed_document = document if isinstance(document, str) and document.strip() else (
                    "\n\n".join(c["text"] if c.get("documentDerived") else render_document([c])
                               for c in merged.values()) if workspace.get("documentFormat") == "markdown"
                    else render_document(list(merged.values())))
                if len(proposed_document) > 30000:
                    raise ValueError("整理后的全文超过 30000 字；保留原正文，请先精简后再整理")
                workspace["suggestions"].append({"id": "charter_suggestion_" + digest([ident, request_id])[:24],
                    "clauses": clauses, "document": proposed_document,
                    "documentFormat": "markdown" if workspace.get("documentFormat") == "markdown" else "clauses",
                    "sources": actual_sources, "sourceRevision": source_revision,
                    "createdAt": utc_now(), "status": "pending", "service": service})
            else:
                # Incremental draft: omitted old clauses are retained, not silently deleted by AI.
                merged = {c["id"]: c for c in workspace["clauses"]}
                merged.update({c["id"]: c for c in clauses})
                workspace["clauses"] = list(merged.values())
            workspace["sources"] = unique_sources([*workspace["sources"], *actual_sources])
            workspace["revision"] += 1
            workspace["lastContextRevision"] = context_revision
            workspace["generationRequests"].append(request_id)
            self._save_workspace(db, workspace)
            return self._record_action(db, scope, request_id, fingerprint, {"workspace": workspace})

    def workspace_action(self, ident, *, scope, cid, revision, request_id, action, selected_ids=None, suggestion_id=None,
                         publish_document=False, confirm_control_changes=False):
        fingerprint = digest([action, ident, cid, revision, selected_ids, suggestion_id] +
                             (["markdown"] if publish_document else []) +
                             (["confirm_control_changes"] if confirm_control_changes else []))
        with self.growth._lock, self.growth._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cached = self._cached_action(db, scope, request_id, fingerprint)
            if cached:
                return cached
            workspace = self._workspace_for_write(db, ident, scope, cid, revision)
            charter = None
            if action == "pause":
                workspace.update(status="paused", generation=workspace["generation"] + 1)
            elif action == "merge":
                suggestion = next((s for s in workspace["suggestions"] if s["id"] == suggestion_id and s["status"] == "pending"), None)
                if not suggestion:
                    raise GrowthConflictError("待合并建议已处理或不存在")
                if workspace.get("documentFormat") == "markdown" and suggestion.get("documentFormat") != "markdown":
                    raise GrowthConflictError("这份建议来自旧版条款工作稿；当前正文保留，请重新整理成完整正文后再采用")
                merged = {c["id"]: c for c in workspace["clauses"]}
                merged.update({c["id"]: c for c in suggestion["clauses"]})
                if suggestion.get("documentFormat") == "markdown":
                    previous_document = workspace["document"]
                    workspace["document"] = suggestion["document"]
                    workspace["documentFormat"] = "markdown"
                    workspace["clauses"] = derive_document_clauses(
                        suggestion["document"], list(merged.values()),
                        unique_sources([*workspace["sources"], *suggestion["sources"]]), previous_document=previous_document)
                    current = self.growth._current_charter(db, scope)
                    retain_formal_controls(workspace["clauses"], current)
                    workspace["controlChanges"] = control_changes(workspace["clauses"], current)
                else:
                    workspace["clauses"] = list(merged.values())
                workspace["manualRevision"] += 1
                workspace["deletedClauseIds"] = [i for i in workspace.get("deletedClauseIds", []) if i not in merged]
                suggestion["status"] = "merged"
            elif action == "publish":
                current = self.growth._current_charter(db, scope)
                # Confirming a new/changed item is not consent to remove other
                # standing clauses. Only a manual draft deletion removes them.
                if publish_document:
                    if selected_ids:
                        raise ValueError("确认整篇正文时不能同时选择部分条款")
                    document, clauses, sources = publication_document(workspace, current)
                    changes = control_changes(clauses, current)
                    if changes and not confirm_control_changes:
                        raise ValueError("正文改变了原有自动执行约定，请确认规则变化后生效")
                else:
                    if workspace.get("documentFormat") == "markdown":
                        raise GrowthConflictError("当前工作稿以完整正文为准，请确认整篇章程")
                    clauses = publication_clauses(workspace, current, selected_ids)
                    document = render_document(clauses)
                    sources = unique_sources([s for c in clauses for s in c.get("sources", [])])
                charter = self.growth._insert_charter(db, {"document": document, "clauses": clauses,
                    "workspaceId": ident, "expectedVersion": workspace["baseVersion"],
                    "metadata": {"scope": scope, "origin": "workspace", "workspaceId": ident,
                        "workspaceRevision": revision, "documentFormat": "markdown" if publish_document else "clauses",
                        "sources": sources, "confirmedControlChanges": changes if publish_document else [],
                        "confirmedAt": utc_now()}})
                if publish_document:
                    workspace.update(document=document, documentFormat="markdown", clauses=clauses)
                workspace.update(status="published", publishedCharterId=charter["id"], generation=workspace["generation"] + 1)
            else:
                raise ValueError("不支持的工作稿操作")
            workspace["revision"] += 1
            self._save_workspace(db, workspace)
            response = {"workspace": workspace}
            if charter:
                response["charter"] = charter
            return self._record_action(db, scope, request_id, fingerprint, response)

    def cached_workspace_action(self, ident, *, scope, cid, revision, request_id, action, selected_ids=None, suggestion_id=None,
                                publish_document=False, confirm_control_changes=False):
        fingerprint = digest([action, ident, cid, revision, selected_ids, suggestion_id] +
                             (["markdown"] if publish_document else []) +
                             (["confirm_control_changes"] if confirm_control_changes else []))
        with self.growth._connect() as db:
            return self._cached_action(db, scope, request_id, fingerprint)
