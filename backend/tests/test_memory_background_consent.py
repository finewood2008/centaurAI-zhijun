"""Real isolated workspace recovery of background memory consent; no network."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_zhijun_worker import CHILD


CAPABILITIES = r'''
class MemoryCapabilities:
    def __init__(self):
        self.calls=[]
        self.configuration='a'*64
        self.issued=[]
        self.generated=[]
        self.previews={}
    def call(self,name,payload):
        self.calls.append((name,payload))
        if name=='model.describe':
            return dict(name='openai',model='synthetic-memory',external=not payload.get('localOnly',False),
                        configurationRevision=self.configuration,serviceId='synthetic-memory-service')
        if name=='domain.preview.register':
            preview=payload['preview']
            self.previews[preview['revision']]=preview
            return {'ok':True}
        if name in {'domain.background.register','domain.background.finish',
                    'domain.consent-policy.register','models.consent.revoke'}:
            return {'ok':True}
        if name=='models.consent.issue':
            import time
            self.issued.append(payload)
            return {'consentId':'synthetic-consent-'+str(len(self.issued)),'expiresAt':time.time()+600}
        if name=='model.complete_json':
            assert payload['purpose']=='extract_turn'
            assert payload['localOnly'] is False
            assert payload['consentId'].startswith('synthetic-consent-')
            grant=next(grant for grant in self.issued if grant['grantId']==payload['grantId'])
            assert self.previews[grant['previewRevision']]['request']==payload['request']
            self.generated.append(payload)
            text='我长期认同做决定前尊重当事人的意愿'
            return {'entities':[], 'claims':[{
                'section':'principles','layer':'self_declared','predicate':'holds_principle',
                'subject':'me','object':None,'content':text,'quote':text,'confidence':0.95,
                'scope_hint':'long_term','privacy_hint':'private','merge_into':None,
                'why_it_matters':'讨论合作与个人决定时，先核对当事人的意愿和边界。','date':None}]}
        if name=='materials.evidence':return []
        if name=='retrieval.score':return {}
        raise AssertionError('Unexpected capability '+name)
    def stream(self,name,payload):
        raise AssertionError('Memory extraction must not stream or silently fall back')
port=MemoryCapabilities()
app=create_app(w,port)
'''


SCENARIO = r'''
    from mindos.zhijun import jobs
    from mindos.stores.ontology_store import OntologyStore
    from mindos.stores.conversation_store import ConversationStore
    from mindos.stores.routing_store import RoutingStore
    from zhijun_worker.capabilities import execution
    jobs.stop_worker()  # Deterministic lease execution; actual routing/extraction remain real.
    onto=OntologyStore.instance();convs=ConversationStore.instance();routes=RoutingStore(onto)
    cid={'conversationId':ident}
    mode=dispatch(client,'put_api_mindos_conversations_conversation_id_routing',
        {'mode':'online','acknowledge':True,'expectedRevision':0,'serviceId':'synthetic-memory-service'},cid)
    assert mode.status_code==200,mode.text
    policy=dispatch(client,'put_api_mindos_conversations_conversation_id_routing_default_consent',
        {'enabled':True,'includeFiles':False,'includeCharter':True,'autoEgress':True,
         'acknowledge':True,'serviceId':'synthetic-memory-service','expectedRevision':0},cid)
    assert policy.status_code==200,policy.text
    port.calls.clear()  # Initial settings may describe local availability; the job must not fall back.
    message=convs.append_message(ident,'user','请记住：我长期认同做决定前尊重当事人的意愿。',meta={'routingSources':[]})
    origin=execution.set({'requestId':'synthetic-message-request',
        'operationId':'post_api_mindos_conversations_conversation_id_messages'})
    try:
        jid=jobs.enqueue_extraction(ident,message['id'],store=onto)
    finally:
        execution.reset(origin)

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

    paused=run_claimed()
    assert paused['state']=='paused' and paused['reason']=='consent_required',paused
    saved=onto.get_job(jid)
    assert saved['state']=='done' and saved['result']['state']=='paused',saved
    p={**routes.get_preview(paused['previewId'],ident),'revision':paused['previewId']}
    assert p['deConsentRequired'] is True and p['missing']==[],p
    assert p['defaultAuthorization']['applies'] is True,p
    assert port.generated==[] and port.issued==[],port.calls
    assert onto.list_claims(trust_states=('confirmed','working'))==[]
    assert not any(name=='model.describe' and payload.get('localOnly') for name,payload in port.calls)

    action=ACTION
    if action=='pause_only':
        assert resume()['queuedCount']==1
        assert resume()['queuedCount']==0
        again=run_claimed()
        assert again['state']=='paused' and again['reason']=='consent_required',again
        assert port.issued==[] and port.generated==[]
    else:
        grant_body={'revision':p['revision'],'keys':[s['key'] for s in p['sources']]}
        if action!='explicit':
            grant_body['defaultPolicyRevision']=p['defaultAuthorization']['revision']
        if action=='source_changed_before_grant':
            convs.update_message(message['id'],content='请记住：我长期认同先核对事实再做决定。')
        if action=='configuration_changed_before_grant':
            port.configuration='b'*64
        if action=='policy_changed_before_grant':
            response=dispatch(client,'put_api_mindos_conversations_conversation_id_routing_default_consent',
                {'enabled':False,'expectedRevision':p['defaultAuthorization']['revision']},cid)
            assert response.status_code==200,response.text
        grant=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_grant',grant_body,cid)
        if action.endswith('_before_grant'):
            assert grant.status_code==409,grant.text
            assert port.issued==[] and port.generated==[]
        else:
            assert grant.status_code==200,grant.text
            assert len(port.issued)==1 and port.generated==[]
            assert port.issued[0]['authorization']['kind']==('explicit' if action=='explicit' else 'default')
            if action=='source_changed_after_grant':
                convs.update_message(message['id'],content='请记住：我长期认同先核对事实再做决定。')
            if action=='configuration_changed_after_grant':
                port.configuration='b'*64
            if action=='policy_changed_after_grant':
                response=dispatch(client,'put_api_mindos_conversations_conversation_id_routing_default_consent',
                    {'enabled':False,'expectedRevision':p['defaultAuthorization']['revision']},cid)
                assert response.status_code==200,response.text
            assert resume()['queuedCount']==1
            assert resume()['queuedCount']==0
            resumed=run_claimed()
            if action.endswith('_after_grant'):
                assert resumed['state']=='paused',resumed
                assert port.generated==[]
            else:
                assert resumed['state']=='done' and len(resumed['created'])==1,resumed
                assert len(port.generated)==1
                assert onto.list_claims(trust_states=('confirmed',))==[]
                candidates=onto.list_claims(trust_states=('working',))
                assert len(candidates)==1 and candidates[0]['trustState']=='working',candidates
                assert resume()['queuedCount']==0
                assert len(port.generated)==1
            assert len(port.issued)==1,'resume must not mint any additional consent'
    assert convs.get_message(message['id']) is not None
    assert all(payload['localOnly'] is False for payload in port.generated)
'''


@pytest.mark.parametrize("action", [
    "pause_only", "explicit", "default", "source_changed_before_grant",
    "configuration_changed_before_grant", "policy_changed_before_grant",
    "source_changed_after_grant", "configuration_changed_after_grant", "policy_changed_after_grant",
])
def test_workspace_memory_background_consent(tmp_path, action):
    backend = Path(__file__).resolve().parents[1]
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    script = CHILD.replace("app=create_app(w)", CAPABILITIES)
    extra = SCENARIO.replace("ACTION", repr(action))
    script = script.replace("assert not app.domain.state.active", extra + "\nassert not app.domain.state.active")
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), "synthetic-memory-owner", ""],
        cwd=backend, env=dict(os.environ, PYTHONPATH=str(backend)),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
