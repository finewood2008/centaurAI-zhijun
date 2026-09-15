"""Ordinary short self-description: real workspace turn -> queue -> ontology.

The only model stub is the HTTP response. Production provider selection,
Zhijun source authorization, job execution and working-claim persistence run
unchanged; DE model and model-consent capabilities are deliberately unavailable.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_memory_background_consent import CAPABILITIES
from tests.test_zhijun_worker import CHILD


TRANSPORT = r'''
import io
text=UTTERANCE
def model_http(*args,**kwargs):
    from mindos.zhijun.routing import EGRESS_PERMIT
    permit=EGRESS_PERMIT.get()
    assert callable(permit),'must authorize at the actual HTTP boundary'
    preview=permit()
    assert not preview['missing'] and 'deConsentRequired' not in preview,preview
    purpose=preview['purpose']
    generated.append(purpose)
    if purpose=='chat':
        chunk={'choices':[{'delta':{'content':'了解，我们可以继续聊你的工作。'},'finish_reason':'stop'}]}
        return io.BytesIO(('data: '+json.dumps(chunk,ensure_ascii=False)+'\n\ndata: [DONE]\n\n').encode())
    assert purpose=='extract_turn',purpose
    content={'entities':[], 'claims':[{
        'section':'who','layer':'self_declared','predicate':'role','subject':'me','object':None,
        'content':text,'quote':text,'confidence':0.95,'scope_hint':'long_term',
        'privacy_hint':'private','merge_into':None,
        'why_it_matters':'提供职业规划或技术学习建议时，应结合用户的岗位背景。','date':None}]}
    return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps(content,ensure_ascii=False)}}]}).encode())
patch.object(provider_module.llm_transport,'allowed_urlopen',model_http).start()
'''


SCENARIO = r'''
    from mindos.zhijun import jobs
    from mindos.stores.ontology_store import OntologyStore
    from mindos.stores.conversation_store import ConversationStore
    from mindos.chat_imports import service_info
    # Control when the real worker processes its job, so the assertions see
    # the actual queue event before it completes. No extraction method is mocked.
    jobs.stop_worker()
    onto=OntologyStore.instance();convs=ConversationStore.instance()
    cid={'conversationId':ident}
    service=service_info(provider_module.build_provider())['id']
    mode=dispatch(client,'put_api_mindos_conversations_conversation_id_routing',
        {'mode':'online','acknowledge':True,'expectedRevision':0,'serviceId':service},cid)
    assert mode.status_code==200,mode.text
    policy=dispatch(client,'put_api_mindos_conversations_conversation_id_routing_default_consent',
        {'enabled':True,'includeFiles':False,'includeCharter':True,'autoEgress':True,
         'acknowledge':True,'serviceId':service,'expectedRevision':0},cid)
    assert policy.status_code==200,policy.text
    preview=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_preview',
        {'content':text},cid)
    assert preview.status_code==200,preview.text
    reply=dispatch(client,'post_api_mindos_conversations_conversation_id_messages',
        {'content':text,'routeRevision':preview.json()['revision'],'requestId':'programmer-memory-turn'},cid)
    assert reply.status_code==200,reply.text
    events=[]
    for frame in reply.text.split('\n\n'):
        kind=next((line[7:] for line in frame.splitlines() if line.startswith('event: ')),None)
        data=next((line[6:] for line in frame.splitlines() if line.startswith('data: ')),None)
        if kind and data:events.append((kind,json.loads(data)))
    assert not [data for kind,data in events if kind=='error'],events
    assert events[-1][0]=='message_done',events
    extraction=next(data for kind,data in events if kind=='extraction')
    assert extraction['state']=='queued' and extraction['jobId'],extraction
    jid=extraction['jobId']
    assert not onto.list_claims(trust_states=('confirmed','working'))
    job=onto.claim_next_job('programmer-test-worker')
    assert job['jobId']==jid and job['kind']=='extract_turn',job
    jobs.OntologyWorker.instance().process(job,'programmer-test-worker',store=onto,conv_store=convs)
    completed=onto.get_job(jid)
    assert completed['state']=='done' and len(completed['result']['created'])==1,completed
    claims=onto.list_claims(trust_states=('working',))
    assert len(claims)==1 and claims[0]['section']=='who' and claims[0]['content']==text,claims
    assert not onto.list_claims(trust_states=('confirmed',)),'a proposal is not user confirmation'
    evidence=claims[0]['evidence'][0]
    source=convs.get_message(evidence['messageId'])
    assert source['role']=='user' and source['content']==text and evidence['quote']==text
    assert generated==['chat','extract_turn'],generated
    assert not any(name.startswith(('model.','models.','domain.preview','domain.consent')) for name,_ in port.calls)
'''


@pytest.mark.parametrize('utterance', ['我是程序员', '我是医生', '我是一个程序员'])
def test_programmer_chat_creates_working_ontology_without_de_model_consent(tmp_path, utterance):
    backend = Path(__file__).resolve().parents[1]
    root = tmp_path / 'workspace'
    root.mkdir(mode=0o700)
    setup = CAPABILITIES + TRANSPORT.replace('UTTERANCE', repr(utterance))
    script = CHILD.replace('app=create_app(w)', setup)
    script = script.replace('assert not app.domain.state.active', SCENARIO + '\nassert not app.domain.state.active')
    result = subprocess.run([sys.executable, '-c', script, str(root), 'synthetic-programmer-owner', ''],
        cwd=backend, env=dict(os.environ, PYTHONPATH=str(backend), PYTHONDONTWRITEBYTECODE='1'),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
