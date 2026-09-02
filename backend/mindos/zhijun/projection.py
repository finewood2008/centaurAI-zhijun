"""把已确认本体投影为人类可读的 Markdown。

- ``ZHIJUN_PROFILE.md``：完整的已确认视图（留在设备内）。
- ``USER.md``：可导出子集（confirmed ∧ export_allowed ∧ privacy ∈ {public, private}），
  让旧的 ``/api/memory/context`` 与 MCP ``memory_get_user_profile`` 零改动地变成「只读已确认本体」。
  只有存在至少一条已确认理解时才覆盖 USER.md，避免把用户手写的画像清空。
投影是输出，不是事实源；本体的权威永远在 ontology.db。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import MEMORY_DIR

from ..stores.ontology_store import LAYER_TITLES, SECTION_TITLES, SECTIONS, OntologyStore

logger = logging.getLogger(__name__)

PROFILE_FILE = "ZHIJUN_PROFILE.md"
USER_FILE = "USER.md"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _line(claim: dict) -> str:
    layer = LAYER_TITLES.get(claim["layer"], claim["layer"])
    obj = f"（涉及：{claim['objectName']}）" if claim.get("objectName") else ""
    scope = "（只适用于当时那件事）" if claim.get("scope") == "context_only" else ""
    return f"- {claim['content']}{obj} — {layer}{scope}"


def render(store: OntologyStore) -> tuple[str, str]:
    """返回 (完整视图, 可导出子集)。"""
    claims = store.list_claims(trust_states=("confirmed",), limit=2000)
    by_section: dict[str, list[dict]] = {s: [] for s in SECTIONS}
    for claim in claims:
        by_section.setdefault(claim["section"], []).append(claim)

    stamp = _now()
    full = [f"# 知君对我的认识（已确认）", f"", f"> 生成于 {stamp}；共 {len(claims)} 条已确认理解。只有我确认过的内容才会出现在这里。", ""]
    export = [f"# 用户画像（由知君本体投影，仅含允许导出的已确认理解）", f"", f"> 生成于 {stamp}。", ""]
    exported = 0
    for section in SECTIONS:
        items = by_section.get(section) or []
        if not items:
            continue
        full.append(f"## {SECTION_TITLES[section]}")
        full.extend(_line(c) for c in items)
        full.append("")
        exportable = [c for c in items if c.get("exportAllowed") and c.get("privacyLevel") in ("public", "private")]
        if exportable:
            export.append(f"## {SECTION_TITLES[section]}")
            export.extend(_line(c) for c in exportable)
            export.append("")
            exported += len(exportable)
    if not claims:
        full.append("（还没有已确认的理解。先去和知君聊几句。）")
    if exported == 0:
        export.append("（没有允许导出的已确认理解。）")
    return "\n".join(full).rstrip() + "\n", "\n".join(export).rstrip() + "\n"


def _write(rel_path: str, content: str) -> None:
    try:
        import memory_store  # 旧记忆层：写文件 + （可选）向量化；这里跳过向量化

        memory_store.write_memory_file(rel_path, content, source_agent="zhijun-projection", skip_index=True)
        return
    except Exception as exc:  # noqa: BLE001 - 旧层不可用时直接落盘
        logger.debug("memory_store 不可用，直接写文件：%s", type(exc).__name__)
    target = Path(MEMORY_DIR) / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def write_projection(store: OntologyStore | None = None) -> dict:
    store = store or OntologyStore.instance()
    full, export = render(store)
    _write(PROFILE_FILE, full)
    has_confirmed = store.stats()["claims"]["confirmed"] > 0
    if has_confirmed:
        _write(USER_FILE, export)
    store.meta_set("last_projection_at", _now())
    return {"profile": PROFILE_FILE, "user": USER_FILE if has_confirmed else None, "generatedAt": _now()}


def projection_payload(store: OntologyStore | None = None) -> dict:
    store = store or OntologyStore.instance()
    full, export = render(store)
    return {"markdown": full, "exportableMarkdown": export, "generatedAt": _now()}
