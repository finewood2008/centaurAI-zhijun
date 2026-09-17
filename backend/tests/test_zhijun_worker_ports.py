"""Synthetic capability fixtures exercise the real domain routes and protocol adapters."""
import json
import os
from pathlib import Path
import subprocess
import sys
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from zhijun_worker.capabilities import CapabilityError, HttpCapabilities, execution
from zhijun_worker.model import CapabilityProvider
from tests.test_zhijun_worker import CHILD, workspace


def test_default_consent_policy_stays_in_workspace_without_de_registration(monkeypatch):
    from mindos import routing_routes
    from zhijun_worker import capabilities

    port = Mock(side_effect=AssertionError("consent must stay in workspace"))
    monkeypatch.setenv("ZHIJUN_WORKSPACE_ID", "workspace-fixture")
    monkeypatch.setattr(capabilities, "require", port)
    store = Mock()
    store.policy.return_value = {"service": "service-fixture", "serviceName": "fixture"}
    router = SimpleNamespace(store=store, scope="workspace-fixture")
    provider = SimpleNamespace(name="openai", model="fixture", external=True,
                               service_id="service-fixture", configuration_revision="a" * 64)
    monkeypatch.setattr(routing_routes, "router_for", lambda *args: router)
    monkeypatch.setattr(routing_routes, "build_provider", lambda: provider)
    monkeypatch.setattr(routing_routes, "state", lambda *args: {"saved": True})
    req = routing_routes.DefaultConsent(enabled=True, autoEgress=True, acknowledge=True,
        includeCharter=True, serviceId="service-fixture", expectedRevision=4)
    assert routing_routes.set_default_consent("conversation", req, None) == {"saved": True}
    assert store.set_policy.call_args.kwargs["configuration_revision"] == "a" * 64
    routing_routes.revoke("conversation", routing_routes.Revoke(key="claim:one"), None)
    store.revoke.assert_called_once_with("workspace-fixture", "claim:one")
    port.assert_not_called()


def test_capability_url_and_execution_binding(tmp_path):
    w = workspace(tmp_path)
    for url in ("https://127.0.0.1", "http://example.com", "http://user@127.0.0.1", "http://127.0.0.1/arbitrary", "http://127.0.0.1/?x=1"):
        with pytest.raises(ValueError):
            HttpCapabilities(w, url)
    port = HttpCapabilities(w, "http://127.0.0.1:1")
    class Opener:
        def open(self, request, timeout):
            parsed = json.loads(request.data)
            assert parsed == {"name": "materials.get", "payload": {"materialId": "one"},
                              "executionRequestId": "trusted-request", "operationId": "trusted-operation"}
            assert request.full_url == "http://127.0.0.1:1/internal/zhijun/capabilities"
            return object()
    port.opener = Opener()
    token = execution.set({"requestId": "trusted-request", "operationId": "trusted-operation"})
    try:
        port._request("materials.get", {"materialId": "one"}, "/internal/zhijun/capabilities")
    finally:
        execution.reset(token)


def test_only_safe_material_read_has_large_response_budget(tmp_path, monkeypatch):
    port = HttpCapabilities(workspace(tmp_path), "http://127.0.0.1:1")
    encoded = json.dumps({"ok": True, "result": {"text": "x" * (2 * 1024 * 1024)}}).encode()
    monkeypatch.setattr(port, "_request", lambda *args: io.BytesIO(encoded))
    assert len(port.call("materials.read_ref", {})["text"]) == 2 * 1024 * 1024
    with pytest.raises(CapabilityError, match="CAPABILITY_RESPONSE_TOO_LARGE"):
        port.call("materials.get", {})


def test_model_provider_fails_closed_and_stream_contract(monkeypatch):
    from zhijun_worker import model
    from mindos.zhijun.provider import ChatRequest, ProviderError, TextDelta, Done
    class Port:
        external = False
        events = [{"type": "text", "text": "分段"}, {"type": "done", "stop_reason": "stop"}]
        def call(self, name, payload):
            assert name == "model.describe"
            return dict(name="ollama", model="synthetic-test", external=self.external,
                        configurationRevision="r1", serviceId="local-test")
        def stream(self, name, payload):
            assert name == "model.stream" and payload["configurationRevision"] == "r1"
            return iter(self.events)
    port = Port()
    monkeypatch.setattr(model, "require", lambda: port)
    request = ChatRequest("system", [{"role": "user", "content": "test"}])
    provider = CapabilityProvider(local_only=True)
    assert list(provider.stream(request)) == [TextDelta("分段"), Done("stop")]
    port.events = [{"type": "error", "code": "MODEL_RESPONSE_EMPTY"}]
    with pytest.raises(ProviderError, match="没有返回可显示的正文") as empty:
        list(provider.stream(request))
    assert empty.value.code == "MODEL_RESPONSE_EMPTY"
    port.events = [{"type": "error", "code": "MODEL_PROVIDER_FAILED"}]
    with pytest.raises(ProviderError, match="本地模型服务没有运行或尚未就绪") as unavailable:
        list(provider.stream(request))
    assert unavailable.value.code == "MODEL_PROVIDER_FAILED"
    port.events = [{"type": "error", "code": "MODEL_TOTAL_TIMEOUT"}]
    with pytest.raises(ProviderError, match="本地模型等待超时") as timeout:
        list(provider.stream(request))
    assert timeout.value.code == "MODEL_TOTAL_TIMEOUT"
    port.events = [{"type": "text", "text": "partial"}]
    with pytest.raises(ProviderError, match="模型能力调用失败"):
        list(provider.stream(request))
    port.external = True
    with pytest.raises(CapabilityError):
        CapabilityProvider(local_only=True)
    provider = CapabilityProvider()
    with pytest.raises(ProviderError) as exc:
        list(provider.stream(request))
    assert exc.value.code == "EGRESS_NOT_AUTHORIZED"


@pytest.mark.parametrize("failure", [None, "invented_quote", "privacy_epoch", "safe_body", "snapshot", "empty_text"])
def test_material_understanding_uses_safe_text_and_rechecks_fence(monkeypatch, failure):
    from zhijun_worker import material_understanding as understanding, model
    class CanonicalFixture:
        reads = 0
        generated = False
        raw_source = "RAW_SECRET_NEVER_SENT"
        safe = "团队甲正在支持项目乙。" + "经过安全处理的上下文。" * 600
        def call(self, name, payload):
            if name == "materials.read_ref":
                self.reads += 1
                assert payload == {"materialId": "material-safe", "version": 3}
                return {"record": {"versionNumber": 3}, "snapshot": {
                    "snapshot_id": "changed" if self.generated and failure == "snapshot" else "snapshot-safe",
                    "privacyEpoch": 5 if self.generated and failure == "privacy_epoch" else 4,
                    "redactionVersion": "safe-attempt-2"},
                    "text": self.safe + ("变化" if self.generated and failure == "safe_body" else "")}
            if name == "model.describe":
                assert payload == {"localOnly": True}
                return dict(name="ollama", model="protocol-fixture", external=False,
                            configurationRevision="a" * 64, serviceId="local-fixed")
            if name == "model.complete_json":
                assert payload["localOnly"] is True
                assert payload["request"]["messages"] == [{"role": "user", "content": self.safe[:4000]}]
                assert self.raw_source not in json.dumps(payload, ensure_ascii=False)
                assert payload["request"]["json_schema"]["properties"]["relations"]["maxItems"] == 20
                assert payload["request"]["debug"]["truncated"] is True
                self.generated = True
                return {"relations": [{"subject": {"name": "团队甲", "type": "organization"}, "predicate": "支持",
                    "object": {"name": "项目乙", "type": "project"}, "confidence": 0.9,
                    "evidence": "团队甲支持不存在的关系" if failure == "invented_quote" else "团队甲正在支持项目乙。"}]}
            raise AssertionError("Unexpected capability " + name)
    port = CanonicalFixture()
    if failure == "empty_text":
        port.safe = ""
    monkeypatch.setattr(understanding, "require", lambda: port)
    monkeypatch.setattr(model, "require", lambda: port)
    if failure:
        with pytest.raises(CapabilityError) as exc:
            understanding.extract("material-safe", 3)
        assert exc.value.code == ("MATERIAL_MODEL_EVIDENCE_INVALID" if failure == "invented_quote" else "MATERIAL_SAFE_TEXT_EMPTY" if failure == "empty_text" else "MATERIAL_SAFE_SNAPSHOT_CHANGED")
    else:
        entities, relations, fence, report = understanding.extract("material-safe", 3)
        assert len(entities["content"]["items"]) == 2
        assert relations["content"]["items"][0]["locator"]["privacyEpoch"] == 4
        assert fence["version"] == 3 and port.reads == 2
        assert report["truncated"] is True and report["analyzedChars"] == 4000
        assert report["generatedRelations"] == 1 and report["external"] is False


CAPABILITY_FIXTURE = r'''
class SyntheticCapabilities:
    def call(self,name,payload):
        assert not name.startswith(('model.','models.consent.','domain.preview.','domain.consent-policy.')),name
        if name in {'domain.background.register','domain.background.finish'}:return {'ok':True}
        if name=='materials.evidence':return []
        if name=='retrieval.score':return {}
        raise AssertionError('Unimplemented fixture capability '+name)
    def stream(self,name,payload):
        raise AssertionError('No model stream may pass through DE: '+name)
app=create_app(w,SyntheticCapabilities())
from types import SimpleNamespace
from mindos import runtime_config_provider
from mindos.zhijun import provider as direct_provider
from mindos.zhijun.routing import EGRESS_PERMIT
from mindos.chat_imports import service_info
import io
local=SimpleNamespace(base_url='http://127.0.0.1:18134',model='fixture-npu',timeout_seconds=3,keep_alive=0,context_window=4096)
snapshot=SimpleNamespace(provider='openai',external_enabled=True,base_url='https://fixture.invalid/v1',
    model='fixture-online',timeout_seconds=3,external_provider_id='fixture-account',secret_ref='workspace-fixture-secret',local=local)
original_runtime_factory=runtime_config_provider.get_provider
def fixture_runtime():
    runtime=original_runtime_factory()
    runtime.get_chat_snapshot=lambda:snapshot
    runtime.resolve_api_key=lambda snap:'fixture-test-key'
    return runtime
runtime_config_provider.get_provider=fixture_runtime
direct_provider.get_provider=fixture_runtime
model_requests=[]
def direct_transport(url,*,channel,**kwargs):
    payload=json.loads(kwargs['data'])
    assert payload['model'] in {'fixture-npu','fixture-online'}
    assert url in {'http://127.0.0.1:18134/api/chat','https://fixture.invalid/v1/chat/completions'}
    if channel=='chat':
        assert callable(EGRESS_PERMIT.get())
        EGRESS_PERMIT.get()()
        assert kwargs['headers']['Authorization']=='Bearer fixture-test-key'
    model_requests.append(payload)
    if payload.get('stream'):
        from zhijun_worker.capabilities import execution
        assert execution.get()['requestId']=='request-test'
        assert execution.get()['operationId']=='post_api_mindos_conversations_conversation_id_messages'
        if channel=='chat':
            response='data: '+json.dumps({'choices':[{'delta':{'content':'隔离模型端口测试回答。'},'finish_reason':'stop'}]})+'\ndata: [DONE]\n'
        else:
            response=json.dumps({'message':{'content':'隔离模型端口测试回答。'},'done':True,'done_reason':'stop'})+'\n'
    else:
        content=json.dumps({'claims':[],'entities':[]})
        response=json.dumps({'choices':[{'message':{'content':content}}]} if channel=='chat' else {'message':{'content':content}})
    return io.BytesIO(response.encode())
direct_provider.llm_transport.allowed_urlopen=direct_transport
def online_service():
    return service_info(direct_provider.build_provider())['id']
'''


def test_real_domain_sse_via_authenticated_dispatch(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    script = CHILD.replace("app=create_app(w)", CAPABILITY_FIXTURE)
    extra = '''    result=dispatch(client,'post_api_mindos_conversations_conversation_id_messages',
        {'content':'你好，这是一条隔离测试消息','localOnly':True}, {'conversationId':ident})
    assert result.status_code==200,result.text
    assert 'text/event-stream' in result.headers['content-type']
    assert '隔离模型端口测试回答' in result.text,result.text
    assert 'event: message_done' in result.text,result.text
    assert 'event: error' not in result.text,result.text
'''
    script = script.replace("assert not app.domain.state.active", extra + "assert not app.domain.state.active")
    result = subprocess.run([sys.executable, "-c", script, str(root), "test-owner", ""],
                            cwd=backend, env=dict(os.environ, PYTHONPATH=str(backend)),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_online_preview_grant_and_default_policy_stay_in_workspace(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    script = CHILD.replace("app=create_app(w)", CAPABILITY_FIXTURE)
    extra = '''    cid={'conversationId':ident}
    mode=dispatch(client,'put_api_mindos_conversations_conversation_id_routing',
        {'mode':'online','acknowledge':True,'expectedRevision':0,'serviceId':online_service()},cid)
    assert mode.status_code==200,mode.text
    content='明确授权的本次测试问题'
    preview=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_preview',{'content':content},cid)
    assert preview.status_code==200,preview.text
    p=preview.json()
    assert 'deConsentRequired' not in p
    assert 'deConsentExpiresAt' not in p
    grant=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_grant',
        {'revision':p['revision'],'keys':[s['key'] for s in p['sources']]},cid)
    assert grant.status_code==200,grant.text
    fresh=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_preview',{'content':content},cid).json()
    assert 'deConsentRequired' not in fresh
    result=dispatch(client,'post_api_mindos_conversations_conversation_id_messages',
        {'content':content,'routeRevision':fresh['revision']},cid)
    assert result.status_code==200,result.text
    assert 'event: message_done' in result.text,result.text
    assert 'event: error' not in result.text,result.text
    assert any(request['model']=='fixture-online' and request['stream'] for request in model_requests)
    from mindos.routing_routes import DefaultConsent,set_default_consent,Revoke,revoke
    from starlette.requests import Request
    request=Request({'type':'http','headers':[],'state':{'device_scope':'test-owner'}})
    # Exercise the real SQLite policy write with the direct provider's opaque revision.
    from mindos.zhijun.routing import Router
    from mindos.stores.ontology_store import OntologyStore
    from mindos.stores.conversation_store import ConversationStore
    from mindos import routing_routes
    router=Router(OntologyStore.instance(),ConversationStore.instance(),ident)
    routing_routes.router_for=lambda *args:router
    policy_revision=router.store.policy(router.scope)['revision']
    saved=set_default_consent(ident,DefaultConsent(enabled=True,autoEgress=True,acknowledge=True,
        serviceId=online_service(),expectedRevision=policy_revision),request)
    revision=saved['defaultAuthorization']['configurationRevision']
    assert isinstance(revision,str) and len(revision)==64,revision
    assert 'workspace-fixture-secret' not in json.dumps(saved)
    assert router.store.policy(router.scope)['configurationRevision']==revision
    assert revoke(ident,Revoke(),request)['revoked'] is True
'''
    script = script.replace("assert not app.domain.state.active", extra + "assert not app.domain.state.active")
    result = subprocess.run([sys.executable, "-c", script, str(root), "test-owner", ""],
                            cwd=backend, env=dict(os.environ, PYTHONPATH=str(backend)), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_chat_attachment_retrieval_only_still_validates_trusted_upload_handle(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    fixture = CAPABILITY_FIXTURE.replace("        if name=='materials.evidence':return []", """        if name in ('uploads.describe','uploads.read','materials.get','materials.read_ref'):
            raise AssertionError('retrieval-only route reached material capability')
        if name=='materials.evidence':return []""")
    fixture = fixture.replace("app=create_app(w,SyntheticCapabilities())", """from zhijun_worker import data_agent_rag_v2
def no_material_client(*args,**kwargs):
    raise AssertionError('retrieval-only import attempted Data Agent upload or polling')
data_agent_rag_v2.configured_client=no_material_client
app=create_app(w,SyntheticCapabilities())""")
    script = CHILD.replace("app=create_app(w)", fixture)
    extra = '''    cid={'conversationId':ident}
    batch=dispatch(client,'post_api_mindos_conversations_conversation_id_imports',
        {'requestId':'import-request-0001','files':[{'id':'file-fixture-0001','name':'sample.txt','size':11}]},cid)
    assert batch.status_code==409,batch.text
    assert batch.json()['detail']['code']=='RAG_RETRIEVAL_ONLY',batch.text
    from mindos.stores.chat_import_store import ChatImportStore
    assert ChatImportStore().batches(ident)==[]
    target={**cid,'batchId':'imp_historical0001','fileId':'file-fixture-0001'}
    operation='post_api_mindos_conversations_conversation_id_imports_batch_id_files_file_id'
    bad=dispatch(client,operation,{'kind':'multipart','fields':{},'files':[{'field':'file','uploadId':'/tmp/fake'}]},target)
    assert bad.status_code==400,bad.text
    uploaded=dispatch(client,operation,{'kind':'multipart','fields':{},'files':[{'field':'file','uploadId':'a'*32}]},target)
    assert uploaded.status_code==409,uploaded.text
    assert uploaded.json()['detail']['code']=='RAG_RETRIEVAL_ONLY',uploaded.text
    assert 'material-fixture' not in ChatImportStore().protected_ids()
    ref={'materialId':'material-fixture','version':1}
    from fastapi import HTTPException
    try:
        ChatImportStore().grant([ref],'test-service')
    except HTTPException as exc:
        assert exc.status_code==409 and exc.detail['code']=='ATTACHMENT_VERSION_CHANGED'
    else:
        raise AssertionError('unregistered material must not receive a legacy grant')
    assert not ChatImportStore().allowed(ref,'test-service','rag-v2:material-fixture:1')
    assert not ChatImportStore().allowed({**ref,'version':2},'test-service')
    assert 'mindos.stores.material_pipeline_store' not in sys.modules
    assert 'mindos.services.ingestion' not in sys.modules
'''
    script = script.replace("assert not app.domain.state.active", extra + "assert not app.domain.state.active")
    result = subprocess.run([sys.executable, "-c", script, str(root), "test-owner", ""], cwd=backend,
                            env=dict(os.environ, PYTHONPATH=str(backend)), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_trusted_material_events_are_idempotent_and_detach_real_claims(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    fixture = CAPABILITY_FIXTURE.replace("        if name=='materials.evidence':return []", """        if name in ('materials.get','materials.read_ref','model.complete_json'):
            raise AssertionError('material-ready event must not automatically read or infer')
        if name=='materials.evidence':return []""")
    script = CHILD.replace("app=create_app(w)", fixture)
    extra = '''    from mindos.zhijun.jobs import OntologyWorker
    from mindos.stores.ontology_store import OntologyStore
    from mindos.stores.conversation_store import ConversationStore
    import time
    OntologyWorker.instance().stop()
    store=OntologyStore.instance()
    def event(ident,kind,payload,target='/v1/events'):
        value={'eventId':ident,'type':kind,'payload':payload,'executionRequestId':'system-test-0001','operationId':'system_material_event'}
        raw=json.dumps(value).encode()
        return client.post(target,content=raw,headers={HEADER:sign(w.key,wid,1,'POST','/v1/events',raw)})
    value={'materialId':'mat:event.legacy-01','version':1}
    first=event('a'*32,'material.ready',value)
    assert first.status_code==200,first.text
    duplicate=event('a'*32,'material.ready',value)
    assert duplicate.json()['duplicate'] is True,duplicate.text
    assert first.json()['jobIds']==[],first.text
    assert first.json()['state']=='skipped',first.text
    assert first.json()['code']=='RAG_RETRIEVAL_ONLY',first.text
    changed=event('a'*32,'material.purged',value)
    assert changed.status_code==409,changed.text
    wrong=event('b'*32,'material.ready',value,target='/v1/dispatch')
    assert wrong.status_code==401,wrong.text
    assert not store.list_claims(trust_states=('working',))
    # Older material-derived records remain valid audit objects. Seed them
    # directly, without invoking the retired automatic material reader.
    for number in range(2):
        store.create_claim({'subject_entity_id':'ent_me','section':'matters','layer':'observed',
                            'predicate':'happened','content':'历史资料观察'+str(number),'confidence':0.8},
            [{'kind':'material_span','material_id':value['materialId'],'quote':'历史证据'+str(number)}],
            trust_state='working',trust_origin='material')
    assert len(store.list_claims(trust_states=('working',)))==2
    purged=event('c'*32,'material.purged',value)
    assert purged.status_code==200,purged.text
    assert len(store.list_claims(trust_states=('working',)))==0
    reviews=len(store.review_events(limit=100))
    assert event('c'*32,'material.purged',value).json()['duplicate'] is True
    assert len(store.review_events(limit=100))==reviews
    tick=event('d'*32,'domain.tick',{'scheduledAt':int(time.time())//60})
    assert tick.status_code==200,tick.text
    second=event('e'*32,'domain.tick',{'scheduledAt':int(time.time())//60})
    assert second.json()['jobIds']==[],second.text
'''
    script = script.replace("assert not app.domain.state.active", extra + "assert not app.domain.state.active")
    result = subprocess.run([sys.executable, "-c", script, str(root), "test-owner", ""], cwd=backend,
                            env=dict(os.environ, PYTHONPATH=str(backend)), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
