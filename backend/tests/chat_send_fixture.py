"""Disposable real API + recording models for chat-send browser regression (8775)."""
from tests.task_routing_fixture import app, convs, online, local, onto
from mindos import zhijun_status
from mindos.stores.routing_store import RoutingStore
from mindos.zhijun import provider, routing
from mindos.zhijun.provider import TextDelta, Usage, Done
import uvicorn


def deny_network(*args, **kwargs):
    raise RuntimeError("Chat-send fixture forbids external model transport")


provider._open = deny_network
routing.build_provider = lambda: online
routing.local_provider = lambda **kwargs: local
zhijun_status.provider_status = lambda: {"provider": "synthetic", "model": "chat-send-fixture", "external": True, "configured": True, "error": None}


def reply(model, request):
    model.requests.append(request)
    yield TextDelta("合成完整回复：先明确这次沟通希望达成什么，再列出可以商量与不能承诺的部分。")
    yield TextDelta("\n\n你可以先选一个最重要的目标，我们再把它整理成可修改的提纲。[p1]")
    yield Usage(20, 30)
    yield Done("stop")


online.stream = lambda request: reply(online, request)
local.stream = lambda request: reply(local, request)
store = RoutingStore(onto)
service = routing.service_info(online)["id"]
store.set_mode("default:global", "online", service)
store.set_policy("global", enabled=True, service=service, service_name="合成服务", include_files=False,
                 purposes=["chat", "reply_assistance"], expected_revision=0)

cases = {}
protected = onto.create_claim({"content": "合成原则：沟通确认前先说明承诺范围", "section": "principles", "layer": "self_declared"},
                              [{"kind": "user_edit", "quote": "合成原则：沟通确认前先说明承诺范围"}], trust_state="confirmed", trust_origin="user_created")
for name in ("short", "assisted", "refresh", "failure", "source"):
    conversation = convs.create_conversation(title="合成发送验收：" + name)
    cid = conversation["id"]
    store.set_mode(cid, "online", service)
    if name in ("short", "assisted", "source"):
        convs.append_message(cid, "user", "我想准备一次与合伙人的沟通，先明确希望达成的目标。", meta={"routingSources": []})
        sources = []
        if name == "source":
            router = routing.Router(onto, convs, cid)
            sources = [router.resolve(router.ref("claim", protected["id"]))[0]["ref"]]
        convs.append_message(cid, "assistant", "你希望这次重要沟通先明确目标与职责。[p1] 你更想先谈目标，还是先谈分工？", meta={"routingSources": sources})
    cases[name] = cid

# A real stored legacy summary with explicitly null ancestry must be excluded,
# not crash a fresh conversation's context preview during global retrieval.
legacy = convs.create_conversation(title="合成旧摘要")
old = convs.append_message(legacy["id"], "user", "准备一次重要沟通，明确合伙人的沟通目标和职责。", meta={"routingSources": []})
summary = convs.save_summary(legacy["id"], up_to_seq=old["seq"], summary=old["content"], meta={"routingSources": None})

app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", "") not in ("/api/health", "/__fixture")]


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "chat-send-fixture", "model": "synthetic"}


@app.get("/__fixture")
def info():
    return {"cases": cases, "legacySummaryId": f"{legacy['id']}:{summary['revision']}",
            "onlineRequests": len(online.requests), "localRequests": len(local.requests)}


@app.post("/__fixture/change-source")
def change_source():
    # Real ancestor version change, without changing the conversation/context.
    onto.add_evidence(protected["id"], [{"kind": "user_edit", "quote": "合成新依据：尚未确认的责任应留空"}])
    return {"changed": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8775, log_level="warning")
