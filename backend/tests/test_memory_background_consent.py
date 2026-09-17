"""Workspace background memory preserves source authorization across DE model capabilities."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_zhijun_worker import CHILD
from tests.test_zhijun_worker_ports import CAPABILITY_FIXTURE


CAPABILITIES = CAPABILITY_FIXTURE + r'''
provider_module=direct_provider
generated=[]
def fixture_json(preview):
    assert preview['purpose']=='extract_turn' and not preview['missing']
    generated.append(preview)
    text='我长期认同做决定前尊重当事人的意愿'
    return {'entities':[], 'claims':[{
        'section':'principles','layer':'self_declared','predicate':'holds_principle',
        'subject':'me','object':None,'content':text,'quote':text,'confidence':0.95,
        'scope_hint':'long_term','privacy_hint':'private','merge_into':None,
        'why_it_matters':'讨论合作与个人决定时，先核对当事人的意愿和边界。','date':None}]}
'''


SCENARIO = r'''
    from mindos.zhijun import jobs
    from mindos.stores.ontology_store import OntologyStore
    from mindos.stores.conversation_store import ConversationStore
    from mindos.stores.routing_store import RoutingStore
    from mindos.chat_imports import service_info
    from zhijun_worker.capabilities import execution
    jobs.stop_worker()
    onto=OntologyStore.instance();convs=ConversationStore.instance();routes=RoutingStore(onto)
    cid={'conversationId':ident}
    service=service_info(provider_module.build_provider())['id']
    mode=dispatch(client,'put_api_mindos_conversations_conversation_id_routing',
        {'mode':'online','acknowledge':True,'expectedRevision':0,'serviceId':service},cid)
    assert mode.status_code==200,mode.text
    action=ACTION
    if action in {'default','revoked','configuration_changed','charter_default','charter_previous'}:
        policy=dispatch(client,'put_api_mindos_conversations_conversation_id_routing_default_consent',
            {'enabled':True,'includeFiles':False,'includeCharter':not action.startswith('charter_'),'autoEgress':True,
             'acknowledge':True,'serviceId':service,'expectedRevision':0},cid)
        assert policy.status_code==200,policy.text
    charter=None
    if action.startswith('charter_'):
        from mindos.stores.growth_store import GrowthStore
        from mindos.stores.chat_import_store import ChatImportStore
        from mindos.stores.charter_draft_store import render_document
        clause={'id':'synthetic-private-charter','section':'与知君合作','text':'合成私密章程正文，不应进入抽取请求',
                'kind':'principle','scope':'global','context':'','control':None,'sources':[]}
        charter=GrowthStore.instance().create_charter({'document':render_document([clause]),'clauses':[clause],
            'expectedVersion':0,'workspaceId':'synthetic-memory-workspace',
            'metadata':{'scope':ChatImportStore(convs).scope(ident),'origin':'workspace','sources':[]}})
        if action=='charter_previous':
            convs.append_message(ident,'assistant','合成上一条私密回复。你的职业是什么？',meta={
                'routingOrigin':{'service':service},'routingSources':[
                    {'kind':'charter_clause','id':charter['id']+':'+clause['id']}]})
    message=convs.append_message(ident,'user','请记住：我长期认同做决定前尊重当事人的意愿。',meta={'routingSources':[]})
    origin=execution.set({'requestId':'synthetic-message-request',
        'operationId':'post_api_mindos_conversations_conversation_id_messages'})
    try:jid=jobs.enqueue_extraction(ident,message['id'],store=onto)
    finally:execution.reset(origin)
    if action=='revoked':
        response=dispatch(client,'put_api_mindos_conversations_conversation_id_routing_default_consent',
            {'enabled':False,'expectedRevision':1},cid)
        assert response.status_code==200,response.text
    if action=='configuration_changed':snapshot.secret_ref='rotated-synthetic-ref'
    def run_claimed():
        job=onto.claim_next_job('synthetic-worker')
        assert job['jobId']==jid,job
        jobs.OntologyWorker.instance().process(job,'synthetic-worker',store=onto,conv_store=convs)
        saved=onto.get_job(jid)
        assert saved['state']=='done',saved
        return saved['result']
    def resume():
        response=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_resume',
            {'task':'extract_turn','jobId':jid},cid)
        assert response.status_code==200,response.text
        return response.json()
    result=run_claimed()
    if action in {'default','charter_default','charter_previous'}:
        assert result['state']=='done' and len(result['created'])==1,result
        assert len(generated)==1
        if charter:
            from mindos.stores.chat_import_store import ChatImportStore
            assert not routes.policy(ChatImportStore(convs).scope(ident))['includeCharter']
            assert not any(s['kind'].startswith('charter') for s in generated[0]['sources'])
            assert clause['text'] not in generated[0]['request']['system']
            assert '合成上一条私密回复' not in str(generated[0]['request']['messages'])
            assert generated[0]['charterBasis']['version']==charter['version']
    elif action=='configuration_changed':
        assert result['state']=='paused' and result['reason']=='online_service_changed',result
        assert not generated and not onto.list_claims(trust_states=('confirmed','working'))
    else:
        assert result['state']=='paused' and result['reason']=='consent_required',result
        assert not generated and not onto.list_claims(trust_states=('confirmed','working'))
        p=routes.get_preview(result['previewId'],ident)
        assert p['missing'],p
        assert not generated and not port.receipts
        if action in {'explicit','source_changed'}:
            grant=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_grant',
                {'revision':result['previewId'],'keys':[s['key'] for s in p['sources']]},cid)
            assert grant.status_code==200,grant.text
            if action=='source_changed':
                convs.update_message(message['id'],content='请记住：我长期认同先核对事实再做决定。')
            assert resume()['queuedCount']==1
            assert resume()['queuedCount']==0
            result=run_claimed()
            if action=='explicit':
                assert result['state']=='done' and len(result['created'])==1,result
                assert len(generated)==1
            else:assert result['state']=='paused' and not generated,result
        elif action=='pause_only':
            assert resume()['queuedCount']==1
            assert run_claimed()['state']=='paused' and not generated
    if generated:
        # V3（拍板 4）：用户亲口要求记住的第一人称原话直接记为已确认（可撤回），不再进待确认队列。
        assert onto.list_claims(trust_states=('working',))==[]
        candidates=onto.list_claims(trust_states=('confirmed',))
        assert len(candidates)==1 and candidates[0]['trustOrigin']=='utterance',candidates
        assert result['autoConfirmed']==result['created'],result
        assert resume()['queuedCount']==0
    assert convs.get_message(message['id']) is not None
    assert any(name=='model.describe' for name,_ in port.calls)
    assert len([1 for name,_ in port.calls if name=='model.complete_json'])==len(generated)
'''


@pytest.mark.parametrize('action', ['default','pause_only','explicit','source_changed','revoked','configuration_changed',
                                  'charter_default','charter_previous'])
def test_workspace_memory_background_consent(tmp_path, action):
    backend = Path(__file__).resolve().parents[1]
    root = tmp_path / 'workspace'
    root.mkdir(mode=0o700)
    script = CHILD.replace('app=create_app(w)', CAPABILITIES)
    script = script.replace('assert not app.domain.state.active', SCENARIO.replace('ACTION', repr(action)) + '\nassert not app.domain.state.active')
    result = subprocess.run([sys.executable, '-c', script, str(root), 'synthetic-memory-owner', ''],
        cwd=backend, env=dict(os.environ, PYTHONPATH=str(backend)), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
