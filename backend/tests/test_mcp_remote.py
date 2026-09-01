import asyncio
import base64
import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import AuthorizationParams, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.testclient import TestClient

import mcp_access


class RemoteMcpAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old_env = {
            key: os.environ.get(key)
            for key in ("CENTAUR_MCP_DATA_DIR", "CENTAUR_MCP_CONFIG_DIR", "CENTAUR_MCP_PUBLIC_BASE")
        }
        os.environ["CENTAUR_MCP_DATA_DIR"] = str(root / "data")
        os.environ["CENTAUR_MCP_CONFIG_DIR"] = str(root / "config")
        os.environ["CENTAUR_MCP_PUBLIC_BASE"] = "https://192.168.1.86:8443"
        mcp_access.save_runtime_config(
            {
                "enabled": True,
                "mode": "advanced",
                "lan_ip": "192.168.1.86",
                "https_port": 8443,
                "public_base": "https://192.168.1.86:8443",
                "mcp_port": 8620,
            }
        )
        self.store = mcp_access.AccessStore(root / "data", root / "config")
        self.old_store = mcp_access._STORE
        mcp_access._STORE = self.store

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        mcp_access._STORE = self.old_store
        self.temp.cleanup()

    def test_access_store_connection_context_commits_and_closes(self):
        connections = []
        original_connect = sqlite3.connect

        class TrackingConnection(sqlite3.Connection):
            closed = False

            def close(self):
                self.closed = True
                super().close()

        def connect(*args, **kwargs):
            connection = original_connect(*args, factory=TrackingConnection, **kwargs)
            connections.append(connection)
            return connection

        root = Path(self.temp.name) / "connection-lifecycle"
        with patch.object(mcp_access.sqlite3, "connect", side_effect=connect):
            store = mcp_access.AccessStore(root / "data", root / "config")
            _, token = store.create_compat_client("Close Test", "kb")
            self.assertIsNotNone(store.load_access_token(token))

        self.assertTrue(connections)
        self.assertTrue(all(connection.closed for connection in connections))

    def test_compatibility_tokens_are_resource_bound_and_rotatable(self):
        client, token = self.store.create_compat_client("KB Agent", "kb")
        access = self.store.load_access_token(token)
        self.assertIsNotNone(access)
        self.assertEqual(access.scopes, ["kb:read"])
        self.assertEqual(access.resource, "https://192.168.1.86:8443/mcp/kb")

        kb_verifier = mcp_access.ResourceTokenVerifier(access.resource, self.store)
        full_verifier = mcp_access.ResourceTokenVerifier(
            "https://192.168.1.86:8443/mcp/full", self.store
        )
        self.assertIsNotNone(asyncio.run(kb_verifier.verify_token(token)))
        self.assertIsNone(asyncio.run(full_verifier.verify_token(token)))

        replacement = self.store.rotate_compat_client(client["client_id"])
        self.assertIsNone(self.store.load_access_token(token))
        self.assertIsNotNone(self.store.load_access_token(replacement))

        self.assertTrue(self.store.revoke_client(client["client_id"]))
        self.assertIsNone(self.store.load_access_token(replacement))

    def test_basic_token_has_fixed_tools_and_isolated_mode(self):
        advanced_client, advanced_token = self.store.create_compat_client("Advanced Agent", "full")
        mcp_access.save_runtime_config({"mode": "basic"})

        basic_client, basic_token = self.store.create_basic_token()
        access = self.store.load_access_token(basic_token)
        self.assertIsNotNone(access)
        self.assertEqual(access.scopes, ["basic:read"])
        self.assertEqual(access.resource, "https://192.168.1.86:8443/mcp/basic")
        self.assertIsNone(self.store.load_access_token(advanced_token))

        with self.store._connect() as db:
            stored = db.execute(
                "SELECT token_hash FROM tokens WHERE client_id=? AND revoked=0",
                (mcp_access.BASIC_CLIENT_ID,),
            ).fetchone()
        self.assertEqual(stored["token_hash"], self.store.token_hash(basic_token))
        self.assertNotEqual(stored["token_hash"], basic_token)

        replacement_client, replacement = self.store.rotate_basic_token()
        self.assertEqual(replacement_client["client_id"], basic_client["client_id"])
        self.assertIsNone(self.store.load_access_token(basic_token))
        self.assertIsNotNone(self.store.load_access_token(replacement))

        mcp_access.save_runtime_config({"mode": "advanced"})
        self.assertIsNone(self.store.load_access_token(replacement))
        self.assertIsNotNone(self.store.load_access_token(advanced_token))
        self.assertEqual(advanced_client["tier"], "full")

        mcp_access.save_runtime_config({"mode": "basic"})
        self.assertIsNotNone(self.store.load_access_token(replacement))
        self.assertIsNone(self.store.load_access_token(advanced_token))

        mcp_access.save_runtime_config({"enabled": False})
        self.assertIsNone(self.store.load_access_token(replacement))

    def test_basic_profile_exposes_only_common_six_tools(self):
        from mcp_tools import create_mcp_server

        server = create_mcp_server(profile="basic")
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertEqual(
            names,
            {
                "kb_search",
                "kb_get_stats",
                "kb_list_documents",
                "kb_health",
                "memory_search",
                "memory_get_context",
            },
        )

    def test_modes_default_to_basic_and_new_remote_access_is_off(self):
        defaults = mcp_access.default_runtime_config()
        self.assertFalse(defaults["enabled"])
        self.assertEqual(defaults["mode"], "basic")
        self.assertEqual(defaults["lan_http_port"], 8080)

    def test_oauth_authorization_code_pkce_state_and_refresh_rotation(self):
        async def scenario():
            provider = mcp_access.CentaurOAuthProvider(self.store)
            client = OAuthClientInformationFull(
                client_id="oauth-test",
                redirect_uris=[AnyUrl("http://127.0.0.1:33445/callback")],
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope="kb:read memory:read",
                client_name="Neutral MCP Test Client",
            )
            await provider.register_client(client)
            params = AuthorizationParams(
                state="state-123",
                scopes=["memory:read"],
                code_challenge="challenge",
                redirect_uri=AnyUrl("http://127.0.0.1:33445/callback"),
                redirect_uri_provided_explicitly=True,
                resource="https://192.168.1.86:8443/mcp/full",
            )
            consent_url = await provider.authorize(client, params)
            request_id = parse_qs(urlparse(consent_url).query)["request"][0]
            _, redirect = self.store.approve_authorization_request(request_id)
            query = parse_qs(urlparse(redirect).query)
            self.assertEqual(query["state"], ["state-123"])
            code = query["code"][0]

            loaded_code = await provider.load_authorization_code(client, code)
            self.assertIsNotNone(loaded_code)
            tokens = await provider.exchange_authorization_code(client, loaded_code)
            access = await provider.load_access_token(tokens.access_token)
            self.assertEqual(access.resource, "https://192.168.1.86:8443/mcp/full")
            self.assertEqual(access.scopes, ["memory:read"])

            refresh = await provider.load_refresh_token(client, tokens.refresh_token)
            replacement = await provider.exchange_refresh_token(client, refresh, ["memory:read"])
            self.assertIsNone(await provider.load_access_token(tokens.access_token))
            self.assertIsNotNone(await provider.load_access_token(replacement.access_token))

        asyncio.run(scenario())

    def test_dynamic_registration_rejects_insecure_external_redirect(self):
        client = OAuthClientInformationFull(
            client_id="bad-client",
            redirect_uris=[AnyUrl("http://example.com/callback")],
            token_endpoint_auth_method="none",
            scope="kb:read",
        )
        with self.assertRaises(RegistrationError):
            self.store.save_oauth_client(client)

    def test_http_oauth_discovery_dcr_consent_pkce_and_token(self):
        import mcp_remote_server

        mcp_access.set_admin_password("test-owner-password")
        verifier = "neutral-mcp-pkce-verifier-0123456789"
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        app = mcp_remote_server.build_app()
        with TestClient(app, base_url="http://testserver") as client:
            metadata = client.get("/.well-known/oauth-authorization-server").json()
            self.assertIn("none", metadata["token_endpoint_auth_methods_supported"])

            registered = client.post(
                "/register",
                json={
                    "redirect_uris": ["http://127.0.0.1:33445/callback"],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "scope": "kb:read memory:read",
                    "client_name": "Generic MCP Client",
                },
            )
            self.assertEqual(registered.status_code, 201)
            client_id = registered.json()["client_id"]

            authorized = client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "http://127.0.0.1:33445/callback",
                    "response_type": "code",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": "http-flow-state",
                    "scope": "kb:read",
                    "resource": "https://192.168.1.86:8443/mcp/kb",
                },
                follow_redirects=False,
            )
            self.assertEqual(authorized.status_code, 302)
            consent_url = authorized.headers["location"]
            request_id = parse_qs(urlparse(consent_url).query)["request"][0]

            consent = client.post(
                "/oauth/consent",
                data={
                    "request": request_id,
                    "action": "approve",
                    "password": "test-owner-password",
                },
                follow_redirects=False,
            )
            callback = urlparse(consent.headers["location"])
            callback_query = parse_qs(callback.query)
            self.assertEqual(callback_query["state"], ["http-flow-state"])

            token = client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": callback_query["code"][0],
                    "redirect_uri": "http://127.0.0.1:33445/callback",
                    "client_id": client_id,
                    "code_verifier": verifier,
                    "resource": "https://192.168.1.86:8443/mcp/kb",
                },
            )
            self.assertEqual(token.status_code, 200)
            self.assertEqual(token.json()["scope"], "kb:read")
            self.assertTrue(token.json()["refresh_token"])

    def test_basic_service_document_hides_advanced_resources_and_oauth(self):
        import mcp_remote_server

        mcp_access.save_runtime_config({"mode": "basic"})
        app = mcp_remote_server.build_app()
        with TestClient(app, base_url="http://testserver") as client:
            document = client.get("/")
            self.assertEqual(document.status_code, 200)
            self.assertEqual(
                document.json()["resources"],
                {"basic_memory": "https://192.168.1.86:8443/mcp/basic"},
            )
            self.assertEqual(
                client.get("/.well-known/oauth-authorization-server").status_code,
                404,
            )

    def test_basic_http_token_lists_six_tools_and_cannot_cross_resource(self):
        import mcp_remote_server

        mcp_access.save_runtime_config({"mode": "basic"})
        _, token = self.store.create_basic_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-11-25",
        }
        app = mcp_remote_server.build_app()
        with TestClient(app, base_url="http://testserver") as client:
            tools = client.post(
                "/mcp/basic",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            self.assertEqual(tools.status_code, 200)
            self.assertEqual(
                {item["name"] for item in tools.json()["result"]["tools"]},
                {
                    "kb_search",
                    "kb_get_stats",
                    "kb_list_documents",
                    "kb_health",
                    "memory_search",
                    "memory_get_context",
                },
            )
            crossed = client.post(
                "/mcp/full",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            self.assertEqual(crossed.status_code, 401)


if __name__ == "__main__":
    unittest.main()
