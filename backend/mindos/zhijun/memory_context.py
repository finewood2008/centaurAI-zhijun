"""Small, explicit conversation controls, never a model intent-classification call."""
import re
from datetime import datetime, timezone
from .memory_retrieval import is_followup, is_self_overview


_SWITCH = re.compile(r"换个话题|换一(?:个)?话题|不聊.*章程|不谈.*章程|先不.*章程|聊(?:点|聊)?别的|新话题|另外问|不说这个|先不聊这个")
_QUESTION = re.compile(r"[？?]|怎么|如何|什么|帮我|请问|会不会|能不能|是否|要不要")
_RETROSPECTIVE = re.compile(r"回顾|以前|过去|当时|曾经|那时候|历史|原来|这些年|这几年")
_COURTESY = re.compile(r"^(?:你好|谢谢|好的|好|嗯|收到|再见)[。！!\s]*$")


def build_focus(content, allowed_history):
    """Rebuild a small working focus from permitted text; never persist a profile.

    An assistant question is only a slot cue, not a user fact. The caller must
    exclude denied history before invoking this helper. Indexes refer to that
    supplied history, so all derived focus text retains those source dependencies.
    """
    current = str(content or "").strip()[:1000]
    window = [(index, m) for index, m in list(enumerate(allowed_history))[-6:]
              if m.get("status", "complete") == "complete"]
    changed = bool(_SWITCH.search(current))
    # A prior explicit topic switch is a boundary even for a later short answer.
    for position in reversed(range(len(window))):
        _, message = window[position]
        if message.get("role") == "user" and _SWITCH.search(str(message.get("content") or "")):
            window = window[position:]
            break
    question, question_index = "", None
    for index, message in reversed(window):
        if message.get("role") != "assistant":
            continue
        text = str(message.get("content") or "")[-640:]
        questions = re.findall(r"[^。！!？?\n]{2,320}[？?]", text)
        if questions:
            question, question_index = questions[-1].strip(), index
        break
    slot_answer = bool(question and current and len(current) <= 80
                       and not _QUESTION.search(current) and not _COURTESY.fullmatch(current))
    previous = next(((i, m) for i, m in reversed(window) if m.get("role") == "assistant"), None)
    provenance = (previous[1].get("meta") or {}).get("routingProvenance") or {} if previous else {}
    saved = (provenance.get("contextPlan") or {}).get("focus") or {}
    from .memory_retrieval import _tokens
    same_event = bool(saved.get("userStatements") and len(_tokens(current) & _tokens(saved.get("topic", ""))) >= 2)
    confirmation = bool(re.match(r"^(?:对|是的|就是|没错)[，,。\s]*(?:就是|我|这个|这件|担心)", current))
    fragment = bool(saved.get("userStatements") and len(current) <= 80 and not _QUESTION.search(current)
                    and re.match(r"^(?:只是|自然是|主要是|还是|还没|没有|并没|已经|不是|而是)", current))
    continuation = not changed and (is_followup(current) or slot_answer or confirmation or fragment or same_event)
    hints, used = [], []
    if continuation:
        for index, message in reversed(window):
            if message.get("role") != "user":
                continue
            text = str(message.get("content") or "").strip()[:500]
            if not text or _COURTESY.fullmatch(text):
                continue
            hints.insert(0, text)
            used.insert(0, index)
            # Short slot answers keep walking back to the event they answer.
            if len(hints) >= 3 or (not is_followup(text) and
                    (len(text) >= 12 or re.search(r"我在|我想|考虑|项目|这次|最近|工作|家庭|章程", text))):
                break
        if question_index is not None:
            used.append(question_index)
    else:
        question = ""
    # A working event is persisted only in this conversation's reply metadata.
    # It holds original user statements, not an AI-authored personality summary.
    # The previous reply remains a required source for all carried statements.
    records = []
    if continuation and saved.get("userStatements") and previous:
        records.extend({**r, "messageId": r.get("messageId") or (previous[1].get("meta") or {}).get("replyTo")}
                       for r in saved["userStatements"])
        used.append(previous[0])
    for index in sorted(set(used)):
        message = allowed_history[index]
        if message.get("role") == "user":
            records.append({"messageId": message.get("id"), "text": message.get("content", "")})
    records = list({r.get("messageId") or r["text"]: r for r in records if r.get("text")}.values())
    total, retained = 0, []
    for record in reversed(records):
        if total + len(record["text"]) <= 1800 and len(retained) < 8:
            retained.insert(0, record)
            total += len(record["text"])
    event = "\n".join(r["text"] for r in retained) if continuation else ""
    current_record = {"messageId": None, "text": str(content or "").strip()}
    statements = [*retained, current_record] if continuation else [current_record]
    query = "\n".join(x for x in (current, event, question) if x)[:2322]
    past_year = any(int(year) < datetime.now(timezone.utc).year for year in re.findall(r"(?<!\d)((?:19|20)\d{2})年", current))
    mode = "retrospective" if _RETROSPECTIVE.search(current) or past_year else "continuation" if continuation else "current"
    return {"query": query, "current": current, "event": event, "topic": event or current,
            "question": question, "continuation": continuation, "topicChanged": changed,
            "historyUsed": sorted(set(used)), "mode": mode, "userStatements": statements,
            "omittedConditions": len(records) - len(retained)}


def _charter_context(text):
    return "章程" in text and bool(re.search(
        r"结合|参考.*(?:经历|本体|情况)|根据.*(?:经历|本体|情况)|对照.*(?:经历|本体)|"
        r"(?:适合|符合|矛盾|冲突).*我|我的(?:经历|本体|实际情况|工作|生活)", text))


def conversation_intent(content, allowed_history, task=""):
    text = content.strip()
    if _SWITCH.search(text):
        return "conversation"
    if "章程" in text:
        return "charter_context" if _charter_context(text) else "charter"
    if is_self_overview(text):
        return "self_overview"
    followup = build_focus(text, allowed_history)["continuation"]
    if not followup and re.search(r"[？?]|怎么|如何|什么|帮我|请问", text):
        return "conversation"
    # A topic switch remains in force on following short turns. Only completed,
    # permitted history is supplied by routing; cutoff history is never scanned.
    previous = [m["content"] for m in allowed_history if m.get("role") == "user"]
    for prior in reversed(previous[-4:]):
        if re.search(r"换个话题|不聊.*章程|不谈.*章程|聊点别的", prior):
            return "conversation"
        if "章程" in prior:
            if followup:
                return "charter_context" if _charter_context(prior) else "charter"
            break
        if not is_followup(prior) and re.search(r"[？?]|怎么|如何|什么|帮我|请问", prior):
            return "conversation"
        if len(prior) > 35:
            break
    # A stale charter task marker cannot absorb a self-contained new statement.
    return "charter" if task == "charter" and (followup or not text) else "conversation"
