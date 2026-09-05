"""Isolated loopback UI fixture. Never opens the user's application databases."""
import os
import tempfile

fixture_root = tempfile.TemporaryDirectory(prefix="zhijun-alignment-e2e-")
os.environ["CENTAURAI_DATABASE_DATA_ROOT"] = fixture_root.name
os.environ["ZHIJUN_PROVIDER"] = "fake"
os.environ["ZHIJUN_MATERIAL_EVIDENCE"] = "0"
os.environ["ZHIJUN_EXTRACTION"] = "0"

from fastapi import FastAPI
import uvicorn
from mindos import ontology, conversations, zhijun_status, zhijun_onboarding, chat_import_routes, alignment_routes
from mindos.stores.ontology_store import OntologyStore
from mindos.stores.conversation_store import ConversationStore
from mindos.stores.alignment_store import AlignmentStore
from mindos.zhijun.provider import FakeProvider

onto, convs = OntologyStore.instance(), ConversationStore.instance()
store = AlignmentStore(onto)
conv = convs.create_conversation(title="自我贴合度验收（隔离合成数据）")
user = convs.append_message(conv["id"], "user", "我负责星桥项目，但这是公司的工作安排。")
user2 = convs.append_message(conv["id"], "user", "我仍然负责星桥项目，不过我更想自己选择方向。")
assistant = convs.append_message(conv["id"], "assistant", "这件事确实发生了，但不一定代表你的核心意愿。可以一起校准这条理解。", provider="fixture", model="synthetic")

assigned = onto.create_claim({"subject_entity_id": "ent_me", "section": "matters", "layer": "self_declared", "content": "我负责星桥项目，这是工作安排"}, [
    {"kind": "conversation_turn", "conversation_id": conv["id"], "message_id": m["id"], "quote": m["content"]} for m in (user, user2)
], trust_state="confirmed", trust_origin="user_confirm")
a = assigned["selfAlignment"]
store.propose(assigned["id"], expected_revision=0, version=a["claimVersion"], level=2, framing="long_term",
    reason="你两次提到项目，但也说这是工作安排。它有多能代表你？", evidence_ids=[e["id"] for e in assigned["evidence"]],
    conversation_id=conv["id"], message_id=assistant["id"], evidence_digest=a["evidenceVersion"])

cases = [
    ("我认同自主选择产品方向", "principles", "self_declared", "long_term", 4),
    ("我希望成为更耐心的倾听者", "direction", "aspirational", "aspirational", 4),
    ("我这次为了交付接受加班", "ways", "self_declared", "context_only", 4),
    ("我在合成案例里养一只猫", "who", "self_declared", "long_term", None),
]
for i, (text, section, layer, framing, level) in enumerate(cases):
    c = onto.create_claim({"subject_entity_id": "ent_me", "content": text, "section": section, "layer": layer,
                           "scope": "context_only" if framing == "context_only" else "long_term"},
                          [{"kind": "user_edit", "quote": text}], trust_state="confirmed", trust_origin="user_created")
    if level is not None:
        a = c["selfAlignment"]
        store.review(c["id"], {"requestId": f"fixture-calibrate-{i}", "expectedRevision": 0, "claimVersion": a["claimVersion"],
                               "evidenceVersion": a["evidenceVersion"], "action": "calibrate", "level": level, "framing": framing})

class ExternalFixture(FakeProvider):
    name = "fixture-external"
    model = "synthetic-only"
    external = True
    _base_url = "https://fixture.invalid/v1"

alignment_routes.build_provider = lambda: ExternalFixture()
app = FastAPI()
for router in (ontology.router, conversations.router, zhijun_status.router, zhijun_onboarding.router, chat_import_routes.build_router(lambda: None)):
    app.include_router(router)

@app.get("/api/mindos/access-context")
def access():
    return {"mode": "local_debug", "localDebug": True}

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "isolated-alignment-fixture", "model": "synthetic"}

@app.get("/__fixture")
def fixture():
    return {"conversationId": conv["id"], "claimId": assigned["id"], "root": fixture_root.name}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8769, log_level="warning")
