"""Legacy runtime settings adapter and secret-store isolation.

The product catalog now routes model settings to DE. The explicit adapter fixture
here retains migration/old database behavior; it is not the production route."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


CHILD = r'''
import hashlib, json, os, sqlite3, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
account, action, foreign = sys.argv[2:5]
secret_root = root.parent / (root.name + '-secrets')
secret_root.mkdir(mode=0o700, exist_ok=True)
wid = hashlib.sha256(json.dumps(['device-model-test', account, 1], separators=(',', ':')).encode()).hexdigest()
for key in list(os.environ):
    if key.startswith(('CENTAUR', 'MINDOS_', 'ZHIJUN_')):
        os.environ.pop(key)
os.environ.update(ZHIJUN_WORKSPACE_ID=wid, MINDOS_RUNTIME_ENV='production', MINDOS_LOCAL_WEB_DEBUG_ACCESS='0',
    CENTAURAI_DATABASE_DATA_ROOT=str(root), CENTAUR_SECRET_STORE_DIR=str(secret_root),
    CENTAUR_METADATA_DB=str(root/'db/meta.db'), CENTAUR_GBRAIN_HOME=str(root/'gbrain'),
    CENTAUR_MCP_DATA_DIR=str(root/'mcp/data'), CENTAUR_MCP_CONFIG_DIR=str(root/'mcp/config'),
    CENTAUR_QA_AI_PROVIDER='openai', CENTAUR_QA_AI_BASE_URL='https://global.invalid/v1',
    CENTAUR_QA_AI_MODEL='global-model', CENTAUR_QA_AI_API_KEY='synthetic-global-never-use',
    CENTAUR_QA_AI_EXTERNAL_ENABLED='true')
from fastapi.testclient import TestClient
from zhijun_worker.workspace import Workspace
from zhijun_worker.app import create_app
from zhijun_worker.auth import HEADER, sign
from mindos import model_runtime as api, runtime_config_provider as rcp, external_model_discovery
class NoDE:
    def call(self, *args):
        raise AssertionError('chat settings must not call DE')
    stream = call
w = Workspace(wid, account, 'device-model-test', 1, root, b'k'*32, root/'worker.sock')
app = create_app(w, NoDE())
# Retain the old adapter's signed-dispatch and storage regressions explicitly.
# Production never publishes these domain routes: the catalog is DE-owned.
operations = json.loads((Path.cwd().parent/'frontend/shared/product-operations.json').read_text())['operations']
legacy = {item['id']: item for item in operations
          if item['path'].startswith(('/api/system/models/chat-provider', '/api/system/models/external-providers'))}
assert len(legacy) == 9 and all(item['capability']=='models' for item in legacy.values())
assert not set(legacy) & set(app.catalog)
app.catalog.update({key: {**item, 'capability':'domain'} for key,item in legacy.items()})
def dispatch(client, op, body=None, params=None, proof=True, query=None):
    raw = json.dumps(dict(version=1, requestId='model-settings-test', operationId=op,
        params=params or {}, query=query or {}, body=body)).encode()
    response = client.post('/v1/dispatch', content=raw,
        headers={HEADER: sign(w.key, wid, 1, 'POST', '/v1/dispatch', raw)} if proof else {})
    assert 'synthetic-global-never-use' not in response.text
    assert 'secret_ref' not in response.text
    assert 'synthetic-'+account not in response.text
    return response
with TestClient(app) as client:
    provider = rcp.get_provider()
    assert provider is api.get_runtime_provider()
    assert provider.store._db_path == root/'db/runtime_settings.db'
    assert provider._secret_store._db_path == secret_root/'model-secrets.db'
    routes = {(method, route.path) for route in api.build_workspace_router(lambda: None).routes if hasattr(route, 'methods')
              for method in route.methods if route.path.startswith('/api/system/models')}
    assert len(routes) == 9
    assert not any(any(term in path for term in ('material-runtime', '/jobs', '/monitor', '/health')) for method, path in routes)
    assert dispatch(client, 'get_api_system_models_chat_provider', proof=False).status_code == 401
    assert client.get('/api/system/models/chat-provider').status_code == 404
    assert dispatch(client, 'get_api_system_models_material_runtime').status_code == 400
    if foreign:
        response = dispatch(client, 'post_api_system_models_external_providers_provider_id_models',
            {'revision': 1}, {'providerId': foreign})
        assert response.status_code == 404, response.text
    state = dispatch(client, 'get_api_system_models_chat_provider').json()
    if action == 'create':
        assert state['configurationRequired'] and not state['apiKeyConfigured'], state
        assert state['baseUrl'] is None and state['model'] is None
        assert provider.resolve_api_key(provider.get_chat_snapshot()) is None
        assert provider.get_local_snapshot().base_url == ''
        assert provider.get_local_snapshot().model == ''
        invalid = dispatch(client, 'post_api_system_models_external_providers',
            {'name': 'A', 'baseUrl': 'https://workspace.invalid/v1', 'apiKey': ['synthetic-'+account]})
        assert invalid.status_code == 422, invalid.text
        candidate = dispatch(client, 'post_api_system_models_chat_provider_test', {})
        assert candidate.status_code == 400, candidate.text
        created = dispatch(client, 'post_api_system_models_external_providers',
            {'name': account, 'baseUrl': 'https://workspace.invalid/v1', 'apiKey': 'synthetic-'+account})
        assert created.status_code == 200, created.text
        profile = created.json()
        def discover(base, key):
            assert base == 'https://workspace.invalid/v1' and key == 'synthetic-'+account
            return ['workspace-model']
        external_model_discovery.discover_models = discover
        models = dispatch(client, 'post_api_system_models_external_providers_provider_id_models',
            {'revision': profile['revision']}, {'providerId': profile['id']})
        assert models.status_code == 200 and models.json()['models'] == ['workspace-model'], models.text
        activated = dispatch(client, 'post_api_system_models_external_providers_provider_id_activate',
            {'revision': profile['revision'], 'model': 'workspace-model', 'chatRevision': 0}, {'providerId': profile['id']})
        assert activated.status_code == 200, activated.text
        assert not activated.json()['chat']['configurationRequired']
        assert activated.json()['chat']['apiKeyHint'] == '••••'
        conflict = dispatch(client, 'post_api_system_models_external_providers_provider_id_activate',
            {'revision': profile['revision'], 'model': 'workspace-model', 'chatRevision': 0}, {'providerId': profile['id']})
        assert conflict.status_code == 409, conflict.text
        edited = dispatch(client, 'put_api_system_models_chat_provider',
            {'provider': 'openai', 'externalEnabled': True, 'baseUrl': 'https://workspace.invalid/v1',
             'model': 'workspace-model', 'timeoutSeconds': 80, 'totalBudgetSeconds': 100,
             'fallbackOllama': False, 'revision': 1})
        assert edited.status_code == 200 and edited.json()['revision'] == 2, edited.text
    else:
        assert not state['configurationRequired'] and state['apiKeyConfigured'], state
        assert state['revision'] == 2 and state['timeoutSeconds'] == 80
        assert state['model'] == 'workspace-model'
        profile = dispatch(client, 'get_api_system_models_external_providers').json()['providers'][0]
    assert provider.resolve_api_key(provider.get_chat_snapshot()) == 'synthetic-'+account
    with sqlite3.connect(root/'db/runtime_settings.db') as db:
        assert db.execute('SELECT count(*) FROM encrypted_secrets').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM secret_store_metadata').fetchone()[0] == 0
    assert 'server' not in sys.modules
    print(json.dumps({'id': profile['id']}))
'''


def test_legacy_settings_adapter_two_workspaces_and_restart(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    foreign = ""
    for account, action in (("a", "create"), ("b", "create"), ("a", "reopen")):
        root = tmp_path / account
        root.mkdir(mode=0o700, exist_ok=True)
        result = subprocess.run(
            [sys.executable, "-c", CHILD, str(root), account, action, foreign],
            env=dict(os.environ, PYTHONPATH=str(backend), PYTHONDONTWRITEBYTECODE="1"),
            cwd=backend, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        foreign = json.loads(result.stdout.strip().splitlines()[-1])["id"]


def test_restored_secret_reference_requires_reentry_without_de_lookup(tmp_path, monkeypatch):
    from mindos import runtime_config_provider as rcp
    from mindos.secret_store import EncryptedSQLiteSecretStore, MemorySecretStore
    from mindos.stores.runtime_settings_store import RuntimeSettingsStore

    monkeypatch.setenv("ZHIJUN_WORKSPACE_ID", "isolated-workspace")
    monkeypatch.delenv("ZHIJUN_LOCAL_NPU_BASE_URL", raising=False)
    monkeypatch.delenv("ZHIJUN_LOCAL_NPU_MODEL", raising=False)
    monkeypatch.setattr(rcp.config, "QA_AI_API_KEY", "synthetic-global-key")
    store = RuntimeSettingsStore(tmp_path / "runtime.db")
    # Simulate a restored business DB that contains old encrypted credentials.
    legacy_secrets = EncryptedSQLiteSecretStore(tmp_path / "runtime.db")
    legacy = rcp.RuntimeConfigProvider(store, legacy_secrets)
    profile = legacy.save_external_provider(name="restored", base_url="https://restored.invalid/v1", api_key="synthetic-old-key")
    legacy.activate_external_provider(profile["id"], expected_revision=1, model="restored-model", chat_revision=0)
    provider = rcp.RuntimeConfigProvider(store, MemorySecretStore())
    assert not provider.get_chat_snapshot().api_key_configured
    assert provider.resolve_api_key(provider.get_chat_snapshot()) is None
    assert not provider.list_external_providers()["providers"][0]["apiKeyConfigured"]
    with pytest.raises(rcp.ValidationError, match="重新保存"):
        provider.activate_external_provider(profile["id"], expected_revision=2, model="restored-model", chat_revision=1)
    with pytest.raises(rcp.ValidationError, match="必须配置 API Key"):
        provider.save_chat_provider(provider="openai", external_enabled=True, base_url="https://restored.invalid/v1",
            model="restored-model", timeout_seconds=60, total_budget_seconds=90, fallback_ollama=False, expected_revision=1)


def test_workspace_local_service_requires_explicit_npu_configuration(tmp_path, monkeypatch):
    from mindos import runtime_config_provider as rcp
    from mindos.secret_store import MemorySecretStore
    from mindos.stores.runtime_settings_store import RuntimeSettingsStore

    monkeypatch.setenv("ZHIJUN_WORKSPACE_ID", "isolated-workspace")
    monkeypatch.setenv("ZHIJUN_LOCAL_NPU_BASE_URL", "http://127.0.0.1:18124/")
    monkeypatch.setenv("ZHIJUN_LOCAL_NPU_MODEL", "configured-npu-model")
    monkeypatch.setattr(rcp.config, "LOCAL_OLLAMA_URL", "http://wrong-global.invalid:11434")
    monkeypatch.setattr(rcp.config, "LOCAL_OLLAMA_MODEL", "wrong-global-model")
    provider = rcp.RuntimeConfigProvider(RuntimeSettingsStore(tmp_path / "runtime.db"), MemorySecretStore())
    local = provider.get_chat_snapshot().local
    assert local.base_url == "http://127.0.0.1:18124"
    assert local.model == "configured-npu-model"


def _maintenance_workspace(tmp_path):
    import hashlib
    from zhijun_worker.workspace import Workspace, WorkspaceLock

    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    account, device, epoch = "maintenance-account", "maintenance-device", 1
    wid = hashlib.sha256(json.dumps([device, account, epoch], separators=(",", ":")).encode()).hexdigest()
    workspace = Workspace(wid, account, device, epoch, root, bytes(32), root / "worker.sock")
    lock = WorkspaceLock(workspace)
    lock.acquire()
    lock.close()
    return workspace


def _maintenance_cli(workspace, *options):
    backend = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, "-m", "zhijun_worker.configure_local_model", "--data-root", str(workspace.data_root),
         "--workspace-id", workspace.workspace_id, *options],
        env=dict(os.environ, PYTHONPATH=str(backend), PYTHONDONTWRITEBYTECODE="1"), cwd=backend,
        capture_output=True, text=True, timeout=10,
    )


def test_offline_npu_configuration_persists_and_checks_revisions(tmp_path, monkeypatch):
    from mindos import runtime_config_provider as rcp
    from mindos.secret_store import MemorySecretStore
    from mindos.stores.runtime_settings_store import RuntimeSettingsStore

    workspace = _maintenance_workspace(tmp_path)
    show = _maintenance_cli(workspace, "--show")
    assert show.returncode == 0, show.stdout + show.stderr
    assert json.loads(show.stdout)["localRevision"] == 0
    args = ("--npu-base-url", "http://127.0.0.1:18124", "--npu-model", "npu-configured-model", "--revision", "0")
    stale_chat = _maintenance_cli(workspace, *args, "--activate", "--chat-revision", "99")
    assert stale_chat.returncode == 2
    assert json.loads(_maintenance_cli(workspace, "--show").stdout)["localRevision"] == 0
    saved = _maintenance_cli(workspace, *args, "--activate", "--chat-revision", "0")
    assert saved.returncode == 0, saved.stdout + saved.stderr
    assert json.loads(saved.stdout)["restartWorkerRequired"]
    assert _maintenance_cli(workspace, *args).returncode == 2
    monkeypatch.setenv("ZHIJUN_WORKSPACE_ID", workspace.workspace_id)
    provider = rcp.RuntimeConfigProvider(RuntimeSettingsStore(workspace.data_root / "db/runtime_settings.db"), MemorySecretStore())
    snap = provider.get_chat_snapshot()
    assert snap.provider == "ollama" and not snap.external_enabled
    assert snap.local.base_url == "http://127.0.0.1:18124"
    assert snap.local.model == "npu-configured-model"
    assert not snap.fallback_ollama


def test_offline_local_only_configuration_is_visible_to_chat_after_restart(tmp_path, monkeypatch):
    from mindos import runtime_config_provider as rcp
    from mindos.secret_store import MemorySecretStore
    from mindos.stores.runtime_settings_store import RuntimeSettingsStore, SECTION_CHAT

    workspace = _maintenance_workspace(tmp_path)
    saved = _maintenance_cli(workspace, '--npu-base-url', 'http://127.0.0.1:18124',
        '--npu-model', 'explicit-npu', '--revision', '0')
    assert saved.returncode == 0, saved.stdout + saved.stderr
    assert not json.loads(saved.stdout)['activated']
    monkeypatch.setenv('ZHIJUN_WORKSPACE_ID', workspace.workspace_id)
    monkeypatch.delenv('ZHIJUN_LOCAL_NPU_BASE_URL', raising=False)
    monkeypatch.delenv('ZHIJUN_LOCAL_NPU_MODEL', raising=False)
    store = RuntimeSettingsStore(workspace.data_root / 'db/runtime_settings.db')
    assert store.get_section(SECTION_CHAT) is None
    provider = rcp.RuntimeConfigProvider(store, MemorySecretStore())
    assert provider.get_chat_snapshot().local == provider.get_local_snapshot()
    assert provider.get_chat_snapshot().local.model == 'explicit-npu'


def test_offline_configuration_rejects_live_worker_wrong_identity_and_symlinks(tmp_path):
    from dataclasses import replace
    from zhijun_worker.workspace import WorkspaceLock

    workspace = _maintenance_workspace(tmp_path)
    lock = WorkspaceLock(workspace)
    lock.acquire()
    try:
        result = _maintenance_cli(workspace, "--show")
        assert result.returncode == 2
        assert json.loads(result.stdout)["code"] == "WORKER_RUNNING"
    finally:
        lock.close()
    assert _maintenance_cli(replace(workspace, workspace_id="f" * 64), "--show").returncode == 2
    link = tmp_path / "linked-workspace"
    link.symlink_to(workspace.data_root)
    assert _maintenance_cli(replace(workspace, data_root=link), "--show").returncode == 2
    workspace.data_root.chmod(0o755)
    assert _maintenance_cli(workspace, "--show").returncode == 2
