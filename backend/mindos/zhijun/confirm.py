"""对话内一键确认：状态机转移 + 追加系统备注消息，让下一轮模型看到「用户刚确认 / 否认了什么」。"""
from __future__ import annotations

import logging

from ..stores.conversation_store import ConversationStore
from ..stores.ontology_store import OntologyStore
from . import jobs

logger = logging.getLogger(__name__)

_NOTE_TEMPLATES = {
    "confirm": "你确认了：{content}",
    "partial": "你把这条改成了：{content}",
    "context_only": "你说这只适用于这次：{content}",
    "reject": "你否认了：{content}",
    "defer": "你先不保存：{content}",
    "retract": "你撤回了：{content}",
    "reaffirm": "你重申了：{content}",
}


def review_claim(
    claim_id: str,
    *,
    action: str,
    surface: str,
    edited_content: str | None = None,
    context_ref: str | None = None,
    note: str = "",
    conversation_id: str | None = None,
    message_id: str | None = None,
    store: OntologyStore | None = None,
    conv_store: ConversationStore | None = None,
) -> dict:
    store = store or OntologyStore.instance()
    conv_store = conv_store or ConversationStore.instance()
    result = store.transition(
        claim_id,
        action,
        surface=surface,
        conversation_id=conversation_id,
        message_id=message_id,
        edited_content=edited_content,
        context_ref=context_ref,
        note=note,
    )
    shown = result["replacedBy"] or result["claim"]
    if conversation_id:
        template = _NOTE_TEMPLATES.get(action)
        if template and conv_store.get_conversation(conversation_id) is not None:
            try:
                conv_store.append_message(
                    conversation_id,
                    "system",
                    template.format(content=shown["content"]),
                    meta={
                        "kind": "review",
                        "claimId": claim_id,
                        "action": action,
                        "replacedBy": result["replacedBy"]["id"] if result["replacedBy"] else None,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - 备注失败不影响状态机结果
                logger.debug("追加系统备注失败：%s", type(exc).__name__)
    try:
        jobs.enqueue_projection(store=store)
    except Exception:  # noqa: BLE001
        pass
    return result
