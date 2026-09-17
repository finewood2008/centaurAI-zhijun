"""Unified review against existing SQLite-backed APIs; synthetic data only."""
import os
import tempfile
from pathlib import Path

private = tempfile.TemporaryDirectory(prefix='zhijun-revisit-secrets-')
for key in ('CENTAUR_METADATA_DB', 'CENTAUR_GBRAIN_HOME', 'CENTAUR_MCP_DATA_DIR', 'CENTAUR_MCP_CONFIG_DIR'):
    os.environ.pop(key, None)
os.environ['CENTAUR_SECRET_STORE_DIR'] = private.name
from tests.task_routing_fixture import app, convs, onto, local, online
from mindos import growth
from mindos.zhijun import provider, routing
from mindos.zhijun.provider import TextDelta, Done, Usage
from mindos.stores.growth_store import GrowthStore
import json
import uvicorn

def deny_network(*args, **kwargs):
    raise RuntimeError('Revisit fixture forbids external model transport')

provider._open = deny_network
routing.build_provider = lambda: local
routing.local_provider = lambda **kwargs: local

def stream(request):
    local.requests.append(request)
    yield TextDelta('合成回复：可以补充后来发生的事情。这次观察只适用于你说的情境，你可以继续修正。')
    yield Usage(20, 30)
    yield Done('stop')

local.stream = stream
source = convs.create_conversation(title='合成：小范围合作试点')
message = convs.append_message(source['id'], 'user', '我决定先做两周小范围试点，再决定是否继续合作。', meta={'routingSources': []})
decision = GrowthStore.instance().create_decision({'title': '合成：小范围合作试点', 'context': '先核对双方的合作方式',
    'options': ['立即扩大', '先试点'], 'choice': '先试点', 'rationale': '先了解双方如何处理问题', 'confidence': 65,
    'expectedOutcome': '两周内完成一次交付', 'reviewAt': None, 'relatedEntityIds': [],
    'evidenceRefs': [json.dumps({'kind': 'message', 'conversationId': source['id'], 'messageId': message['id']})]})
ordinary = convs.create_conversation(title='合成：一次普通的团队交流')
convs.append_message(ordinary['id'], 'user', '昨天和团队把分工聊清楚了，我想再看看这次交流。', meta={'routingSources': []})
convs.append_message(ordinary['id'], 'assistant', '可以回看具体的交流过程，不必先作出一个选择。', meta={'routingSources': []})
app.router.routes[:] = [r for r in app.router.routes if getattr(r, 'path', '') not in ('/api/health', '/__fixture', '/mindos/{path:path}')]

@app.get('/api/health')
def health():
    return {'status': 'ok', 'version': 'zhijun-revisit-fixture', 'model': 'synthetic'}

@app.get('/__fixture')
def info():
    return {'synthetic': True, 'decisionId': decision['id'], 'sourceId': source['id'], 'conversationId': ordinary['id'], 'requests': len(local.requests)}

from fastapi.responses import HTMLResponse
@app.get('/mindos/{path:path}')
def preview(path: str):
    dist = Path(__file__).resolve().parents[2] / 'frontend/mindos-web/dist/index.html'
    banner = '<div role="note" style="position:fixed;bottom:8px;right:12px;z-index:9999;background:#f2f4eb;padding:8px;font:12px sans-serif">隔离验收 · 合成数据 · 正式使用请打开知君桌面并连接盒子</div>'
    return HTMLResponse(dist.read_text().replace('</body>', banner + '</body>'))

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8777, log_level='warning')
