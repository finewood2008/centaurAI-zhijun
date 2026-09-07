"""Disposable actual-API UI fixture for matters; never reads live data or invokes a network model."""
from tests.task_routing_fixture import app, convs, online, local, onto
from mindos import matters_routes, zhijun_home
from mindos.zhijun import provider
import uvicorn

def deny_network(*args, **kwargs):
    raise RuntimeError("Matter fixture forbids external model transport")

provider._open = deny_network
app.include_router(matters_routes.build_router())
app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", "") not in ("/api/health", "/__fixture")]
conversation = convs.create_conversation(title="合成案例：准备与合伙人的沟通")
other = convs.create_conversation(title="合成案例：隔天继续同一件事")
convs.append_message(conversation["id"], "user", "我想准备与合伙人的沟通，先明确职责，再讨论授权范围。", meta={"routingSources": []})
content = "# 合伙人沟通准备\n\n## 目标\n讨论职责与授权边界，未决定的安排留待双方确认。\n\n## 谈话提纲\n" + "先说明希望减少反复审批，同时保留关键风险的共同决定。核对哪些决定可以独立作出，哪些需要事先商量；对未确定的时间、负责人不做假设。\n\n" * 4 + "完整文稿末尾标记：先做一次可逆的尝试。"
reply = convs.append_message(conversation["id"], "assistant", content, meta={"routingSources": []})
convs.append_message(other["id"], "user", "隔天想继续准备沟通。", meta={"routingSources": []})
convs.append_message(other["id"], "assistant", "可以从已经明确的条件接着往下看。", meta={"routingSources": []})

@app.get('/api/health')
def health():
    return {"status": "ok", "version": "matters-fixture", "model": "synthetic"}

@app.get('/__fixture')
def info():
    return {"conversationId": conversation["id"], "otherConversationId": other["id"], "replyMessageId": reply["id"],
            "replyContent": content, "onlineRequests": [{"system": r.system, "messages": r.messages} for r in online.requests],
            "localRequests": len(local.requests)}

@app.get('/api/mindos/zhijun/home')
def home():
    result = zhijun_home.build_home_overview(enqueue=False, ontology=onto, conversations=convs)
    result['brief']['status'] = 'ready'
    return result

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8774, log_level='warning')
