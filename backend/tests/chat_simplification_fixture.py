"""Conversation UI acceptance: synthetic SQLite and recorded providers only (8778)."""
import os
import tempfile

private = tempfile.TemporaryDirectory(prefix='zhijun-chat-simple-secrets-')
for key in ('CENTAUR_METADATA_DB', 'CENTAUR_GBRAIN_HOME', 'CENTAUR_MCP_DATA_DIR', 'CENTAUR_MCP_CONFIG_DIR'):
    os.environ.pop(key, None)
os.environ['CENTAUR_SECRET_STORE_DIR'] = private.name

from tests.chat_send_fixture import app, convs, online, local, store, service, cases, onto
from mindos import zhijun_status
from mindos.zhijun.provider import TextDelta, Done, Usage
from pathlib import Path
from fastapi.responses import HTMLResponse
import uvicorn

legacy = convs.create_conversation(title='合成：旧对话继续')
store.set_mode(legacy['id'], 'local', '')
convs.append_message(legacy['id'], 'assistant', 'PROTECTED_OLD_HISTORY', meta={'localOnlyDerived': True})
cases['legacy'] = legacy['id']
cases['consent'] = convs.create_conversation(title='合成：逐项资料确认')['id']
store.set_mode(cases['consent'], 'online', service)
job_message = convs.list_messages(cases['short'])[0]
job_id = onto.enqueue_job('extract_turn', job_message['id'], payload={'conversationId': cases['short'], 'messageId': job_message['id']})
with onto._connect() as db:
    db.execute("UPDATE ontology_jobs SET state='failed', error_code='PROVIDER_TIMEOUT' WHERE job_id=?", (job_id,))

def reply(request):
    online.requests.append(request)
    if online.error:
        raise online.error
    yield TextDelta('合成回答：先核对这次的目标，保留可以调整的空间。')
    yield Usage(20, 30)
    yield Done('stop')

online.stream = reply
zhijun_status.provider_status = lambda: {'provider': 'openai', 'model': 'synthetic', 'external': True, 'configured': True, 'error': None}
app.router.routes[:] = [r for r in app.router.routes if getattr(r, 'path', '') not in ('/api/health', '/__fixture', '/mindos/{path:path}')]

@app.get('/api/health')
def health():
    return {'status': 'ok', 'version': 'chat-simplification-fixture'}

@app.get('/__fixture')
def info():
    return {'synthetic': True, 'cases': cases, 'onlineRequests': len(online.requests), 'localRequests': len(local.requests),
            'payloads': [{'system': r.system, 'messages': r.messages} for r in online.requests], 'defaultMode': store.mode('default:global'), 'policy': store.policy('global')}

@app.post('/__fixture/source-consent')
def source_consent():
    p = store.policy('global')
    store.set_policy('global', enabled=False, service=p['service'], service_name=p['serviceName'], include_files=False,
                     purposes=p['purposes'], expected_revision=p['revision'])
    return {'ok': True}

# Read-only settings fixtures let the shared component render its original mode.
@app.get('/api/system/models/material-runtime')
def material_runtime():
    return {'revision': 1, 'source': 'defaults', 'baseUrl': 'http://synthetic.invalid', 'model': 'synthetic-local',
            'timeoutSeconds': 30, 'appliesTo': [], 'health': {'reachable': False, 'modelInstalled': False}}

@app.get('/api/system/models/chat-provider')
def chat_provider():
    return {'revision': 1, 'source': 'defaults', 'provider': 'openai', 'effectiveProvider': 'openai',
            'externalEnabled': True, 'baseUrl': 'http://synthetic.invalid', 'model': 'synthetic',
            'apiKeyConfigured': True, 'apiKeyHint': None, 'timeoutSeconds': 30, 'totalBudgetSeconds': 60,
            'fallbackOllama': False}

@app.get('/mindos/{path:path}')
def preview(path: str):
    dist = Path(__file__).resolve().parents[2] / 'frontend/mindos-web/dist/index.html'
    banner = '<div role="note" style="position:fixed;bottom:4px;right:8px;z-index:9999;background:#f4f1e9;padding:4px;font:11px sans-serif">隔离验收 · 合成数据</div>'
    return HTMLResponse(dist.read_text().replace('</body>', banner + '</body>'))

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8778, log_level='warning')
