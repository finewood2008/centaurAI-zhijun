"""Chat survives failed followup registration at the signed HTTP boundary."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_zhijun_worker import CHILD
from tests.test_zhijun_worker_ports import CAPABILITY_FIXTURE


def test_enqueue_error_keeps_capability_handler_and_safe_status():
    from zhijun_worker.background import BackgroundEnqueueError
    from zhijun_worker.capabilities import CapabilityError
    cause = CapabilityError('WORKSPACE_BACKGROUND_CAPACITY', 429)
    error = BackgroundEnqueueError('job-example', cause)
    assert isinstance(error, CapabilityError)
    assert (error.code, error.status, error.job_id) == ('WORKSPACE_BACKGROUND_CAPACITY', 429, 'job-example')
    unsafe = BackgroundEnqueueError(None, RuntimeError('private upstream details'))
    assert (unsafe.code, unsafe.status, unsafe.job_id) == ('BACKGROUND_ENQUEUE_FAILED', 503, None)
    assert 'private' not in str(unsafe)


CALLBACK = r'''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from zhijun_worker.auth import NonceStore, verify
from zhijun_worker.capabilities import HttpCapabilities
callback_requests=[]
nonces=NonceStore()
class Callback(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        raw=self.rfile.read(int(self.headers['Content-Length']))
        verify(self.headers[HEADER],w.key,w.workspace_id,w.ownership_epoch,raw,nonces,
               expected_relative_path=self.path)
        envelope=json.loads(raw)
        callback_requests.append(envelope)
        assert envelope['executionRequestId']=='request-test',envelope
        assert envelope['operationId'] in {
            'post_api_mindos_conversations_conversation_id_messages',
            'post_api_mindos_conversations_conversation_id_routing_resume'},envelope
        failure=os.environ['COMPLETION_FAILURE']
        status=200
        if failure=='rejected' and envelope['name']=='domain.background.register':
            status=429
            result={'error':{'code':'WORKSPACE_BACKGROUND_CAPACITY'}}
        elif failure=='invalid' and envelope['name']=='domain.background.register':
            result={'ok':True}
        else:
            result={'ok':True,'result':{'registered':True}}
        body=json.dumps(result).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
server=ThreadingHTTPServer(('127.0.0.1',0),Callback)
thread=Thread(target=server.serve_forever,daemon=True);thread.start()
callback=HttpCapabilities(w,'http://127.0.0.1:'+str(server.server_port))
'''


CHAT = r'''    from mindos.zhijun import jobs
    jobs.stop_worker()
    cid={'conversationId':ident}
    mode=dispatch(client,'put_api_mindos_conversations_conversation_id_routing',
        {'mode':'online','acknowledge':True,'expectedRevision':0,'serviceId':online_service()},cid)
    assert mode.status_code==200,mode.text
    content='我是一个程序员，主要想技术架构上深究'
    turn_id='completion-regression-turn'
    preview_body={'content':content,'requestId':turn_id}
    preview=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_preview',preview_body,cid)
    assert preview.status_code==200,preview.text
    p=preview.json()
    grant=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_grant',
        {'revision':p['revision'],'keys':[s['key'] for s in p['sources']]},cid)
    assert grant.status_code==200,grant.text
    fresh=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_preview',preview_body,cid).json()
    wire.clear()
    message_body={**preview_body,'routeRevision':fresh['revision']}
    result=dispatch(client,'post_api_mindos_conversations_conversation_id_messages',message_body,cid)
    assert result.status_code==200,result.text
    output=b''.join(wire).decode()
    events=[]
    for frame in output.strip().split('\n\n'):
        lines=frame.splitlines()
        events.append((lines[0][7:],json.loads(lines[1][6:])))
    names=[name for name,data in events]
    print(json.dumps({'failure':os.environ['COMPLETION_FAILURE'],'events':names,
                      'callbacks':callback_requests},ensure_ascii=False))
    assert 'token' in names,output
    assert callback_requests,output
    assert callback_requests[0]['name']=='domain.background.register'
    assert names[-2:]==['extraction','message_done'],names
    assert 'error' not in names,names
    extraction=events[-2][1]
    from mindos.stores.ontology_store import OntologyStore
    onto=OntologyStore.instance()
    saved=onto.get_job(extraction['jobId'])
    assert saved['kind']=='extract_turn',saved
    assert saved['payload']['conversationId']==ident,saved
    assert saved['payload']['messageId']==events[0][1]['userMessageId'],saved
    if os.environ['COMPLETION_FAILURE']=='none':
        assert extraction['state']=='queued',extraction
        assert 'code' not in extraction,extraction
        assert saved['state']=='queued',saved
    else:
        expected='WORKSPACE_BACKGROUND_CAPACITY' if os.environ['COMPLETION_FAILURE']=='rejected' else 'CAPABILITY_CONTRACT_INVALID'
        assert extraction['state']=='failed' and extraction['code']==expected,extraction
        assert saved['state']=='failed' and saved['errorCode']==expected,saved
        assert saved['failureClass']=='registration',saved
        assert saved['attempts']==0,saved
        assert onto.claim_next_job('must-not-run') is None
        resume_body={'task':'extract_turn','jobId':saved['jobId']}
        denied=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_resume',resume_body,cid)
        assert denied.status_code>=400,denied.text
        assert onto.get_job(saved['jobId'])['state']=='failed'
        os.environ['COMPLETION_FAILURE']='none'
        resumed=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_resume',resume_body,cid)
        assert resumed.status_code==200,resumed.text
        assert resumed.json()['queuedCount']==1 and resumed.json()['jobId']==saved['jobId'],resumed.text
        queued=onto.get_job(saved['jobId'])
        assert queued['state']=='queued',queued
        assert queued['payload']['conversationId']==saved['payload']['conversationId']
        assert queued['payload']['messageId']==saved['payload']['messageId']
        again=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_resume',resume_body,cid)
        assert again.status_code==200 and again.json()['queuedCount']==0,again.text
    from mindos.stores.conversation_store import ConversationStore
    convs=ConversationStore.instance()
    assistants=[m for m in convs.list_messages(ident) if m['role']=='assistant']
    assert len(assistants)==1 and assistants[-1]['status']=='complete',assistants
    assert '隔离模型端口测试回答' in assistants[-1]['content']
    before=convs.list_messages(ident)
    callback_count=len(callback_requests)
    model_count=len(model_requests)
    replay=dispatch(client,'post_api_mindos_conversations_conversation_id_messages',message_body,cid)
    assert replay.status_code==200 and 'event: message_done' in replay.text,replay.text
    assert '"replayed": true' in replay.text,replay.text
    assert convs.list_messages(ident)==before
    assert len(callback_requests)==callback_count
    assert len(model_requests)==model_count
    server.shutdown();server.server_close();thread.join(timeout=2)
'''


@pytest.mark.parametrize('failure', ['none', 'rejected', 'invalid'])
def test_signed_online_completion_callback(tmp_path, failure):
    backend = Path(__file__).resolve().parents[1]
    root = tmp_path / 'data'
    root.mkdir(mode=0o700)
    fixture = CAPABILITY_FIXTURE.replace(
        "if name in {'domain.background.register','domain.background.finish'}:return {'ok':True}",
        "if name in {'domain.background.register','domain.background.finish'}:return callback.call(name,payload)",
    )
    fixture = fixture.replace('app=create_app(w,SyntheticCapabilities())', CALLBACK + '''
domain_app=create_app(w,SyntheticCapabilities())
wire=[]
class Capture:
    domain=domain_app.domain
    catalog=domain_app.catalog
    async def __call__(self,scope,receive,send):
        async def capture(message):
            if message['type']=='http.response.body':wire.append(message.get('body',b''))
            await send(message)
        await domain_app(scope,receive,capture)
app=Capture()
''')
    script = CHILD.replace('app=create_app(w)', fixture)
    script = script.replace('assert not app.domain.state.active', CHAT + 'assert not app.domain.state.active')
    result = subprocess.run(
        [sys.executable, '-c', script, str(root), 'test-owner', ''], cwd=backend,
        env=dict(os.environ, PYTHONPATH=str(backend), PYTHONDONTWRITEBYTECODE='1', COMPLETION_FAILURE=failure),
        capture_output=True, text=True, timeout=45,
    )
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
