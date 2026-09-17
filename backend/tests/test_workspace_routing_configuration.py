"""Workspace source consent is bound to the configured model account."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_memory_background_consent import CAPABILITIES
from tests.test_zhijun_worker import CHILD


SCENARIO = r'''
    from fastapi import HTTPException
    from mindos.zhijun import jobs
    from mindos.zhijun.provider import ChatRequest,ProviderError
    from mindos.zhijun.routing import Router,GuardedProvider
    from mindos.stores.ontology_store import OntologyStore
    from mindos.stores.conversation_store import ConversationStore
    from mindos.chat_imports import service_info
    jobs.stop_worker()
    from zhijun_worker.capabilities import execution
    authority=execution.set({'requestId':'synthetic-background', 'operationId':'post_api_mindos_conversations_conversation_id_messages'})
    onto=OntologyStore.instance();convs=ConversationStore.instance()
    cid={'conversationId':ident}
    original_provider=provider_module.build_provider()
    original_service=service_info(original_provider)['id']
    mode=dispatch(client,'put_api_mindos_conversations_conversation_id_routing',
        {'mode':'online','acknowledge':True,'expectedRevision':0,'serviceId':original_service},cid)
    assert mode.status_code==200,mode.text
    message=convs.append_message(ident,'user','我长期认同做决定前尊重当事人的意愿。',meta={'routingSources':[]})
    router=Router(onto,convs,ident)
    refs=[router.ref('message',message['id'])]
    request=ChatRequest('Extract claims',[{'role':'user','content':message['content']}])
    preview=router.prepare('extract_turn',request,refs,original_provider,background=True)
    assert preview['missing'],preview
    assert preview['configurationRevision']==original_provider.configuration_revision
    keys=[s['key'] for s in preview['sources']]
    grant=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_grant',
        {'revision':preview['revision'],'keys':keys},cid)
    assert grant.status_code==200,grant.text
    authorized=router.prepare('extract_turn',request,refs,original_provider,background=True)
    assert not authorized['missing'],authorized
    original_guard=GuardedProvider(router,original_provider,'extract_turn',refs,
                                  revision=authorized['revision'],background=True)
    action=ACTION
    if action=='unchanged':
        assert original_guard.complete_json(request)['claims']
        assert len(generated)==1
        same=router.prepare('extract_turn',request,refs,provider_module.build_provider(),background=True)
        assert same['revision']==authorized['revision']
    else:
        snapshot.secret_ref='rotated-account-secret-ref'
        replacement=provider_module.build_provider()
        assert replacement.model==original_provider.model
        assert replacement.name==original_provider.name
        assert replacement.configuration_revision!=original_provider.configuration_revision
        replacement_service=service_info(replacement)['id']
        assert replacement_service!=original_service
        # A captured explicit grant must not authorize the replacement account.
        old_grant=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_grant',
            {'revision':preview['revision'],'keys':keys},cid)
        assert old_grant.status_code==409,old_grant.text
        try:original_guard.complete_json(request)
        except (HTTPException,ProviderError):pass
        else:raise AssertionError('queued background request used a different model account')
        assert not generated
        # The user can select the replacement service, but old source grants
        # remain scoped to the original account and require a fresh approval.
        switched=dispatch(client,'put_api_mindos_conversations_conversation_id_routing',
            {'mode':'online','acknowledge':True,'expectedRevision':mode.json()['mode']['revision'],
             'serviceId':replacement_service},cid)
        assert switched.status_code==200,switched.text
        fresh_router=Router(onto,convs,ident)
        fresh=fresh_router.prepare('extract_turn',request,refs,replacement,background=True)
        assert fresh['missing'] and fresh['revision']!=authorized['revision'],fresh
        guard=GuardedProvider(fresh_router,replacement,'extract_turn',refs,
                             revision=authorized['revision'],background=True)
        try:guard.complete_json(request)
        except HTTPException as exc:assert exc.detail['code']=='ROUTE_CONSENT_REQUIRED',exc.detail
        else:raise AssertionError('old source grant silently carried to replacement account')
        assert not generated
        new_grant=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_grant',
            {'revision':fresh['revision'],'keys':[s['key'] for s in fresh['sources']]},cid)
        assert new_grant.status_code==200,new_grant.text
        refreshed=fresh_router.prepare('extract_turn',request,refs,replacement,background=True)
        assert not refreshed['missing']
        permitted=GuardedProvider(fresh_router,replacement,'extract_turn',refs,
                                 revision=refreshed['revision'],background=True)
        assert permitted.complete_json(request)['claims']
        assert len(generated)==1
    assert any(name=='models.consent.issue' for name,_ in port.calls)
    assert len([1 for name,_ in port.calls if name=='model.complete_json'])==len(generated)
    execution.reset(authority)
'''


@pytest.mark.parametrize("action", ["unchanged", "rotated"])
def test_workspace_model_account_rotation_requires_new_source_authorization(tmp_path, action):
    backend = Path(__file__).resolve().parents[1]
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    script = CHILD.replace("app=create_app(w)", CAPABILITIES)
    script = script.replace("assert not app.domain.state.active",
        SCENARIO.replace("ACTION", repr(action)) + "\nassert not app.domain.state.active")
    result = subprocess.run([sys.executable, "-c", script, str(root), "configuration-review-owner", ""],
        cwd=backend, env=dict(os.environ, PYTHONPATH=str(backend)), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
