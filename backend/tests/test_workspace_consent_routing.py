"""Real authenticated product routes must acquire DE consent before egress."""
import os
from pathlib import Path
import subprocess
import sys

from tests.test_zhijun_worker import CHILD


FIXTURE = r'''
import time
from zhijun_worker.capabilities import execution, CapabilityError
class ConsentPort:
    def __init__(self):
        self.calls=[]
        self.previews={}
        self.fail_issue=False
        self.fail_policy=False
        self.configuration='a'*64
        self.policy=None
        self.tasks={}
    def call(self,name,payload):
        self.calls.append((name,payload))
        if name=='model.describe':
            return dict(name='ollama' if payload['localOnly'] else 'openai', model='fixture',
                external=not payload['localOnly'],configurationRevision=self.configuration,
                serviceId='local-fixture' if payload['localOnly'] else 'managed-fixture')
        if name=='domain.preview.register':
            assert payload['configurationRevision']==self.configuration
            self.previews[payload['preview']['revision']]=payload['preview']
            return {'registered':True}
        if name=='models.consent.issue':
            assert execution.get()['operationId']=='post_api_mindos_conversations_conversation_id_routing_grant'
            if self.fail_issue: raise CapabilityError('WORKSPACE_CONSENT_MISMATCH',403)
            preview=self.previews[payload['previewRevision']]
            assert sorted(s['key'] for s in payload['selectedSources'])==sorted(s['key'] for s in preview['sources'])
            assert payload['configurationRevision']==self.configuration
            return {'consentId':'fixture-'+payload['grantId'],'expiresAt':time.time()+1800}
        if name=='models.consent.background':
            grant=payload['grant']
            if (self.tasks.get(payload['backgroundId'])!=grant['purpose'] or not self.policy
                    or self.policy.get('action')!='enable' or not self.policy.get('autoEgress')
                    or self.policy['policyRevision']!=grant['authorization']['policyRevision']):
                raise CapabilityError('WORKSPACE_DEFAULT_CONSENT_MISMATCH',403)
            assert grant['authorization']['kind']=='default'
            assert grant['background']=={'id':payload['backgroundId'],
                'executionRequestId':execution.get()['requestId'],'operationId':execution.get()['operationId']}
            return {'consentId':'fixture-'+grant['grantId'],'expiresAt':time.time()+1800}
        if name=='domain.consent-policy.register':
            assert execution.get()['operationId'] in {
                'put_api_mindos_conversations_conversation_id_routing_default_consent',
                'post_api_mindos_conversations_conversation_id_routing_revoke'}
            if self.fail_policy: raise CapabilityError('CAPABILITY_UNAVAILABLE',503)
            self.policy=payload
            return {'registered':True,'revision':payload['policyRevision']}
        if name=='models.consent.revoke':return {'revoked':True}
        if name=='domain.background.register':
            self.tasks[payload['id']]=payload['purpose']
            return {'registered':True}
        if name=='domain.background.finish':
            self.tasks.pop(payload['id'],None)
            return {'finished':True}
        if name=='materials.evidence':return []
        if name=='retrieval.score':return {}
        raise AssertionError('Unexpected capability '+name)
    def stream(self,name,payload):
        self.calls.append((name,payload))
        assert name=='model.stream'
        assert payload['localOnly'] or payload.get('consentId','').startswith('fixture-')
        return iter([{'type':'text','text':'合成回答'},{'type':'done','stop_reason':'stop'}])
port=ConsentPort()
app=create_app(w,port)
# A workspace must never fall back to the old direct transport.
from mindos.zhijun import provider as product_provider
def denied_transport(*args,**kwargs):raise AssertionError('worker attempted direct model transport')
product_provider.llm_transport.allowed_urlopen=denied_transport
'''


def run_routes(tmp_path, body):
    backend = Path(__file__).resolve().parents[1]
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    script = CHILD.replace("app=create_app(w)", FIXTURE)
    script = script.replace("assert not app.domain.state.active", body + "assert not app.domain.state.active")
    result = subprocess.run([sys.executable, "-c", script, str(root), "test-owner", ""],
        cwd=backend, env=dict(os.environ, PYTHONPATH=str(backend)), capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr


SETUP = r'''    cid={'conversationId':ident}
    mode=dispatch(client,'put_api_mindos_conversations_conversation_id_routing',
        {'mode':'online','acknowledge':True,'expectedRevision':0,'serviceId':'managed-fixture'},cid)
    assert mode.status_code==200,mode.text
    def preview(content):
        result=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_preview',{'content':content},cid)
        assert result.status_code==200,result.text
        return result.json()
    def grant(p,**extra):
        return dispatch(client,'post_api_mindos_conversations_conversation_id_routing_grant',
            {'revision':p['revision'],'keys':[s['key'] for s in p['sources']],**extra},cid)
'''


def test_product_preview_grant_send_binds_exact_prompt_and_blocks_unconsented_send(tmp_path):
    run_routes(tmp_path, SETUP + r'''    p=preview('合成问题')
    assert p['deConsentRequired'] is True
    rejected=dispatch(client,'post_api_mindos_conversations_conversation_id_messages',
        {'content':'合成问题','routeRevision':p['revision']},cid)
    assert 'ROUTE_CONSENT_REQUIRED' in rejected.text,rejected.text
    assert not any(name=='model.stream' for name,_ in port.calls)
    g=grant(p)
    assert g.status_code==200,g.text
    fresh=preview('合成问题')
    assert fresh['deConsentRequired'] is False
    from mindos.stores.ontology_store import OntologyStore
    with OntologyStore.instance()._connect() as db:
        db.execute('UPDATE workspace_consent_receipts SET expires_at=0')
    assert preview('合成问题')['deConsentRequired'] is True
    renewed=grant(fresh)
    assert renewed.status_code==200,renewed.text
    assert preview('另一条合成问题')['deConsentRequired'] is True
    # A policy or mode/source flag change cannot transfer the exact prompt receipt.
    port.configuration='b'*64
    assert preview('合成问题')['deConsentRequired'] is True
    port.configuration='a'*64
    result=dispatch(client,'post_api_mindos_conversations_conversation_id_messages',
        {'content':'合成问题','routeRevision':fresh['revision']},cid)
    assert 'event: message_done' in result.text,result.text
    assert 'event: error' not in result.text,result.text
    assert any(name=='model.stream' for name,_ in port.calls)
''')


def test_rejected_grant_cannot_create_local_source_permission(tmp_path):
    run_routes(tmp_path, SETUP + r'''    from mindos.stores.ontology_store import OntologyStore
    onto=OntologyStore.instance()
    onto.create_claim({'subject_entity_id':'ent_me','section':'matters','layer':'self_declared',
        'predicate':'working_on','content':'我正在准备合成模型微调项目','confidence':0.99},
        [{'kind':'user_edit','quote':'合成模型微调项目'}],trust_state='confirmed',trust_origin='user_created')
    p=preview('对我的合成模型微调项目有什么看法？')
    assert p['sources'],p
    port.fail_issue=True
    denied=grant(p)
    assert denied.status_code==403,denied.text
    with onto._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM routing_grants').fetchone()[0]==0
    port.fail_issue=False
    partial=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_grant',
        {'revision':p['revision'],'keys':[]},cid)
    assert partial.status_code==409,partial.text
    assert partial.json()['detail']['code']=='CONSENT_ALL_SOURCES_REQUIRED',partial.text
    g=grant(p)
    assert g.status_code==200,g.text
''')


def test_explicit_default_policy_sync_default_grant_and_revoke(tmp_path):
    run_routes(tmp_path, SETUP + r'''    body={'enabled':True,'includeFiles':False,'includeCharter':False,'autoEgress':True,
        'acknowledge':True,'serviceId':'managed-fixture','expectedRevision':0}
    saved=dispatch(client,'put_api_mindos_conversations_conversation_id_routing_default_consent',body,cid)
    assert saved.status_code==200,saved.text
    policy=saved.json()['defaultAuthorization']
    assert policy['revision']==1
    p=preview('默认授权内的合成问题')
    assert p['deConsentRequired'] and p['defaultAuthorization']['applies']
    # Merely preparing an eligible task does not mint a foreground grant.
    assert not any(name=='models.consent.issue' for name,_ in port.calls)
    g=grant(p,defaultPolicyRevision=1)
    assert g.status_code==200,g.text
    issued=[v for n,v in port.calls if n=='models.consent.issue'][-1]
    assert issued['authorization']=={'kind':'default','policyRevision':1}
    assert preview('默认授权内的合成问题')['deConsentRequired'] is False
    revoked=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_revoke',{},cid)
    assert revoked.status_code==200,revoked.text
    assert preview('默认授权内的合成问题')['deConsentRequired'] is True
    assert any(n=='models.consent.revoke' for n,v in port.calls)
    assert any(n=='domain.consent-policy.register' and v['action']=='revoke' and v['policyRevision']==2 for n,v in port.calls)
''')


def test_failed_policy_registration_disables_local_auto_consent(tmp_path):
    run_routes(tmp_path, SETUP + r'''    port.fail_policy=True
    body={'enabled':True,'includeFiles':False,'includeCharter':False,'autoEgress':True,
        'acknowledge':True,'serviceId':'managed-fixture','expectedRevision':0}
    saved=dispatch(client,'put_api_mindos_conversations_conversation_id_routing_default_consent',body,cid)
    assert saved.status_code==503,saved.text
    from mindos.stores.ontology_store import OntologyStore
    from mindos.stores.routing_store import RoutingStore
    policy=RoutingStore(OntologyStore.instance()).policy('global')
    assert policy['enabled'] is False
    assert policy['revision']==2
''')


def test_background_without_registered_task_cannot_mint_foreground_consent(tmp_path):
    run_routes(tmp_path, SETUP + r'''    body={'enabled':True,'includeFiles':False,'includeCharter':False,'autoEgress':True,
        'acknowledge':True,'serviceId':'managed-fixture','expectedRevision':0}
    saved=dispatch(client,'put_api_mindos_conversations_conversation_id_routing_default_consent',body,cid)
    assert saved.status_code==200,saved.text
    from mindos.zhijun.routing import Router, GuardedProvider
    from mindos.zhijun.provider import ChatRequest, build_provider
    from mindos.stores.ontology_store import OntologyStore
    from mindos.stores.conversation_store import ConversationStore
    from fastapi import HTTPException
    router=Router(OntologyStore.instance(),ConversationStore.instance(),ident)
    guard=GuardedProvider(router,build_provider(),'extract_turn',[],background=True)
    try:
        guard.check(ChatRequest('合成后台任务',[{'role':'user','content':'合成内容'}]))
    except HTTPException as exc:
        assert exc.detail['code']=='ROUTE_CONSENT_REQUIRED',exc.detail
        assert exc.detail['preview']['deConsentRequired'] is True
    else:
        raise AssertionError('background task silently minted foreground consent')
    assert not any(n=='models.consent.issue' for n,v in port.calls)
    assert router.store.pending(ident)
''')


def test_registered_background_uses_only_de_registered_standing_policy(tmp_path):
    run_routes(tmp_path, SETUP + r'''    body={'enabled':True,'includeFiles':False,'includeCharter':False,'autoEgress':True,
        'acknowledge':True,'serviceId':'managed-fixture','expectedRevision':0}
    saved=dispatch(client,'put_api_mindos_conversations_conversation_id_routing_default_consent',body,cid)
    assert saved.status_code==200,saved.text
    from mindos.zhijun.routing import Router, GuardedProvider
    from mindos.zhijun.provider import ChatRequest, build_provider
    from mindos.stores.ontology_store import OntologyStore
    from mindos.stores.conversation_store import ConversationStore
    from zhijun_worker.background import register, activated, active_task
    from fastapi import HTTPException
    router=Router(OntologyStore.instance(),ConversationStore.instance(),ident)
    provider=build_provider()
    token=execution.set({'requestId':'background-origin','operationId':'post_api_mindos_conversations_conversation_id_messages'})
    register('task-fixture','extract_turn')
    execution.reset(token)
    registered_policy=port.policy
    port.policy=None
    req=ChatRequest('合成后台任务',[{'role':'user','content':'合成内容'}])
    with activated('task-fixture'):
        try:
            GuardedProvider(router,provider,'extract_turn',[],background=True).check(req)
        except HTTPException as exc:
            assert exc.detail['code']=='ROUTE_CONSENT_REQUIRED',exc.detail
        else:
            raise AssertionError('local-only policy substituted for DE consent')
    port.policy=registered_policy
    with activated('task-fixture'):
        preview=GuardedProvider(router,provider,'extract_turn',[],background=True).check(req)
        assert preview['deConsentRequired'] is False
        from zhijun_worker.consent import find_receipt
        assert find_receipt(preview,provider) is not None
        token=active_task.set('other-task')
        try:
            assert find_receipt(preview,provider) is None
        finally:
            active_task.reset(token)
        origin_token=execution.set({'requestId':'different-origin','operationId':'post_api_mindos_conversations_conversation_id_messages'})
        try:
            assert find_receipt(preview,provider) is None
        finally:
            execution.reset(origin_token)
    assert active_task.get() is None
    assert find_receipt(preview,provider) is None
    assert not any(n=='models.consent.issue' for n,v in port.calls)
    receipts=[p for n,p in port.calls if n=='models.consent.background']
    assert len(receipts)==2
    assert all(p['backgroundId']=='task-fixture' and p['grant']['authorization']=={'kind':'default','policyRevision':1} for p in receipts)
    import hashlib
    from zhijun_worker.consent import canonical
    grant=receipts[-1]['grant']
    ordinary={k:v for k,v in grant.items() if k not in {'grantId','background'}}
    assert grant['grantId']!=hashlib.sha256(canonical(ordinary).encode()).hexdigest()
    background={'id':'task-fixture','executionRequestId':'background-origin',
        'operationId':'post_api_mindos_conversations_conversation_id_messages'}
    assert grant['background']==background
    assert grant['grantId']==hashlib.sha256(canonical({**ordinary,'background':background}).encode()).hexdigest()
    assert grant['grantId']!=hashlib.sha256(canonical({**ordinary,'background':{**background,'id':'other-task'}}).encode()).hexdigest()
''')
