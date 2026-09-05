"""Disposable model doubles for browser checks. No live personal data/network."""
# ruff: noqa: E402
import os
import json
import tempfile
from pathlib import Path

root = tempfile.TemporaryDirectory(prefix="zhijun-routing-e2e-")
os.environ.update(CENTAURAI_DATABASE_DATA_ROOT=root.name, ZHIJUN_PROVIDER="",
                  ZHIJUN_MATERIAL_EVIDENCE="0", ZHIJUN_EXTRACTION="0")
from fastapi import FastAPI
import uvicorn
from mindos import ontology, conversations, zhijun_status, zhijun_onboarding, chat_import_routes, growth, chat_imports, routing_routes, memory_routes
from mindos.stores.ontology_store import OntologyStore
from mindos.stores.conversation_store import ConversationStore
from mindos.stores.alignment_store import AlignmentStore
from mindos.stores.memory_store import MemoryStore
from mindos.zhijun import memory
from mindos.zhijun import provider, decision_suggestions, deliberate
from tests.test_task_routing import Recording
from tests.test_decision_suggestions import SAMPLE

class FixtureRecording(Recording):
    def complete_json(self, req):
        if req.debug.get("task") == "charter_draft":
            self.requests.append(req)
            if self.error:
                raise self.error
            if req.debug.get("workspaceId"):
                clauses = []
                for record in json.loads(req.messages[0]["content"]):
                    original = record.get("sourceText") or record.get("原话") or ""
                    for index, line in enumerate(original.splitlines()):
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        kind = "aspiration" if "希望" in line else "principle" if "一直" in line else "preference"
                        clauses.append({"id": "fixture-clause-" + str(index), "section": "我在意的方向" if kind == "aspiration" else "怎样一起合作",
                            "text": line, "kind": kind, "scope": "global", "control": None, "sourceId": record["id"], "quote": line})
                return {"clauses": clauses[:6]}
            proposals = {}
            for m in json.loads(req.messages[0]["content"]):
                for field, needle in (("roles", "我是"), ("goals", "希望"), ("challengeStyle", "先听")):
                    if needle in m["原话"]:
                        proposals[field] = {"field": field, "text": m["原话"], "quote": m["原话"], "messageId": m["id"]}
            return {"proposals": list(proposals.values())}
        item = ((req.json_schema or {}).get("$defs") or {}).get("Candidate", {}).get("properties", {})
        if "text" in item:
            self.requests.append(req)
            if self.error:
                raise self.error
            return {"candidates": [{"text": "我更想先确定活动要达到什么效果，再决定怎么组织。"},
                                   {"text": "我更在意投入能不能承受，想先做一次小规模尝试。"},
                                   {"text": "我还不确定要不要办，想先判断这次活动有没有必要。"}]}
        return super().complete_json(req)

online, local = FixtureRecording(), FixtureRecording(False)
online.result = local.result = SAMPLE
provider.build_provider = lambda *a, **kw: online
routing_routes.build_provider = lambda: online
chat_imports.local_provider = lambda **kw: local
decision_suggestions.local_provider = lambda **kw: local
zhijun_onboarding.apply_action(zhijun_onboarding.OnboardingCommand(action="skip"))
onto, convs = OntologyStore.instance(), ConversationStore.instance()
conv = convs.create_conversation(title="双端协作测试（合成资料）")
convs.append_message(conv["id"], "assistant", "SECRET_OLD_PROFILE", meta={"localOnlyDerived": True})
AlignmentStore(onto).status(conv["id"], local_only=True, status="paused")
claim = onto.create_claim({"content": "星桥项目只是工作安排，不代表我的个人追求", "section": "matters", "layer": "self_declared"},
                         [{"kind": "user_edit", "quote": "星桥项目只是工作安排"}], trust_state="confirmed", trust_origin="user_created")
layout_conv = convs.create_conversation(title="对话阅读布局（合成案例）", mode="onboarding")
convs.append_message(layout_conv["id"], "user", "我在考虑星桥项目要不要扩张。预算有限，团队只有三个人，希望能照顾好现有客户，也想试试新的产品方向。")
reply = convs.append_message(layout_conv["id"], "assistant", "可以先区分两件事：你希望探索的方向，以及这次必须遵守的约束。预算和团队规模是当前条件，不必把工作安排当成长期追求。\n\n你现在最想弄清楚的是投入多少，还是先验证哪些需求？")
layout_draft = convs.upsert_draft(layout_conv["id"], {**deliberate.default_fields(), "title": "星桥项目是否扩张", "context": "预算有限，团队三人，需要兼顾现有客户与新方向。先明确范围、投入上限和可验证的目标，再作选择。", "options": ["保持现有产品", "小范围试点"], "keyQuestion": "如何探索新方向，同时保留可撤回的空间？"}, message_id=reply["id"])
assist_conv = convs.create_conversation(title="轻松回复演示（合成）")
convs.append_message(assist_conv["id"], "user", "我想组织一次小活动，但不知道从哪里开始。", meta={"routingSources": []})
convs.append_message(assist_conv["id"], "assistant", "可以先不用想完整方案。你现在最想先弄清楚哪一部分？", meta={"routingSources": []})
memory_conv = convs.create_conversation(title="低打扰记忆（合成测试）")
memory_user = convs.append_message(memory_conv["id"], "user", "我一直重视把复杂的事情向团队讲清楚，方便大家作决定。", meta={"routingSources": []})
convs.append_message(memory_conv["id"], "assistant", "这可能是你长期在意的合作方式，仍由你决定是否保留。", meta={"routingSources": []})
event_user = convs.append_message(memory_conv["id"], "user", "明天我去海湾活动看看；先了解大家的作品，不急着确定合作。", meta={"routingSources": []})
for i in range(4):
    convs.append_message(memory_conv["id"], "assistant", ("合成内容：本次活动的准备可以慢慢展开，事件细节仍留在这次对话中。\n\n" * 3) + f"第 {i + 1} 段。", meta={"routingSources": []})
ledger = MemoryStore(onto)
memory_topic = memory.topic_for(convs, memory_conv["id"])
memory_claims = []
for text in ("我重视把复杂的事情向团队讲清楚", "我重视让团队理解决策依据"):
    c = onto.create_claim({"content": text, "section": "principles", "layer": "self_declared", "scope": "long_term"},
                          [{"kind": "conversation_turn", "conversation_id": memory_conv["id"], "message_id": memory_user["id"], "quote": memory_user["content"]}],
                          trust_state="working", trust_origin="model")
    ledger.register(c["id"], memory_conv["id"], memory_topic, memory_user["id"], False)
    memory_claims.append(c["id"])
ledger.merge_draft(memory_conv["id"], memory_topic, [{"content": "明天去海湾活动了解作品，暂不决定合作", "quote": event_user["content"], "messageId": event_user["id"], "sources": [], "privacyLevel": "private", "layer": "aspirational"}])
app = FastAPI()
for r in (ontology.router, conversations.router, zhijun_status.router, zhijun_onboarding.router, growth.router, chat_import_routes.build_router(lambda: None)):
    app.include_router(r)
from mindos.zhijun.charter import build_router as build_charter_router
app.include_router(build_charter_router())
app.include_router(memory_routes.build_router())

@app.get('/api/mindos/access-context')
def access():
    return {"mode": "local_debug", "localDebug": True}

@app.get('/api/health')
def health():
    return {"status": "ok", "version": "task-routing-fixture", "model": "synthetic"}

@app.get('/__fixture')
def info():
    return {"conversationId": conv["id"], "claimId": claim["id"],
            "layoutConversationId": layout_conv["id"], "layoutDraftId": layout_draft["id"],
            "assistConversationId": assist_conv["id"],
            "memoryConversationId": memory_conv["id"], "memoryClaimIds": memory_claims,
            "onlineRequests": [{"system": r.system, "messages": r.messages} for r in online.requests], "localRequests": len(local.requests),
            "localRequestPayloads": [{"system": r.system, "messages": r.messages} for r in local.requests]}

@app.post('/__fixture/fail')
def fail():
    online.error = provider.ProviderError("合成在线超时", code="PROVIDER_TIMEOUT")
    return {"ok": True}

@app.post('/__fixture/recover')
def recover():
    online.error = None
    return {"ok": True}

# Serve the built UI against this isolated API for computer-use browser checks.
# The fixture never delegates API requests to the user's running application.
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
dist = Path(__file__).resolve().parents[2] / "frontend" / "mindos-web" / "dist"
app.mount('/mindos/assets', StaticFiles(directory=dist / 'assets'), name='fixture-assets')

@app.get('/mindos/{path:path}')
def ui(path: str):
    return FileResponse(dist / 'index.html')

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8772, log_level='warning')
