"""Saved provider profiles and bounded discovery, with synthetic credentials/transport."""
import io
import json
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from mindos import model_runtime as api, runtime_config_provider as rcp
from mindos.external_model_discovery import discover_models, DiscoveryError, MAX_BYTES
from mindos.secret_store import EncryptedSQLiteSecretStore, MemorySecretStore
from mindos.stores.runtime_settings_store import RuntimeSettingsStore, RevisionConflictError, SECTION_CHAT


class ExternalProvidersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.db"
        self.store = RuntimeSettingsStore(self.path)
        self.secrets = MemorySecretStore()
        self.provider = rcp.RuntimeConfigProvider(self.store, self.secrets)
        self.provider._schedule_secret_cleanup = Mock()
        api.set_runtime_provider(self.provider)
        api.configure_guards(lambda: None)
        app = FastAPI()
        app.include_router(api.router)
        api.install_error_handlers(app)
        self.client = TestClient(app)
        self.url = "/api/system/models/external-providers"

    def tearDown(self):
        api.set_runtime_provider(None)
        self.tmp.cleanup()

    def create(self, name="Synthetic A", base="https://a.invalid/v1", key="synthetic-secret-A"):
        response = self.client.post(self.url, json={"name": name, "baseUrl": base, "apiKey": key})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(key, response.text)
        self.assertNotIn("secret_ref", response.text)
        return response.json()

    def activate(self, profile, chat_revision=0, model="model-a"):
        return self.client.post(self.url + "/" + profile["id"] + "/activate",
            json={"revision": profile["revision"], "chatRevision": chat_revision, "model": model})

    def test_create_is_inactive_get_is_readonly_and_activate_is_explicit(self):
        item = self.create()
        self.assertFalse(item["active"])
        self.assertIsNone(self.store.get_section(SECTION_CHAT))
        before = self.store.list_external_profiles()
        self.assertEqual(self.client.get(self.url).json()["chatRevision"], 0)
        self.assertEqual(self.store.list_external_profiles(), before)
        response = self.activate(item)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["chat"]["externalEnabled"])
        self.assertFalse(response.json()["chat"]["fallbackOllama"])
        self.assertEqual(self.provider.get_chat_snapshot().external_provider_id, item["id"])

    def test_two_profiles_switch_without_deleting_either_key(self):
        a, b = self.create(), self.create("B", "https://b.invalid/v1", "synthetic-secret-B")
        self.assertEqual(self.activate(a).status_code, 200)
        self.assertEqual(self.activate(b, 1, "model-b").status_code, 200)
        state = self.client.get(self.url).json()
        self.assertEqual(state["activeProviderId"], b["id"])
        self.assertEqual(len(state["providers"]), 2)
        self.assertEqual(self.provider.resolve_api_key(self.provider.get_chat_snapshot()), "synthetic-secret-B")
        self.assertEqual(self.provider.cleanup_orphan_secrets(0), [])
        self.assertEqual(len(self.secrets.list_secret_refs()), 2)

    def test_delayed_cleanup_rechecks_inactive_profile_reference(self):
        a, b = self.create(), self.create("B", "https://b.invalid/v1", "synthetic-B")
        self.activate(a)
        old_ref = self.store.get_external_profile(a["id"])["secret_ref"]
        self.activate(b, 1)
        with patch("mindos.runtime_config_provider.time.sleep"), \
                patch("mindos.runtime_config_provider.threading.Thread", side_effect=lambda target, daemon: SimpleNamespace(start=target)):
            rcp.RuntimeConfigProvider._schedule_secret_cleanup(self.provider, old_ref, 0)
        self.assertEqual(self.secrets.get_secret(old_ref), "synthetic-secret-A")

    def test_profile_store_isolation_and_failed_commit_compensates_only_new_key(self):
        self.create()
        separate = RuntimeSettingsStore(Path(self.tmp.name) / "separate.db")
        self.assertEqual(separate.list_external_profiles(), [])
        before = set(self.secrets.list_secret_refs())
        with patch.object(self.store, "put_external_profile", side_effect=RevisionConflictError({"revision": 2})):
            with self.assertRaises(RevisionConflictError):
                self.provider.save_external_provider(name="B", base_url="https://b.invalid", api_key="synthetic-rollback")
        self.assertEqual(set(self.secrets.list_secret_refs()), before)

    def test_edit_active_profile_is_pending_until_explicit_activation(self):
        a = self.activate(self.create()).json()["provider"]
        update = self.client.put(self.url + "/" + a["id"], json={"revision": a["revision"], "name": "A edited",
            "baseUrl": "https://a.invalid/v1", "apiKey": "synthetic-new-key", "model": "new-model"})
        self.assertEqual(update.status_code, 200, update.text)
        self.assertTrue(update.json()["pendingActivation"])
        self.assertEqual(self.provider.get_chat_snapshot().model, "model-a")
        self.assertEqual(self.provider.resolve_api_key(self.provider.get_chat_snapshot()), "synthetic-secret-A")
        self.provider.cleanup_orphan_secrets(0)
        self.assertEqual(len(self.secrets.list_secret_refs()), 2)
        self.assertEqual(self.activate(update.json(), 1, "new-model").status_code, 200)
        self.assertEqual(self.provider.resolve_api_key(self.provider.get_chat_snapshot()), "synthetic-new-key")

    def test_legacy_timeout_and_pause_preserve_selected_profile_and_pending_revision(self):
        active = self.activate(self.create()).json()["provider"]
        self.provider.save_external_provider(ident=active["id"], expected_revision=active["revision"],
            name="saved but not activated", base_url=active["baseUrl"], model="pending-model")
        self.provider.save_chat_provider(provider="openai", external_enabled=False, base_url=active["baseUrl"],
            model="model-a", timeout_seconds=80, total_budget_seconds=120, fallback_ollama=False, expected_revision=1)
        snap = self.provider.get_chat_snapshot()
        self.assertEqual(snap.external_provider_id, active["id"])
        self.assertFalse(snap.external_enabled)
        self.assertEqual(snap.model, "model-a")
        self.assertEqual(self.store.get_section(SECTION_CHAT)["payload"]["externalProviderRevision"], active["revision"])
        fresh = rcp.RuntimeConfigProvider(RuntimeSettingsStore(self.path), self.secrets)
        self.assertEqual(fresh.get_chat_snapshot().external_provider_id, active["id"])
        self.assertTrue(fresh.list_external_providers()["providers"][0]["pendingActivation"])

    def test_blank_key_keeps_same_endpoint_but_never_crosses_endpoint(self):
        a = self.create()
        url = self.url + "/" + a["id"]
        body = {"revision": 1, "name": "A", "baseUrl": "https://b.invalid/v1", "apiKey": ""}
        self.assertEqual(self.client.put(url, json=body).status_code, 400)
        body["baseUrl"] = "https://a.invalid/v1/"
        self.assertEqual(self.client.put(url, json=body).status_code, 200)
        self.assertEqual(len(self.secrets.list_secret_refs()), 1)

    def test_activate_double_cas_does_not_change_profile_on_chat_conflict(self):
        a, b = self.create(), self.create("B", "https://b.invalid/v1", "synthetic-B")
        self.assertEqual(self.activate(a).status_code, 200)
        self.assertEqual(self.activate(b).status_code, 409)
        self.assertEqual(self.store.get_external_profile(b["id"])["revision"], 1)
        self.assertEqual(self.activate({**b, "revision": 99}, 1).status_code, 409)
        self.assertEqual(self.provider.get_chat_snapshot().external_provider_id, a["id"])

    def test_inactive_delete_allowed_active_delete_rejected(self):
        a, b = self.create(), self.create("B", "https://b.invalid/v1", "synthetic-B")
        active = self.activate(a).json()["provider"]
        self.assertEqual(self.client.delete(f"{self.url}/{active['id']}?revision={active['revision']}").status_code, 409)
        self.assertEqual(self.client.delete(f"{self.url}/{b['id']}?revision=99").status_code, 409)
        self.assertEqual(self.client.delete(f"{self.url}/{b['id']}?revision=1").status_code, 200)
        self.assertIsNone(self.store.get_external_profile(b["id"]))

    def test_migration_imports_legacy_once_keeps_key_and_chat_revision(self):
        self.provider.save_chat_provider(provider="openai", external_enabled=True, base_url="https://legacy.invalid/v1", model="legacy-model",
            timeout_seconds=60, total_budget_seconds=90, fallback_ollama=False, api_key="legacy-synthetic-key", expected_revision=0)
        ref = self.store.get_section(SECTION_CHAT)["secret_ref"]
        migrated = RuntimeSettingsStore(self.path)
        self.assertEqual(len(migrated.list_external_profiles()), 1)
        profile = migrated.list_external_profiles()[0]
        self.assertEqual(profile["secret_ref"], ref)
        self.assertEqual(migrated.get_section(SECTION_CHAT)["revision"], 1)
        self.assertEqual(self.secrets.get_secret(ref), "legacy-synthetic-key")
        self.assertEqual(len(RuntimeSettingsStore(self.path).list_external_profiles()), 1)

    def test_reopen_active_profiles_never_imports_duplicate_legacy_profile(self):
        a = self.create()
        self.activate(a)
        reopened = RuntimeSettingsStore(self.path)
        self.assertEqual([r["id"] for r in reopened.list_external_profiles()], [a["id"]])
        fresh = rcp.RuntimeConfigProvider(reopened, self.secrets)
        self.assertEqual(fresh.get_chat_snapshot().external_provider_id, a["id"])

    def test_encrypted_persistence_redacts_both_database_and_api(self):
        encrypted = EncryptedSQLiteSecretStore(self.path)
        provider = rcp.RuntimeConfigProvider(self.store, encrypted)
        profile = provider.save_external_provider(name="Private", base_url="https://private.invalid/v1", api_key="do-not-leak-synthetic-token")
        with self.store._connect() as db:
            dump = "\n".join(db.iterdump())
        self.assertNotIn("do-not-leak-synthetic-token", dump)
        again = rcp.RuntimeConfigProvider(RuntimeSettingsStore(self.path), EncryptedSQLiteSecretStore(self.path))
        row = again.store.get_external_profile(profile["id"])
        self.assertEqual(again._secret_store.get_secret(row["secret_ref"]), "do-not-leak-synthetic-token")
        self.assertNotIn(row["secret_ref"], json.dumps(again.list_external_providers()))

    def test_concurrent_activations_only_one_wins(self):
        profiles = [self.create(), self.create("B", "https://b.invalid/v1", "synthetic-B")]
        def activate(profile):
            store = RuntimeSettingsStore(self.path)
            try:
                store.activate_external_profile(profile["id"], 1, "model", 0, {})
                return "ok"
            except RevisionConflictError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(activate, profiles)), ["conflict", "ok"])
        self.assertEqual(self.store.get_section(SECTION_CHAT)["revision"], 1)

    def test_models_revision_race_is_rejected_without_changing_chat(self):
        a = self.create()
        def fetch(*args):
            self.provider.save_external_provider(ident=a["id"], expected_revision=1, name="changed", base_url=a["baseUrl"])
            return ["model-a"]
        with patch("mindos.external_model_discovery.discover_models", side_effect=fetch):
            response = self.client.post(f"{self.url}/{a['id']}/models", json={"revision": 1})
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.store.get_section(SECTION_CHAT))

    def test_validation_errors_never_echo_token_and_stale_update_no_secret_leak(self):
        response = self.client.post(self.url, json={"name": "A", "baseUrl": "https://a.invalid", "apiKey": {"secret": "DO_NOT_ECHO"}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("DO_NOT_ECHO", response.text)
        a = self.create()
        before = self.secrets.list_secret_refs()
        response = self.client.put(f"{self.url}/{a['id']}", json={"name": "A", "baseUrl": a["baseUrl"], "apiKey": "OTHER_SECRET", "revision": 99})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("OTHER_SECRET", response.text)
        self.assertEqual(self.secrets.list_secret_refs(), before)


class DiscoveryTests(unittest.TestCase):
    def open(self, payload):
        response = io.BytesIO(payload)
        return patch("mindos.external_model_discovery.urllib.request.build_opener", return_value=SimpleNamespace(open=Mock(return_value=response)))

    def test_fixed_get_normalized_endpoint_only_current_token_and_valid_ids(self):
        with self.open(b'{"data":[{"id":"model-b"},{"id":"model-a"},{"id":"model-a"},{"id":"bad model"}]}') as factory:
            self.assertEqual(discover_models("https://synthetic.invalid/v1/", "SYNTHETIC_KEY"), ["model-a", "model-b"])
        request = factory.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://synthetic.invalid/v1/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer SYNTHETIC_KEY")

    def test_forbidden_hosts_and_malformed_urls_never_open(self):
        for url in ("https://claude.ai", "https://x.anthropic.com", "https://ANTHROPIC.COM.", "https://anthropic。com",
                    "https://ａｎｔｈｒｏｐｉｃ.com", "https://user:password@a.invalid", "https://a.invalid/v1?key=secret", "https://a.invalid/%2e%2e"):
            with self.subTest(url=url), patch("mindos.external_model_discovery.urllib.request.build_opener") as opened:
                with self.assertRaises(rcp.ValidationError):
                    discover_models(url, "SYNTHETIC")
                opened.assert_not_called()

    def test_redirect_handler_never_reissues_authorization(self):
        from mindos.llm_transport import _NoModelRedirect
        import urllib.request
        request = urllib.request.Request("https://synthetic.invalid/v1/models", headers={"Authorization": "Bearer SYNTHETIC"})
        with self.assertRaises(urllib.error.HTTPError):
            _NoModelRedirect().redirect_request(request, io.BytesIO(), 302, "found", {}, "https://other.invalid/models")

    def test_slow_drip_uses_read1_and_enforces_total_deadline(self):
        response = SimpleNamespace(read=Mock(side_effect=AssertionError("read(n) can wait indefinitely")),
            read1=Mock(return_value=b" "), close=Mock())
        with patch("mindos.external_model_discovery.urllib.request.build_opener", return_value=SimpleNamespace(open=Mock(return_value=response))), \
                patch("mindos.external_model_discovery.time.monotonic", side_effect=[0, 0, 10, 10, 21]):
            with self.assertRaises(DiscoveryError) as caught:
                discover_models("https://synthetic.invalid/v1", "SYNTHETIC")
        self.assertEqual(caught.exception.code, "models_timeout")
        self.assertEqual(response.read1.call_count, 2)
        response.read.assert_not_called()
        response.close.assert_called_once()

    def test_oversized_invalid_timeout_and_server_errors_are_sanitized(self):
        for payload in (b"x" * (MAX_BYTES + 1), b"SYNTHETIC_TOKEN_ECHO", b'{"data":"not a list"}'):
            with self.subTest(size=len(payload)), self.open(payload), self.assertRaises(DiscoveryError) as error:
                discover_models("https://synthetic.invalid/v1", "SYNTHETIC_TOKEN_ECHO")
            self.assertNotIn("SYNTHETIC_TOKEN_ECHO", str(error.exception))
        for error in (TimeoutError("SYNTHETIC_TOKEN_ECHO"), urllib.error.HTTPError("u", 401, "SYNTHETIC_TOKEN_ECHO", {}, io.BytesIO())):
            with patch("mindos.external_model_discovery.urllib.request.build_opener", return_value=SimpleNamespace(open=Mock(side_effect=error))):
                with self.assertRaises(DiscoveryError) as caught:
                    discover_models("https://synthetic.invalid", "SYNTHETIC_TOKEN_ECHO")
                self.assertNotIn("SYNTHETIC_TOKEN_ECHO", str(caught.exception))
