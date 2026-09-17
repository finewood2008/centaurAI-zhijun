"""Product factories must use the same DE authority as workspace model settings."""
from types import SimpleNamespace

import pytest

from mindos import chat_imports
from mindos.zhijun import provider, routing
from zhijun_worker import capabilities, model, consent


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setenv("ZHIJUN_WORKSPACE_ID", "a" * 64)
    monkeypatch.setenv("ZHIJUN_PROVIDER", "fake")
    calls = []
    state = {"name": "openai", "model": "managed-model", "external": True,
             "configurationRevision": "revision-1", "serviceId": "managed-service"}

    def call(name, payload):
        calls.append((name, payload))
        if name == "model.describe":
            return ({**state, "name": "ollama", "model": "local-model", "external": False,
                     "serviceId": "local-service"} if payload["localOnly"] else dict(state))
        assert name == "model.complete_json"
        return {"answer": "synthetic"}

    def stream(name, payload):
        calls.append((name, payload))
        assert name == "model.stream"
        yield {"type": "text", "text": "synthetic"}
        yield {"type": "done"}

    monkeypatch.setattr(model, "require", lambda: SimpleNamespace(call=call, stream=stream))
    def forbidden():
        raise AssertionError("worker settings and secrets are no longer model authority")
    monkeypatch.setattr(provider, "get_provider", forbidden)
    monkeypatch.setattr("mindos.runtime_config_provider.get_provider", forbidden)
    return state, calls


@pytest.mark.parametrize("ambient_override", ["fake", "anthropic", "ollama", "openai"])
def test_workspace_factory_reads_fresh_selection_without_local_key(remote, monkeypatch, ambient_override):
    monkeypatch.setenv("ZHIJUN_PROVIDER", ambient_override)
    state, calls = remote
    first = provider.build_provider()
    assert isinstance(first, model.CapabilityProvider)
    assert first.model == "managed-model" and first.external
    assert chat_imports.service_info(first)["id"] == "managed-service"
    state.update(model="new-selection", configurationRevision="revision-2")
    second = provider.build_provider()
    assert second.model == "new-selection" and second.configuration_revision == "revision-2"
    assert calls == [("model.describe", {"localOnly": False})] * 2


def test_workspace_factory_rejects_old_snapshot_without_capability_call(remote):
    with pytest.raises(provider.ProviderError) as error:
        provider.build_provider(SimpleNamespace(provider="openai", model="stale"))
    assert error.value.code == "WORKSPACE_MODEL_SNAPSHOT_FORBIDDEN"
    assert remote[1] == []


def test_explicit_local_factory_uses_de_local_only_without_worker_settings(remote):
    selected = chat_imports.local_provider(num_ctx=8192, timeout=3)
    assert isinstance(selected, model.CapabilityProvider)
    assert not selected.external and selected.model == "local-model"
    assert remote[1] == [("model.describe", {"localOnly": True})]
    events = list(selected.stream(provider.ChatRequest("", [{"role": "user", "content": "synthetic"}])))
    assert isinstance(events[-1], provider.Done)
    assert remote[1][-1][1]["localOnly"] is True


@pytest.mark.parametrize("code,status", [("WORKER_EXECUTION_REQUIRED", 401), ("BROKER_UNAVAILABLE", 503),
                                        ("BROKER_LEASE_EXPIRED", 503), ("BROKER_PROVIDER_DENIED", 403), ("PROVIDER_MISCONFIGURED", 503)])
@pytest.mark.parametrize("factory", [provider.build_provider, chat_imports.local_provider])
def test_capability_errors_never_fall_back_to_worker_http(remote, monkeypatch, code, status, factory):
    def denied():
        raise capabilities.CapabilityError(code, status)
    monkeypatch.setattr(model, "require", denied)
    with pytest.raises(provider.ProviderError) as error:
        factory()
    assert error.value.code == code and error.value.status_code == status


def test_local_factory_rejects_external_capability_response(remote, monkeypatch):
    monkeypatch.setattr(model, "require", lambda: SimpleNamespace(call=lambda *args: remote[0]))
    with pytest.raises(provider.ProviderError) as error:
        chat_imports.local_provider()
    assert error.value.code == "CAPABILITY_LOCAL_MODEL_REQUIRED"


def test_real_factory_stream_requires_receipt_and_uses_capability_revision(remote, monkeypatch):
    selected = provider.build_provider()
    request = provider.ChatRequest("", [{"role": "user", "content": "synthetic"}])
    with pytest.raises(provider.ProviderError) as error:
        list(selected.stream(request))
    assert error.value.code == "EGRESS_NOT_AUTHORIZED"
    assert all(name == "model.describe" for name, _ in remote[1])
    preview = {"purpose": "chat", "sources": []}
    def receipt(actual, actual_provider):
        assert actual is preview and actual_provider is selected
        return {"grantId": "synthetic-grant", "consentId": "synthetic-consent"}
    monkeypatch.setattr(consent, "receipt", receipt)
    token = routing.EGRESS_PERMIT.set(lambda: preview)
    try:
        events = list(selected.stream(request))
        assert isinstance(events[-1], provider.Done)
        assert selected.complete_json(request) == {"answer": "synthetic"}
    finally:
        routing.EGRESS_PERMIT.reset(token)
    for name, payload in remote[1][1:]:
        assert name in {"model.stream", "model.complete_json"}
        assert payload["configurationRevision"] == "revision-1"
        assert payload["consentId"] == "synthetic-consent"
        assert "apiKey" not in payload and "baseUrl" not in payload
