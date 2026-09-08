"""Synthetic capability fixtures exercise the real domain routes and protocol adapters."""
import json
import os
from pathlib import Path
import subprocess
import sys
import io
from types import SimpleNamespace

import pytest

from zhijun_worker.capabilities import CapabilityError, HttpCapabilities, execution
from zhijun_worker.model import CapabilityProvider
from tests.test_zhijun_worker import CHILD, workspace


def test_default_consent_policy_registration_uses_exact_revisioned_protocol(monkeypatch):
    from mindos import routing_routes
    from zhijun_worker import capabilities

    class Port:
        def __init__(self):
            self.calls = []

        def call(self, name, payload):
            self.calls.append((name, payload))
            return {"registered": True, "revision": payload["policyRevision"]}

    port = Port()
    monkeypatch.setenv("ZHIJUN_WORKSPACE_ID", "workspace-fixture")
    monkeypatch.setattr(capabilities, "require", lambda: port)
    policy = {"enabled": True, "autoEgress": True, "revision": 4,
              "service": "service-fixture", "purposes": ["chat"],
              "includeFiles": False, "includeCharter": True}
    provider = SimpleNamespace(configuration_revision="a" * 64)
    routing_routes._register_default_policy(policy, provider)
    routing_routes._register_default_policy({**policy, "enabled": False, "revision": 5})
    routing_routes._register_default_policy({**policy, "revision": 5}, action="revoke", key="claim:one")
    assert port.calls == [
        ("domain.consent-policy.register", {
            "action": "enable", "policyRevision": 4, "serviceId": "service-fixture",
            "configurationRevision": "a" * 64, "purposes": ["chat"],
            "includeFiles": False, "includeCharter": True, "autoEgress": True,
        }),
        ("domain.consent-policy.register", {"action": "disable", "policyRevision": 5}),
        ("domain.consent-policy.register", {
            "action": "revoke", "key": "claim:one", "policyRevision": 5,
        }),
    ]


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
        if name=='model.describe':return dict(name='ollama',model='fixture-only',external=False,configurationRevision='r1',serviceId='test-local')
        if name in {'domain.preview.register','domain.background.register','domain.background.finish'}:return {'ok':True}
        if name=='materials.evidence':return []
        if name=='retrieval.score':return {}
        raise AssertionError('Unimplemented fixture capability '+name)
    def stream(self,name,payload):
        assert name=='model.stream'
        from zhijun_worker.capabilities import execution
        assert execution.get()['requestId']=='request-test'
        assert execution.get()['operationId']=='post_api_mindos_conversations_conversation_id_messages'
        yield {'type':'text','text':'隔离模型端口测试回答。'}
        yield {'type':'usage','input_tokens':1,'output_tokens':2}
        yield {'type':'done','stop_reason':'stop'}
app=create_app(w,SyntheticCapabilities())
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


def test_real_online_preview_grant_and_exact_prompt_receipt(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    fixture = CAPABILITY_FIXTURE.replace("external=False", "external=not payload.get('localOnly',False)")
    fixture = fixture.replace("        if name=='materials.evidence':return []", """        if name=='models.consent.issue':
            import time
            assert set(payload)=={'previewRevision','conversationId','purpose','serviceId','configurationRevision','selectedSources','authorization','grantId'}
            assert payload['authorization']=={'kind':'explicit'}
            return {'consentId':'synthetic-de-consent','expiresAt':time.time()+600}
        if name=='models.consent.revoke':return {'revoked':True}
        if name=='materials.evidence':return []""")
    script = CHILD.replace("app=create_app(w)", fixture)
    extra = '''    cid={'conversationId':ident}
    mode=dispatch(client,'put_api_mindos_conversations_conversation_id_routing',
        {'mode':'online','acknowledge':True,'expectedRevision':0,'serviceId':'test-local'},cid)
    assert mode.status_code==200,mode.text
    content='明确授权的本次测试问题'
    preview=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_preview',{'content':content},cid)
    assert preview.status_code==200,preview.text
    p=preview.json()
    assert p['deConsentRequired'] is True
    grant=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_grant',
        {'revision':p['revision'],'keys':[s['key'] for s in p['sources']]},cid)
    assert grant.status_code==200,grant.text
    from zhijun_worker.consent import receipt
    from zhijun_worker.model import CapabilityProvider
    provider=CapabilityProvider()
    assert receipt(p,provider)['consentId']=='synthetic-de-consent'
    altered={**p,'request':{**p['request'],'system':'changed'}}
    try:receipt(altered,provider)
    except Exception as exc:assert str(exc)=='MODEL_EGRESS_CONSENT_REQUIRED'
    else:raise AssertionError('different prompt reused consent')
    fresh=dispatch(client,'post_api_mindos_conversations_conversation_id_routing_preview',{'content':content},cid).json()
    assert fresh['deConsentRequired'] is False
    result=dispatch(client,'post_api_mindos_conversations_conversation_id_messages',
        {'content':content,'routeRevision':fresh['revision']},cid)
    assert result.status_code==200,result.text
    assert 'event: message_done' in result.text,result.text
    assert 'event: error' not in result.text,result.text
'''
    script = script.replace("assert not app.domain.state.active", extra + "assert not app.domain.state.active")
    result = subprocess.run([sys.executable, "-c", script, str(root), "test-owner", ""],
                            cwd=backend, env=dict(os.environ, PYTHONPATH=str(backend)), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_chat_attachment_uses_trusted_upload_handle(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    fixture = CAPABILITY_FIXTURE.replace("        if name=='materials.evidence':return []", """        if name=='uploads.describe':return {'fileName':'sample.txt','size':11}
        if name=='materials.ingest':
            assert payload=={'uploadId':'a'*32,'attachmentPurpose':'chat'}
            return {'materialId':'material-fixture','versionNumber':1}
        if name=='materials.processing':return {'record':{'versionNumber':1,'status':'available'},'hasText':True,'jobState':'done','snapshotId':'snap-central'}
        if name=='materials.evidence':return []""")
    script = CHILD.replace("app=create_app(w)", fixture)
    extra = '''    cid={'conversationId':ident}
    batch=dispatch(client,'post_api_mindos_conversations_conversation_id_imports',
        {'requestId':'import-request-0001','files':[{'id':'file-fixture-0001','name':'sample.txt','size':11}]},cid)
    assert batch.status_code==200,batch.text
    target={**cid,'batchId':batch.json()['id'],'fileId':'file-fixture-0001'}
    operation='post_api_mindos_conversations_conversation_id_imports_batch_id_files_file_id'
    bad=dispatch(client,operation,{'kind':'multipart','fields':{},'files':[{'field':'file','uploadId':'/tmp/fake'}]},target)
    assert bad.status_code==400,bad.text
    uploaded=dispatch(client,operation,{'kind':'multipart','fields':{},'files':[{'field':'file','uploadId':'a'*32}]},target)
    assert uploaded.status_code==200,uploaded.text
    assert uploaded.json()['state']=='ready'
    from mindos.stores.chat_import_store import ChatImportStore
    assert 'material-fixture' in ChatImportStore().protected_ids()
    ref={'materialId':'material-fixture','version':1}
    ChatImportStore().grant([ref],'test-service')
    assert ChatImportStore().allowed(ref,'test-service','snap-central')
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
    fixture = CAPABILITY_FIXTURE.replace("        if name=='materials.evidence':return []", """        if name=='materials.get':return {'materialId':payload['materialId'],'versionNumber':1,'status':'available'}
        if name=='materials.read_ref':
            return {'record':{'versionNumber':1},'snapshot':{'snapshot_id':'safe-snapshot','privacyEpoch':1,'redactionVersion':'safe-v1'},
                    'text':'林岚现任远川科技产品负责人。新流程X 将替代旧流程Y。'}
        if name=='model.complete_json':
            assert payload['localOnly'] is True
            assert payload['request']['messages'][0]['content']=='林岚现任远川科技产品负责人。新流程X 将替代旧流程Y。'
            assert payload['request']['debug']['sourceRef']['redactionVersion']=='safe-v1'
            from tests.test_zhijun_materials import RELATION_RECORD
            return {'relations':[{key:value for key,value in item.items() if key!='relationId'} for item in RELATION_RECORD['content']['items']]}
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
    assert len(first.json()['jobIds'])==1,first.text
    changed=event('a'*32,'material.purged',value)
    assert changed.status_code==409,changed.text
    wrong=event('b'*32,'material.ready',value,target='/v1/dispatch')
    assert wrong.status_code==401,wrong.text
    job=store.claim_next_job('test-runner',120)
    assert job['kind']=='extract_material'
    OntologyWorker.instance().process(job,'test-runner',store=store,conv_store=ConversationStore.instance())
    assert len(store.list_claims(trust_states=('working',)))==2,store.get_job(job['jobId'])
    evidence=store.list_claims(trust_states=('working',))[0]['evidence'][0]
    assert evidence['locator']['snapshotId']=='safe-snapshot'
    assert evidence['locator']['privacyEpoch']==1
    assert evidence['locator']['version']==1
    assert evidence['locator']['redactionVersion']=='safe-v1'
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
