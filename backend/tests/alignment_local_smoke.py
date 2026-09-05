"""Opt-in loopback-only Ollama smoke; synthetic data in an ephemeral DB."""
import json
import os
import tempfile
import uuid

root = tempfile.TemporaryDirectory(prefix="zhijun-alignment-local-")
os.environ["CENTAURAI_DATABASE_DATA_ROOT"] = root.name
os.environ["ZHIJUN_MATERIAL_EVIDENCE"] = "0"
os.environ["ZHIJUN_EXTRACTION"] = "0"

from mindos.stores.ontology_store import OntologyStore
from mindos.stores.conversation_store import ConversationStore
from mindos.stores.alignment_store import AlignmentStore
from mindos.zhijun import alignment
from mindos.zhijun.turn import run_turn
from mindos.zhijun.provider import OllamaProvider

onto, convs = OntologyStore.instance(), ConversationStore.instance()
conv = convs.create_conversation(title="隔离合成案例，不是真实用户")
texts = ["我真正认同自主选择，希望自主决定产品方向。", "即使慢一些，我仍然希望自主决定产品方向，这对我很重要。", "这不是一次临时选择，过去几个项目里，我都宁可慢一点也要自己决定产品方向。"]
messages = [convs.append_message(conv["id"], "user", text) for text in texts]
reply = convs.append_message(conv["id"], "assistant", "可以校准自主选择对你有多重要。")
claim = onto.create_claim({"subject_entity_id": "ent_me", "content": "我认同自主决定产品方向", "section": "principles", "layer": "self_declared"},
    [{"kind": "conversation_turn", "conversation_id": conv["id"], "message_id": m["id"], "quote": m["content"]} for m in messages],
    trust_state="confirmed", trust_origin="user_confirm")
local = OllamaProvider("http://127.0.0.1:11434", "qwen3.5:9b", timeout=180, keep_alive="10m", num_ctx=4096)
print(json.dumps({"phase": "proposing", "model": local.model}), flush=True)
result = alignment.propose(claim["id"], conversation_id=conv["id"], message_id=reply["id"], ontology=onto, conversations=convs, provider=local)
print(json.dumps({"phase": "proposed", "state": result["state"], "reason": result.get("reason")}, ensure_ascii=False), flush=True)
assert result["state"] == "ready", result
claim = result["claim"]
a = claim["selfAlignment"]
assert a["level"] is None
assert a["proposal"]["evidenceIds"]
AlignmentStore(onto).review(claim["id"], {"requestId": str(uuid.uuid4()), "expectedRevision": a["revision"], "claimVersion": a["claimVersion"],
    "evidenceVersion": a["evidenceVersion"], "action": "calibrate", "level": 4, "framing": "long_term", "note": "合成案例的明确校准"})
events = list(run_turn(conv["id"], "根据这个虚构案例，自主决定产品方向对我重要吗？请简短回答并区分记录与推测。", provider=local, ontology=onto, conv_store=convs))
assert not any(name == "error" for name, _ in events), events
provenance = next(data for name, data in events if name == "provenance")
assert provenance["alignmentSources"][0]["level"] == 4
answer = "".join(data["t"] for name, data in events if name == "token")
assert answer.strip()
print(json.dumps({"phase": "passed", "proposal": a["proposal"]["reason"], "answer": answer, "external": False}, ensure_ascii=False), flush=True)
