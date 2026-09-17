"""Disposable V3 UI/API preview. All records and model replies are synthetic."""
import os
import tempfile
from pathlib import Path

private = tempfile.TemporaryDirectory(prefix='zhijun-reflection-secrets-')
for key in ('CENTAUR_METADATA_DB','CENTAUR_GBRAIN_HOME','CENTAUR_MCP_DATA_DIR','CENTAUR_MCP_CONFIG_DIR'):
    os.environ.pop(key,None)
os.environ['CENTAUR_SECRET_STORE_DIR'] = private.name
from tests.task_routing_fixture import app, convs, onto, local, online
from mindos import reflection_routes, zhijun_status, zhijun_home
from mindos.zhijun import provider, routing, reflections
from mindos.zhijun.provider import TextDelta, Done, Usage
from mindos.stores.routing_store import RoutingStore
from mindos.stores.reflection_store import ReflectionStore
from datetime import datetime, timedelta, timezone
import uvicorn


def deny_network(*args, **kwargs):
    raise RuntimeError('Reflection fixture forbids external model transport')

provider._open = deny_network
routing.build_provider = lambda: online
routing.local_provider = lambda **kwargs: local
zhijun_status.provider_status = lambda: {'provider':'synthetic','model':'V3 隔离体验 · 合成数据','external':False,'configured':True,'error':None}
app.include_router(reflection_routes.build_router())
from mindos import matters_routes
app.include_router(matters_routes.build_router())
app.include_router(zhijun_home.router)
now = datetime.now(timezone.utc)
prior = convs.create_conversation(title='演示：第一次渠道合作')
current = convs.create_conversation(title='V3 照见体验 · 合成案例')
old = convs.append_message(prior['id'],'user','上周谈新的渠道合作，对方主动讲清楚交付风险，我愿意继续推进合作。',meta={'routingSources':[]})
with convs._connect() as db:
    db.execute('UPDATE messages SET created_at=? WHERE id=?',((now-timedelta(days=8)).isoformat(),old['id']))
new = convs.append_message(current['id'],'user','今天谈另一个新的供应合作，对方把交付问题讲清楚，我决定先做小范围合作。',meta={'routingSources':[]})
reply = convs.append_message(current['id'],'assistant','可以先把这次小范围合作的目标和投入说清楚，给双方留出核对与调整的空间。',meta={'routingSources':[]})
RoutingStore(onto).set_mode(current['id'],'local','')
local.result={'candidate':{'type':'pattern','title':'建立新合作时，你留意对方是否坦诚',
'observation':'最近两次谈新合作时，你都留意对方是否愿意把问题讲清楚。我在想，这可能是你决定要不要进一步投入的重要依据。这样理解贴近你吗？',
'alternative':'也可能是这两次合作的交付风险较大，所以你需要额外核对。','confidence':.86,'sensitivity':'ordinary',
'evidence':[{'messageId':old['id'],'quote':old['content']},{'messageId':new['id'],'quote':new['content']}]}}
result=reflections.run_job({'conversationId':current['id'],'messageId':new['id'],'assistantId':reply['id'],'localOnly':True},onto,convs)
assert result['state']=='done', result
ledger=ReflectionStore(onto)


def stream(request):
    local.requests.append(request)
    item=ledger.get(result['reflectionId'])
    note=(item.get('feedback') or {}).get('note','')
    if note and note in request.system and item['status']=='contextual':
        text='演示回复：我会保留你补充的适用条件：“'+note+'” 这次和老伙伴合作，你最想先核对项目目标还是分工？'
    else:
        text='演示回复：先从这次合作的实际情况聊起。你最想一起想清楚哪一部分？'
    yield TextDelta(text)
    yield Usage(20,30)
    yield Done('stop')
local.stream=stream

app.router.routes[:]=[r for r in app.router.routes if getattr(r,'path','') not in ('/api/health','/__fixture')]
@app.get('/api/health')
def health():
    return {'status':'ok','version':'zhijun-v3-reflection-fixture','model':'synthetic'}
@app.get('/__fixture')
def info():
    return {'synthetic':True,'conversationId':current['id'],'reflectionId':result['reflectionId'],
            'requests':[{'system':r.system,'messages':r.messages} for r in local.requests]}

# A visible fixture label keeps synthetic acceptance data distinct from a live box.
from fastapi.responses import HTMLResponse
app.router.routes[:] = [r for r in app.router.routes if getattr(r, 'path', '') != '/mindos/{path:path}']
@app.get('/mindos/{path:path}')
def preview(path: str):
    dist = Path(__file__).resolve().parents[2] / 'frontend/mindos-web/dist/index.html'
    banner = '<div role="note" style="position:fixed;bottom:10px;right:12px;z-index:9999;max-width:calc(100vw - 24px);box-sizing:border-box;padding:8px 12px;border:1px solid #d4d9cc;border-radius:8px;background:#f2f4eb;color:#59654a;font:12px/1.5 sans-serif">V3 验收预览 · 合成数据 · 未连接真实盒子</div>'
    return HTMLResponse(dist.read_text().replace('</body>', banner + '</body>'))

if __name__=='__main__':
    uvicorn.run(app,host='127.0.0.1',port=8776,log_level='warning')
