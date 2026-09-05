"""Isolated browser fixture; ZHIJUN_TEST_REAL_LOCAL=1 opts into real loopback Ollama."""
import os
import tempfile

root = tempfile.TemporaryDirectory(prefix="zhijun-directions-e2e-")
os.environ["CENTAURAI_DATABASE_DATA_ROOT"] = root.name
os.environ["ZHIJUN_PROVIDER"] = "fake"
os.environ["ZHIJUN_MATERIAL_EVIDENCE"] = "0"
os.environ["ZHIJUN_EXTRACTION"] = "0"

from fastapi import FastAPI
import uvicorn
from mindos import ontology, conversations, zhijun_status, zhijun_onboarding, chat_import_routes, growth
from mindos.stores.conversation_store import ConversationStore
from mindos.zhijun import deliberate, decision_suggestions
from mindos.zhijun.provider import FakeProvider, OllamaProvider
from tests.test_decision_suggestions import SAMPLE

convs = ConversationStore.instance()
zhijun_onboarding.apply_action(zhijun_onboarding.OnboardingCommand(action="skip"))
conv = convs.create_conversation(title="候选方向测试（隔离合成数据）")
convs.append_message(conv["id"], "user", "我在考虑产品要不要扩张，可以继续做好现有产品，也可以先小范围试点，目前没有明确的预算。")
message = convs.append_message(conv["id"], "assistant", "可以先把几个方向放在一起比较，再由你决定。", provider="fixture", model="synthetic")
draft = convs.upsert_draft(conv["id"], {**deliberate.default_fields(), "title": "产品是否扩张", "context": "维持现有产品与尝试新方向之间的选择；尚未确定预算", "options": ["做好现有产品", "小范围试点"]}, message_id=message["id"])

class CandidateFixture(FakeProvider):
    def complete_json(self, req):
        return SAMPLE

if os.environ.get("ZHIJUN_TEST_REAL_LOCAL") == "1":
    decision_suggestions.local_provider = lambda **kw: OllamaProvider("http://127.0.0.1:11434", "qwen3.5:9b", timeout=55, keep_alive=60, num_ctx=8192)
else:
    decision_suggestions.local_provider = lambda **kw: CandidateFixture()

app = FastAPI()
for router in (ontology.router, conversations.router, zhijun_status.router, zhijun_onboarding.router, growth.router, chat_import_routes.build_router(lambda: None)):
    app.include_router(router)

@app.get("/api/mindos/access-context")
def access():
    return {"mode": "local_debug", "localDebug": True}

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "isolated-directions-fixture", "model": "synthetic"}

@app.get("/__fixture")
def info():
    return {"conversationId": conv["id"], "draftId": draft["id"], "revision": draft["revision"], "root": root.name}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8770, log_level="warning")
