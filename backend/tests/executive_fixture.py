"""Disposable zero-state application for manual browser workflow validation.

Run PYTHONPATH=backend backend/.venv/bin/python -m tests.executive_fixture.
All stores, imports and projections are inside a temporary directory. Models
are deterministic doubles; no provider or external transport is reachable.
"""
import os
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager

root = tempfile.TemporaryDirectory(prefix="zhijun-executive-browser-")
os.environ.update(CENTAURAI_DATABASE_DATA_ROOT=root.name, ZHIJUN_PROVIDER="fake",
    ZHIJUN_EXTRACTION="1", ZHIJUN_MATERIAL_EVIDENCE="0")

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from mindos import conversations, ontology, growth, zhijun_onboarding, zhijun_status, memory_routes, chat_import_routes, chat_imports, routing_routes
from mindos.zhijun import provider, routing, jobs, charter
from mindos.material_worker import MaterialWorker


class ExecutiveModel(provider.FakeProvider):
    def _reply(self, req):
        if "星桥制造升级试点" in req.system:
            return "【资料里看到的】这份合成方案的当前预算是 8 万元，负责人是林舟。[m1] 我已读取可用正文，可以继续讨论预算、风险或执行顺序。"
        return super()._reply(req)

    def complete_json(self, req):
        text = req.debug.get("userText", "")
        if "claims" in (req.json_schema or {}).get("properties", {}):
            section, predicate = ("who", "role") if "运营负责人" in text else (("principles", "holds_principle") if "原则" in text else ("matters", "working_on"))
            return {"entities": [], "claims": [{"content": text, "quote": text, "section": section,
                "layer": "self_declared", "predicate": predicate, "subject": "me", "object": None,
                "confidence": .9, "scope_hint": "long_term", "privacy_hint": "private", "merge_into": None,
                "why_it_matters": "后续资源安排与重要取舍时，需要核对用户主动表达的角色和约束。", "date": None}]}
        if "candidates" in (req.json_schema or {}).get("properties", {}):
            return {"candidates": [{"text": "我更想先把眼下的工作安排理清楚。"}, {"text": "我想先看看怎样给团队留出成长空间。"}, {"text": "我还没想清楚，今天先聊到这里。"}]}
        return super().complete_json(req)


model = ExecutiveModel()
provider.build_provider = routing.build_provider = routing_routes.build_provider = lambda *args, **kwargs: model
routing.local_provider = chat_imports.local_provider = lambda **kwargs: model
def deny_network(*args, **kwargs):
    raise RuntimeError("The executive fixture forbids all model network requests")
provider._open = deny_network
# Text parsing remains real; automatic AI derivatives are outside this fixture.
MaterialWorker._trigger_derived = lambda *args, **kwargs: None


@asynccontextmanager
async def lifespan(app):
    materials = MaterialWorker()
    materials.start()
    jobs.start_worker()
    chat_imports.start_worker()
    try:
        yield
    finally:
        jobs.stop_worker()
        chat_imports.stop_worker()
        materials.stop()


app = FastAPI(lifespan=lifespan)
for router in (conversations.router, ontology.router, growth.router, zhijun_status.router,
               zhijun_onboarding.router, memory_routes.build_router(), charter.build_router(),
               chat_import_routes.build_router(lambda: None)):
    app.include_router(router)

@app.get('/api/mindos/access-context')
def access():
    return {"mode": "local_debug", "localDebug": True}

@app.get('/api/health')
def health():
    return {"status": "ok", "version": "executive-fixture", "model": "synthetic"}

dist = Path(__file__).resolve().parents[2] / 'frontend' / 'mindos-web' / 'dist'
app.mount('/mindos/assets', StaticFiles(directory=dist / 'assets'), name='assets')
@app.get('/mindos/{path:path}')
def ui(path: str):
    return FileResponse(dist / 'index.html')

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8773, log_level='warning')
