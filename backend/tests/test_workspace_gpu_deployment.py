"""Explicit GPU-only workspace defaults, persisted conflicts and wire budgets."""
import json
import importlib.util
import os
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

import pytest

from mindos import runtime_config_provider as rcp
from mindos.secret_store import MemorySecretStore
from mindos.stores.runtime_settings_store import reset_for_tests
from mindos.zhijun import provider as providers


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(('ZHIJUN_LOCAL_GPU_', 'ZHIJUN_LOCAL_NPU_')):
            monkeypatch.delenv(name)
    monkeypatch.setenv('ZHIJUN_WORKSPACE_ID', 'synthetic-workspace')
    for key, value in rcp.GPU_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    store = reset_for_tests(str(tmp_path / 'runtime.db'))
    secrets = MemorySecretStore()
    return store, secrets


def make(deployment):
    store, secrets = deployment
    return rcp.RuntimeConfigProvider(store=store, secret_store=secrets)


def test_fresh_workspace_gpu_snapshot_and_native_budget(deployment):
    provider = make(deployment)
    local = provider.get_local_snapshot()
    assert (local.base_url, local.model, local.backend, local.context_window, local.max_output_tokens, local.num_thread,
            local.keep_alive, local.configuration_error) == ('http://127.0.0.1:11435', 'qwen3:1.7b', 'ollama_gpu', 4096, 512, 1, 300, None)
    actual = providers.build_provider(provider.get_chat_snapshot())
    body = actual._body(providers.ChatRequest(system='test', messages=[{'role': 'user', 'content': 'hello'}], max_tokens=8192,
                                            json_schema={'type': 'object'}), stream=True)
    assert body['model'] == 'qwen3:1.7b' and body['think'] is False and body['keep_alive'] == 300
    assert body['options']['num_predict'] == 512 and body['options']['num_ctx'] == 4096
    assert body['options']['num_gpu'] == 99 and body['options']['num_thread'] == 1
    assert body['format'] == 'json'


@pytest.mark.parametrize('field,value', [('ZHIJUN_LOCAL_GPU_BASE_URL', 'http://127.0.0.1:11434'),
                                       ('ZHIJUN_LOCAL_GPU_MODEL', 'qwen3:4b'), ('ZHIJUN_LOCAL_GPU_ENABLED', '0'),
                                       ('ZHIJUN_LOCAL_GPU_CONTEXT_WINDOW', '8192'), ('ZHIJUN_LOCAL_GPU_SECRET', 'not-allowed')])
def test_incomplete_or_modified_gpu_contract_refused(deployment, monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises(rcp.ValidationError, match='LOCAL_GPU_DEPLOYMENT_INVALID'):
        make(deployment)


def test_partial_and_ambiguous_gpu_npu_contract_refused(deployment, monkeypatch):
    monkeypatch.delenv('ZHIJUN_LOCAL_GPU_KEEP_ALIVE')
    with pytest.raises(rcp.ValidationError):
        make(deployment)
    monkeypatch.setenv('ZHIJUN_LOCAL_GPU_KEEP_ALIVE', '300')
    monkeypatch.setenv('ZHIJUN_LOCAL_NPU_MODEL', 'old-npu')
    with pytest.raises(rcp.ValidationError):
        make(deployment)


def test_persisted_cpu_setting_blocks_local_without_rewriting_database(deployment):
    store, _ = deployment
    old = {'baseUrl': 'http://127.0.0.1:11434', 'model': 'qwen3:4b', 'timeoutSeconds': 120}
    store.put_section(rcp.SECTION_MATERIAL, None, old, secret_ref=None)
    provider = make(deployment)
    assert provider.get_local_snapshot().configuration_error == 'LOCAL_GPU_DEPLOYMENT_CONFLICT'
    assert store.get_section(rcp.SECTION_MATERIAL)['payload'] == old
    with pytest.raises(providers.ProviderError, match='冲突'):
        providers.build_provider(provider.get_chat_snapshot())
    with pytest.raises(rcp.ValidationError):
        provider.candidate_chat_snapshot(provider='ollama')
    provider.save_material_runtime(base_url='http://127.0.0.1:11435', model='qwen3:1.7b', timeout_seconds=90, expected_revision=1)
    assert provider.get_local_snapshot().configuration_error is None
    assert provider.get_local_snapshot().keep_alive == 300


def test_candidate_and_persisted_cpu_override_rejected(deployment):
    provider = make(deployment)
    for operation in (provider.candidate_local_snapshot, lambda **kw: provider.save_material_runtime(**kw, expected_revision=None)):
        with pytest.raises(rcp.ValidationError):
            operation(base_url='http://127.0.0.1:11434', model='qwen3:1.7b', timeout_seconds=60)
    assert provider.store.get_section(rcp.SECTION_MATERIAL) is None
    candidate = provider.candidate_local_snapshot(timeout_seconds=45)
    assert candidate.backend == 'ollama_gpu' and candidate.max_output_tokens == 512


def test_cloud_choice_survives_local_conflict_and_never_falls_back(deployment):
    store, secrets = deployment
    store.put_section(rcp.SECTION_MATERIAL, None, {'baseUrl': 'http://127.0.0.1:11434', 'model': 'old', 'timeoutSeconds': 60}, secret_ref=None)
    provider = make(deployment)
    provider.save_chat_provider(provider='openai', external_enabled=True, base_url='https://workspace.invalid/v1',
        model='cloud-choice', timeout_seconds=60, total_budget_seconds=90, fallback_ollama=False,
        api_key='synthetic-workspace-only', expected_revision=None)
    snapshot = provider.get_chat_snapshot()
    assert snapshot.provider == 'openai' and snapshot.model == 'cloud-choice' and not snapshot.fallback_ollama
    with patch.object(providers, 'get_provider', return_value=provider):
        actual = providers.build_provider(snapshot)
    assert actual.external and actual.model == 'cloud-choice'


def test_gpu_error_never_tries_other_endpoint_or_cpu_model(deployment):
    provider = make(deployment)
    actual = providers.build_provider(provider.get_chat_snapshot())
    request = providers.ChatRequest(system='test', messages=[{'role': 'user', 'content': 'hello'}])
    with patch.object(providers, '_open', side_effect=providers.ProviderError('GPU unavailable')) as opened:
        with pytest.raises(providers.ProviderError):
            list(actual.stream(request))
    assert opened.call_count == 1 and opened.call_args.args[0] == 'http://127.0.0.1:11435/api/chat'


def test_npu_and_non_gpu_snapshots_keep_existing_semantics(deployment, monkeypatch):
    for key in rcp.GPU_ENVIRONMENT:
        monkeypatch.delenv(key)
    monkeypatch.setenv('ZHIJUN_LOCAL_NPU_BASE_URL', 'http://127.0.0.1:11436')
    monkeypatch.setenv('ZHIJUN_LOCAL_NPU_MODEL', 'qwen3.5:2b')
    provider = make(deployment)
    assert provider.get_local_snapshot().base_url == 'http://127.0.0.1:11436'
    assert provider.get_local_snapshot().max_output_tokens is None
    actual = providers.OllamaProvider('http://127.0.0.1:11434', 'legacy', timeout=60)
    body = actual._body(providers.ChatRequest(system='test', messages=[], max_tokens=2048), stream=False)
    assert body['options']['num_predict'] == 2048 and body['keep_alive'] == 0


def test_actual_product_wire_is_accepted_by_manager_gpu_runtime(deployment):
    """Offline cross-repository contract, exercised in the release workspace."""
    source = Path(__file__).resolve().parents[3] / 'nexusaos-centuarai-os/scripts/centauros_gpu_runtime.py'
    if not source.is_file():
        pytest.skip('CentaurOS checkout required for release cross-contract test')
    spec = importlib.util.spec_from_file_location('qualified_gpu_wire_contract', source)
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    actual = providers.build_provider(make(deployment).get_chat_snapshot())
    for stream in (True, False):
        for schema in (None, {'type': 'object', 'properties': {'answer': {'type': 'string'}}, 'required': ['answer']}):
            request = providers.ChatRequest(system='synthetic test', messages=[{'role': 'user', 'content': 'hello'}],
                                            max_tokens=8192, json_schema=schema)
            wire = actual._body(request, stream=stream)
            accepted = runtime.request_body('/api/chat', wire)
            assert accepted['model'] == 'qwen3:1.7b'
            assert accepted['stream'] is stream and accepted['think'] is False
            assert accepted['options']['num_ctx'] == 4096 and accepted['options']['num_predict'] == 512
            assert accepted['options']['num_gpu'] == 99 and accepted['options']['num_thread'] == 1
            assert wire['keep_alive'] == 300 and accepted['keep_alive'] == -1
            if schema:
                assert accepted['format'] == 'json'
