"""Real domain CRUD and worker trust boundaries; no user databases or services."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import base64
import hmac

import pytest

from zhijun_worker.auth import DOMAIN, NonceStore, ProofError, sign, verify
from zhijun_worker.workspace import Workspace, WorkspaceLock, secure_read


def workspace(root, account="account-test", epoch=1):
    ident = hashlib.sha256(json.dumps(["device-test", account, epoch], separators=(",", ":")).encode()).hexdigest()
    return Workspace(ident, account, "device-test", epoch, root.resolve(), b"k" * 32, root.resolve() / "worker.sock")


def test_proof_binding_and_replay(tmp_path):
    w = workspace(tmp_path)
    raw = b'{"operationId":"test"}'
    proof = sign(w.key, w.workspace_id, 1, "POST", "/v1/dispatch", raw, now=100)
    nonces = NonceStore()
    assert verify(proof, w.key, w.workspace_id, 1, raw, nonces, now=101)["workspaceId"] == w.workspace_id
    with pytest.raises(ProofError, match="REPLAYED"):
        verify(proof, w.key, w.workspace_id, 1, raw, nonces, now=102)
    for options in ({"body": b"changed"}, {"ownership_epoch": 2}, {"key": b"x" * 32}, {"expected_relative_path": "/internal/zhijun/capabilities"}, {"now": 105}):
        args = dict(header=proof, key=w.key, workspace_id=w.workspace_id, ownership_epoch=1, body=raw, nonces=NonceStore(), now=101)
        args.update(options)
        with pytest.raises(ProofError):
            verify(**args)


def test_nonce_capacity_never_forgets_live_proof():
    store = NonceStore(1)
    store.consume("first", 105, 100)
    with pytest.raises(ProofError, match="CAPACITY"):
        store.consume("second", 105, 101)
    with pytest.raises(ProofError, match="REPLAYED"):
        store.consume("first", 105, 102)
    store.consume("second", 110, 105)


def test_proof_rejects_duplicate_unknown_fields_and_types(tmp_path):
    w = workspace(tmp_path)
    proof = sign(w.key, w.workspace_id, 1, "POST", "/v1/dispatch", b"", now=100)
    original = json.loads(base64.urlsafe_b64decode(proof.split(".")[1] + "=" * (-len(proof.split(".")[1]) % 4)))
    encode = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")
    def header(raw):
        return "v1." + encode(raw) + "." + encode(hmac.digest(w.key, DOMAIN + raw, "sha256"))
    candidates = [{**original, "unexpected": True}, {**original, "v": True},
                  {**original, "exp": 106}, {**original, "nonce": "a"},
                  {**original, "ownershipEpoch": "1"}, {**original, "method": "GET"}]
    raws = [json.dumps(value).encode() for value in candidates]
    raws.append(json.dumps(original).replace('"v": 1', '"v": 1, "v": 1').encode())
    for raw in raws:
        with pytest.raises(ProofError):
            verify(header(raw), w.key, w.workspace_id, 1, b"", NonceStore(), now=101)


def test_config_permissions_symlinks_and_fifo(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    assert secure_read(key, 32) == b"k" * 32
    key.chmod(0o644)
    with pytest.raises(ValueError):
        secure_read(key, 32)
    link = tmp_path / "link"
    link.symlink_to(key)
    with pytest.raises(ValueError):
        secure_read(link, 32)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError):
        secure_read(fifo, 32)


def test_explicit_catalog_path_is_used_and_safely_opened(tmp_path, monkeypatch):
    from zhijun_worker.app import _catalog
    source = tmp_path / "catalog.json"
    source.write_text(json.dumps({"operations": [{"id": "configured-operation", "capability": "domain"}]}))
    source.chmod(0o644)
    monkeypatch.setenv("ZHIJUN_PRODUCT_CATALOG", str(source))
    assert set(_catalog()) == {"configured-operation"}
    source.chmod(0o664)
    with pytest.raises(ValueError, match="CATALOG_FILE_INVALID"):
        _catalog()
    source.chmod(0o600)
    link = tmp_path / "catalog-link.json"
    link.symlink_to(source)
    monkeypatch.setenv("ZHIJUN_PRODUCT_CATALOG", str(link))
    with pytest.raises(ValueError, match="CATALOG_PATH_INVALID"):
        _catalog()
    monkeypatch.setenv("ZHIJUN_PRODUCT_CATALOG", "relative.json")
    with pytest.raises(ValueError, match="CATALOG_PATH_INVALID"):
        _catalog()


def test_lock_and_root_identity_survive_restart(tmp_path):
    tmp_path.chmod(0o700)
    w = workspace(tmp_path)
    first = WorkspaceLock(w)
    first.acquire()
    with pytest.raises(BlockingIOError):
        WorkspaceLock(w).acquire()
    first.close()
    second = WorkspaceLock(w)
    second.acquire()
    second.close()
    with pytest.raises(ValueError, match="SUBJECT_MISMATCH"):
        WorkspaceLock(workspace(tmp_path, account="other-account")).acquire()


CHILD = r'''
import os,json,sys,hashlib
from pathlib import Path
root=Path(sys.argv[1]).resolve();account=sys.argv[2];foreign=sys.argv[3]
secret_root=root.parent/(root.name+'-secrets');secret_root.mkdir(mode=0o700)
wid=hashlib.sha256(json.dumps(['device-test',account,1],separators=(',',':')).encode()).hexdigest()
for key in list(os.environ):
    if key.startswith(('CENTAUR','MINDOS_','ZHIJUN_')):os.environ.pop(key)
os.environ.update(ZHIJUN_WORKSPACE_ID=wid,MINDOS_RUNTIME_ENV='production',MINDOS_LOCAL_WEB_DEBUG_ACCESS='0',
 CENTAURAI_DATABASE_DATA_ROOT=str(root),CENTAUR_SECRET_STORE_DIR=str(secret_root),
 CENTAUR_METADATA_DB=str(root/'db/meta.db'),CENTAUR_GBRAIN_HOME=str(root/'gbrain'),
 CENTAUR_MCP_DATA_DIR=str(root/'mcp/data'),CENTAUR_MCP_CONFIG_DIR=str(root/'mcp/config'))
from zhijun_worker.workspace import Workspace
from zhijun_worker.app import create_app
from zhijun_worker.auth import HEADER,sign
from fastapi.testclient import TestClient
w=Workspace(wid,account,'device-test',1,root,b'k'*32,root/'worker.sock')
app=create_app(w)
def dispatch(client,op,body=None,params=None,proof=True):
    raw=json.dumps(dict(version=1,requestId='request-test',operationId=op,params=params or {},query={},body=body)).encode()
    return client.post('/v1/dispatch',content=raw,headers={HEADER:sign(w.key,wid,1,'POST','/v1/dispatch',raw)} if proof else {})
with TestClient(app) as client:
    assert dispatch(client,'post_api_mindos_conversations',{'title':'private'},proof=False).status_code==401
    assert client.get('/api/mindos/conversations').status_code==404
    assert dispatch(client,'get_api_mindos_materials').status_code==400
    assert dispatch(client,[]).status_code==400
    assert client.post('/v1/dispatch',content=b'x'*(1048576+1),headers={HEADER:'invalid'}).status_code==413
    created=dispatch(client,'post_api_mindos_conversations',{'title':account})
    assert created.status_code==200,created.text
    ident=created.json()['id']
    got=dispatch(client,'get_api_mindos_conversations_conversation_id',params={'conversationId':ident})
    assert got.status_code==200,got.text
    if foreign:
        assert dispatch(client,'get_api_mindos_conversations_conversation_id',params={'conversationId':foreign}).status_code==404
    changed=dispatch(client,'patch_api_mindos_conversations_conversation_id',{'title':'edited','expectedRevision':0},{'conversationId':ident})
    assert changed.status_code==200,changed.text
    conflict=dispatch(client,'patch_api_mindos_conversations_conversation_id',{'title':'stale','expectedRevision':0},{'conversationId':ident})
    assert conflict.status_code==409,conflict.text
assert not app.domain.state.active
from mindos.zhijun.jobs import OntologyWorker
assert not OntologyWorker.instance().running
assert 'server' not in sys.modules
assert 'watcher' not in sys.modules
assert 'vector_store' not in sys.modules
assert not (root/'chroma_data').exists()
print(json.dumps({'id':ident,'catalog':len(app.catalog),'closed':True}))
'''


def test_real_domain_crud_two_processes_and_clean_shutdown(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    previous = ""
    for account in ("account-one", "account-two"):
        root = tmp_path / account
        root.mkdir(mode=0o700)
        env = dict(os.environ, PYTHONPATH=str(backend), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-c", CHILD, str(root), account, previous], env=env,
                                cwd=backend, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        output = json.loads(result.stdout.strip().splitlines()[-1])
        previous = output["id"]
        assert output["catalog"] >= 90 and output["closed"]
