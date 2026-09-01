import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import tokenmanager_sync


class _TokenManagerHandler(BaseHTTPRequestHandler):
    token = "t" * 43
    conversation_id = "conversation-1"
    memory_id = "memory-1"
    memory_enabled = False
    memory_deleted = False
    identity_enabled = False
    identity_payload = None

    def log_message(self, _format, *_args):
        return

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/v1/health":
            capabilities = ["conversations"]
            if self.memory_enabled:
                capabilities.append("memories")
            if self.identity_enabled:
                capabilities.append("identity-write")
            self._send({"status": "ok", "enabled": True, "capabilities": capabilities})
            return
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self._send({"error": "unauthorized"}, 401)
            return
        if parsed.path == "/v1/conversations/changes":
            cursor = parse_qs(parsed.query).get("cursor", [""])[0]
            if cursor:
                self._send({"items": [], "nextCursor": cursor, "hasMore": False})
            else:
                self._send(
                    {
                        "items": [
                            {
                                "sequence": 7,
                                "conversation": {
                                    "id": self.conversation_id,
                                    "source": "local_history",
                                "provider": "codex",
                                "ownerKey": "issuer\\u001fuser-7",
                                "userId": "user-7",
                                "userName": "Alice",
                                "userEmail": "alice@example.com",
                                "status": "imported",
                                    "title": "实现同步",
                                    "createdAt": 1000,
                                    "updatedAt": 2000,
                                    "messageCount": 2,
                                    "hasPartialResponse": False,
                                },
                            }
                        ],
                        "nextCursor": "7",
                        "hasMore": False,
                    }
                )
            return
        if parsed.path == f"/v1/conversations/{self.conversation_id}":
            self._send(
                {
                    "conversation": {
                        "id": self.conversation_id,
                        "source": "local_history",
                        "provider": "codex",
                        "ownerKey": "issuer\\u001fuser-7",
                        "userId": "user-7",
                        "userName": "Alice",
                        "userEmail": "alice@example.com",
                        "status": "imported",
                        "title": "实现同步",
                        "createdAt": 1000,
                        "updatedAt": 2000,
                    },
                    "messages": [
                        {"logicalPosition": 0, "revision": 0, "role": "user", "content": "同步全部 Agent 对话"},
                        {"logicalPosition": 1, "revision": 0, "role": "assistant", "content": "使用增量游标"},
                    ],
                    "exchanges": [],
                }
            )
            return
        if parsed.path == "/v1/memories/changes":
            cursor = parse_qs(parsed.query).get("cursor", [""])[0]
            if not cursor:
                self._send(
                    {
                        "items": [
                            {
                                "sequence": 11,
                                "operation": "upsert",
                                "memory": {
                                    "id": self.memory_id,
                                    "provider": "codex",
                                    "scope": "agent",
                                    "kind": "learned_memory",
                                    "title": "Codex memory",
                                    "path": "~/.codex/memories.sqlite/example.md",
                                    "contentHash": "abc",
                                    "sizeBytes": 22,
                                    "updatedAt": 3000,
                                },
                            }
                        ],
                        "nextCursor": "11",
                        "hasMore": False,
                    }
                )
            elif cursor == "11" and self.memory_deleted:
                self._send(
                    {
                        "items": [
                            {
                                "sequence": 12,
                                "operation": "delete",
                                "memory": {
                                    "id": self.memory_id,
                                    "provider": "codex",
                                    "scope": "agent",
                                    "kind": "learned_memory",
                                    "title": "Codex memory",
                                    "path": "~/.codex/memories.sqlite/example.md",
                                    "contentHash": "abc",
                                    "sizeBytes": 0,
                                    "updatedAt": 4000,
                                    "deletedAt": 4000,
                                },
                            }
                        ],
                        "nextCursor": "12",
                        "hasMore": False,
                    }
                )
            else:
                self._send({"items": [], "nextCursor": cursor, "hasMore": False})
            return
        if parsed.path == f"/v1/memories/{self.memory_id}":
            self._send(
                {
                    "memory": {
                        "id": self.memory_id,
                        "provider": "codex",
                        "scope": "agent",
                        "kind": "learned_memory",
                        "title": "Codex memory",
                        "path": "~/.codex/memories.sqlite/example.md",
                        "contentHash": "abc",
                        "sizeBytes": 22,
                        "updatedAt": 3000,
                    },
                    "content": "Remember the API cursor.",
                }
            )
            return
        self._send({"error": "not found"}, 404)

    def do_PUT(self):
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self._send({"error": "unauthorized"}, 401)
            return
        if self.path != "/v1/identity" or not self.identity_enabled:
            self._send({"error": "identity write disabled"}, 403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        self.__class__.identity_payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self._send(
            {
                "schemaVersion": 1,
                "revision": "remote-revision",
                "state": "applied",
                "attemptedAt": 1784475405594,
                "targets": [
                    {"agent": "codex", "detected": True, "status": "applied", "files": []}
                ],
            }
        )


class TokenManagerSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_dir = self.root / "config"
        self.memory_dir = self.root / "memory"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _TokenManagerHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _TokenManagerHandler.memory_enabled = False
        _TokenManagerHandler.memory_deleted = False
        _TokenManagerHandler.identity_enabled = False
        _TokenManagerHandler.identity_payload = None
        tokenmanager_sync.CONFIG_DIR = self.config_dir
        tokenmanager_sync.CONFIG_PATH = self.config_dir / "tokenmanager-sync.json"
        tokenmanager_sync.IDENTITY_STATE_PATH = self.config_dir / "tokenmanager-identity-state.json"
        tokenmanager_sync.MEMORY_DIR = str(self.memory_dir)
        tokenmanager_sync.CONVERSATION_DIR = self.memory_dir / "conversations"
        tokenmanager_sync.MEMORY_IMPORT_DIR = self.memory_dir / "imports" / "tokenmanager"
        with tokenmanager_sync._RUNTIME_LOCK:
            tokenmanager_sync._RUNTIME.update(
                running=False,
                last_started_at=None,
                last_completed_at=None,
                last_imported=0,
                last_skipped=0,
                last_failed=0,
                last_error=None,
                last_conversation_imported=0,
                last_memory_imported=0,
                last_memory_deleted=0,
                memory_api_supported=None,
                capabilities=[],
                sync_mode="unknown",
                fallback_reason=None,
                identity_running=False,
                identity_last_revision=None,
                identity_last_completed_at=None,
                identity_last_error=None,
                identity_last_result=None,
            )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def _write_memory(self, relative, content, source_agent="manual", skip_index=False):
        target = self.memory_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": relative, "indexed": not skip_index, "source_agent": source_agent}

    def _delete_memory(self, relative):
        target = self.memory_dir / relative
        if not target.is_file():
            return False
        target.unlink()
        return True

    def test_config_is_private_and_never_returns_token(self):
        status = tokenmanager_sync.save_config(
            enabled=True,
            url=self.url,
            token=_TokenManagerHandler.token,
            interval_seconds=60,
        )
        self.assertTrue(status["token_configured"])
        self.assertNotIn("token", status)
        # umask/权限严格校验仅适用于 POSIX；Windows 的 chmod 无法真正设置 0600
        if not os.name == "nt":
            self.assertEqual(tokenmanager_sync.CONFIG_PATH.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(_TokenManagerHandler.token, json.dumps(status))

    def test_valid_config_and_connection_clear_stale_runtime_error(self):
        with tokenmanager_sync._RUNTIME_LOCK:
            tokenmanager_sync._RUNTIME["last_error"] = "TokenManager 只读令牌尚未配置"

        status = tokenmanager_sync.save_config(
            enabled=True,
            url=self.url,
            token=_TokenManagerHandler.token,
        )
        self.assertIsNone(status["last_error"])

        with tokenmanager_sync._RUNTIME_LOCK:
            tokenmanager_sync._RUNTIME["last_error"] = "stale connection error"
        result = tokenmanager_sync.test_connection()
        self.assertTrue(result["success"])
        self.assertIsNone(tokenmanager_sync.public_status()["last_error"])

    def test_rejects_non_loopback_endpoint(self):
        with self.assertRaises(ValueError):
            tokenmanager_sync.save_config(
                enabled=True,
                url="http://192.168.1.20:15722",
                token=_TokenManagerHandler.token,
            )

    def test_changing_server_resets_incremental_cursor(self):
        tokenmanager_sync._atomic_save_config(
            {
                **tokenmanager_sync._default_config(),
                "url": "http://127.0.0.1:15722",
                "token": "a" * 32,
                "cursor": "99",
            }
        )
        tokenmanager_sync.save_config(
            enabled=False,
            url="http://localhost:15722",
            token=None,
        )
        self.assertEqual(tokenmanager_sync._load_config()["cursor"], "")

    def test_initial_sync_writes_full_text_summary_and_advances_cursor(self):
        tokenmanager_sync.save_config(
            enabled=True,
            url=self.url,
            token=_TokenManagerHandler.token,
        )
        with mock.patch.object(tokenmanager_sync.memory_store, "write_memory_file", side_effect=self._write_memory), mock.patch.object(tokenmanager_sync, "_llm_summary", return_value=None):
            result = tokenmanager_sync.sync_now()
        self.assertTrue(result["success"])
        self.assertEqual(result["imported"], 1)
        config = tokenmanager_sync._load_config()
        self.assertEqual(config["cursor"], "7")
        files = list((self.memory_dir / "conversations" / "codex").glob("*.md"))
        self.assertEqual(len(files), 1)
        content = files[0].read_text(encoding="utf-8")
        self.assertIn("## 会话摘要", content)
        self.assertIn("同步全部 Agent 对话", content)
        self.assertIn("使用增量游标", content)
        self.assertIn('user_name: "Alice"', content)
        self.assertIn('user_email: "alice@example.com"', content)
        self.assertEqual(config["schema_version"], tokenmanager_sync.SYNC_SCHEMA_VERSION)

        # TokenManager 的增量源不会下发删除操作；源端后续无记录时，
        # 已沉淀的私人记忆仍然保留，只能由私人记忆库显式删除。
        second = tokenmanager_sync.sync_now()
        self.assertTrue(second["success"])
        self.assertEqual(second["imported"], 0)
        self.assertTrue(files[0].is_file())

    def test_memory_api_upsert_and_delete_are_mirrored(self):
        _TokenManagerHandler.memory_enabled = True
        tokenmanager_sync.save_config(
            enabled=True,
            url=self.url,
            token=_TokenManagerHandler.token,
        )
        with mock.patch.object(tokenmanager_sync.memory_store, "write_memory_file", side_effect=self._write_memory), mock.patch.object(tokenmanager_sync.memory_store, "delete_memory_file", side_effect=self._delete_memory), mock.patch.object(tokenmanager_sync, "_llm_summary", return_value=None):
            first = tokenmanager_sync.sync_now()
            self.assertTrue(first["success"])
            self.assertEqual(first["memory_imported"], 1)
            self.assertEqual(tokenmanager_sync._load_config()["memory_cursor"], "11")
            files = list((self.memory_dir / "imports" / "tokenmanager" / "codex").glob("*.md"))
            self.assertEqual(len(files), 1)
            self.assertIn("Remember the API cursor", files[0].read_text(encoding="utf-8"))

            _TokenManagerHandler.memory_deleted = True
            second = tokenmanager_sync.sync_now()
            self.assertTrue(second["success"])
            self.assertEqual(second["memory_deleted"], 1)
            self.assertFalse(files[0].exists())
            self.assertEqual(tokenmanager_sync._load_config()["memory_cursor"], "12")

    def test_fallback_mode_does_not_flip_on_transient_api_failure(self):
        _TokenManagerHandler.memory_enabled = True
        tokenmanager_sync.save_config(
            enabled=True,
            url=self.url,
            token=_TokenManagerHandler.token,
        )
        self.assertFalse(tokenmanager_sync.should_use_legacy_memory_scanner())
        with mock.patch.object(
            tokenmanager_sync,
            "_health_request",
            side_effect=RuntimeError("temporary outage"),
        ):
            self.assertFalse(tokenmanager_sync.should_use_legacy_memory_scanner())

        with tokenmanager_sync._RUNTIME_LOCK:
            tokenmanager_sync._RUNTIME["memory_api_supported"] = None
        _TokenManagerHandler.memory_enabled = False
        self.assertTrue(tokenmanager_sync.should_use_legacy_memory_scanner())

    def test_identity_publish_reuses_connection_and_clears_pending_revision(self):
        _TokenManagerHandler.identity_enabled = True
        tokenmanager_sync.save_config(
            enabled=False,
            url=self.url,
            token=_TokenManagerHandler.token,
        )
        snapshot = {
            "schemaVersion": 1,
            "files": {
                "SOUL.md": "soul",
                "AGENTS.md": "rules",
                "IDENTITY.md": "identity",
                "USER.md": "user",
            },
        }
        with mock.patch.object(tokenmanager_sync, "_identity_snapshot", return_value=(snapshot, "local-revision")):
            result = tokenmanager_sync.publish_identity()
        self.assertTrue(result["success"])
        self.assertEqual(_TokenManagerHandler.identity_payload, snapshot)
        state = tokenmanager_sync._load_identity_state()
        self.assertIsNone(state["pending_revision"])
        self.assertEqual(state["last_revision"], "remote-revision")

    def test_identity_publish_remains_pending_when_write_capability_is_disabled(self):
        tokenmanager_sync.save_config(
            enabled=False,
            url=self.url,
            token=_TokenManagerHandler.token,
        )
        with mock.patch.object(
            tokenmanager_sync,
            "_identity_snapshot",
            return_value=({"schemaVersion": 1, "files": {}}, "pending-revision"),
        ):
            result = tokenmanager_sync.publish_identity()
        self.assertFalse(result["success"])
        self.assertEqual(result["state"], "pending")
        self.assertEqual(tokenmanager_sync._load_identity_state()["pending_revision"], "pending-revision")


if __name__ == "__main__":
    unittest.main()
