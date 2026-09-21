"""「心里的事」的准入：同一件事被提及两次以上，才成为候选。

PRD V2 6.1 的克制规则。理由是误判两个方向的代价不对称：漏记一条困扰只是少一条理解，
**错记一条会让用户下次不敢说**。一次抱怨不是长期困扰，一次疲惫也不是。

第一次提及只在本地记一个计数，不产生 claim；第二次才放行。计数存在 ontology_meta 的
一个 JSON 里——为这件事单开一张表不值得，而且这份计数是可以随时丢的派生数据：
丢了最多让某条困扰晚一轮被记住，不会损坏任何已确认的理解。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..stores.ontology_store import tokenize

META_KEY = "burden_mentions"
MIN_MENTIONS = 2          # 第一次只计数，第二次才成候选
RETAIN_DAYS = 180         # 困扰会过去：半年没再提起就不再占位子
MAX_ENTRIES = 200         # 上限，避免这份派生数据无限长
SIMILAR = 0.45            # 词面重合到这个程度算「同一件事」

_STOP = {"我", "的", "了", "是", "在", "和", "就", "都", "也", "还", "这", "那", "有", "很", "会", "着", "过"}


def _signature(content: str, object_name: str | None = None) -> set[str]:
    words = {w for w in tokenize(content or "") if w not in _STOP and len(w) > 1}
    if object_name:
        words.add(object_name.strip())
    return words


def _same_thing(left: set[str], right: set[str], *, left_object=None, right_object=None) -> bool:
    if not left or not right:
        return False
    # 指向同一个人 / 同一件具体的事，再加一个共同词就够：「和林岚那次谈话」换个说法
    # 仍然是同一件事，但词面重合可能很低。
    if left_object and right_object and left_object == right_object and (left & right):
        return True
    overlap = len(left & right) / len(left | right)
    return overlap >= SIMILAR


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(store) -> list[dict]:
    try:
        raw = store.meta_get(META_KEY)
        return json.loads(raw) if raw else []
    except Exception:  # noqa: BLE001 - 派生数据，读坏了就当没有
        return []


def _save(store, entries: list[dict]) -> None:
    try:
        store.meta_set(META_KEY, json.dumps(entries, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - 存不下只影响下一次能不能记住，不该中断对话
        pass


def _prune(entries: list[dict], now: datetime) -> list[dict]:
    cutoff = (now - timedelta(days=RETAIN_DAYS)).isoformat()
    alive = [e for e in entries if (e.get("lastSeen") or "") >= cutoff]
    alive.sort(key=lambda e: e.get("lastSeen") or "", reverse=True)
    return alive[:MAX_ENTRIES]


def record_and_count(store, content: str, *, object_name: str | None = None,
                     message_id: str | None = None, now: datetime | None = None) -> int:
    """记一次提及，返回包含这次在内的累计次数。

    同一条消息重复处理（任务重试）不会重复计数——否则一次重试就能把第一次提及
    变成「提过两次」，正好绕开这道闸。
    """
    moment = now or _now()
    signature = _signature(content, object_name)
    if not signature:
        return 0
    entries = _load(store)
    for entry in entries:
        stored = set(entry.get("tokens") or [])
        if _same_thing(signature, stored, left_object=object_name, right_object=entry.get("object")):
            if message_id and entry.get("lastMessageId") == message_id:
                return int(entry.get("count") or 1)      # 同一条消息，不重复计数
            entry["count"] = int(entry.get("count") or 0) + 1
            entry["lastSeen"] = moment.isoformat()
            entry["lastMessageId"] = message_id
            entry["tokens"] = sorted(stored | signature)
            _save(store, _prune(entries, moment))
            return int(entry["count"])
    entries.append({
        "tokens": sorted(signature), "object": object_name, "count": 1,
        "firstSeen": moment.isoformat(), "lastSeen": moment.isoformat(), "lastMessageId": message_id,
    })
    _save(store, _prune(entries, moment))
    return 1


def mention_count(store, content: str, *, object_name: str | None = None) -> int:
    """只读：这件事被提过几次。张力 B（说了没动）要用，不能有副作用。"""
    signature = _signature(content, object_name)
    if not signature:
        return 0
    for entry in _load(store):
        if _same_thing(signature, set(entry.get("tokens") or []),
                       left_object=object_name, right_object=entry.get("object")):
            return int(entry.get("count") or 0)
    return 0


def admitted(store, claim, *, message_id: str | None = None) -> bool:
    """这条 burdens 候选能不能放行。非 burdens 一律放行，由别的规则决定。"""
    if getattr(claim, "section", None) != "burdens":
        return True
    count = record_and_count(store, getattr(claim, "content", ""),
                             object_name=getattr(claim, "object", None) or None,
                             message_id=message_id)
    return count >= MIN_MENTIONS
