"""Persistent authorization and administration for remote MCP access.

Secrets that authorize resource access are never stored in plaintext. OAuth
client secrets are encrypted with a machine-local key because the MCP SDK must
retrieve them for confidential-client authentication.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
import secrets
import socket
import sqlite3
import ssl
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from runtime_paths import MCP_CONFIG_DIR, MCP_DATA_DIR


SCOPES = ["kb:read", "memory:read"]
BASIC_SCOPE = "basic:read"
REMOTE_MODES = {"basic", "advanced"}
BASIC_CLIENT_ID = "basic_shared"
ACCESS_TOKEN_SECONDS = 3600
REFRESH_TOKEN_SECONDS = 30 * 86400
AUTH_CODE_SECONDS = 300
AUTH_REQUEST_SECONDS = 600


def _default_lan_ip() -> str:
    configured = os.getenv("CENTAUR_MCP_LAN_IP", "").strip()
    if configured:
        return configured
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            address = sock.getsockname()[0]
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass
    return "192.168.1.86"


def _data_dir() -> Path:
    return Path(
        os.getenv(
            "CENTAUR_MCP_DATA_DIR",
            str(MCP_DATA_DIR),
        )
    )


def _config_dir() -> Path:
    return Path(
        os.getenv(
            "CENTAUR_MCP_CONFIG_DIR",
            str(MCP_CONFIG_DIR),
        )
    )


def default_runtime_config() -> dict[str, Any]:
    ip = _default_lan_ip()
    port = int(os.getenv("CENTAUR_MCP_HTTPS_PORT", "8443"))
    lan_http_port = int(os.getenv("CENTAUR_LAN_HTTP_PORT", "8080"))
    public_base = os.getenv("CENTAUR_MCP_PUBLIC_BASE", "").strip()
    if not public_base:
        public_base = f"https://{ip}" if port == 443 else f"https://{ip}:{port}"
    return {
        "enabled": False,
        "mode": "basic",
        "lan_ip": ip,
        "https_port": port,
        "lan_http_port": lan_http_port,
        "public_base": public_base.rstrip("/"),
        "mcp_port": 8620,
        "admin_password": {},
        "updated_at": int(time.time()),
    }


def runtime_config_path() -> Path:
    return _config_dir() / "remote_mcp.json"


def get_runtime_config() -> dict[str, Any]:
    config = default_runtime_config()
    path = runtime_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if isinstance(raw, dict):
            config.update(raw)
    except (OSError, ValueError):
        pass
    config["public_base"] = str(config.get("public_base") or "").rstrip("/")
    if config.get("mode") not in REMOTE_MODES:
        config["mode"] = "basic"
    return config


def save_runtime_config(updates: dict[str, Any]) -> dict[str, Any]:
    if "mode" in updates and updates["mode"] not in REMOTE_MODES:
        raise ValueError("Remote MCP mode must be basic or advanced")
    config = get_runtime_config()
    config.update(updates)
    config["updated_at"] = int(time.time())
    path = runtime_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return config


def _password_record(password: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return {
        "algorithm": "scrypt",
        "salt": salt.hex(),
        "digest": digest.hex(),
        "n": 2**14,
        "r": 8,
        "p": 1,
    }


def set_admin_password(password: str) -> None:
    if len(password) < 10:
        raise ValueError("MCP administrator password must contain at least 10 characters")
    save_runtime_config({"admin_password": _password_record(password)})


def admin_password_is_set() -> bool:
    return bool(get_runtime_config().get("admin_password", {}).get("digest"))


def verify_admin_password(password: str) -> bool:
    record = get_runtime_config().get("admin_password") or {}
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(record["salt"]),
            n=int(record.get("n", 2**14)),
            r=int(record.get("r", 8)),
            p=int(record.get("p", 1)),
            dklen=32,
        )
        return hmac.compare_digest(digest.hex(), str(record["digest"]))
    except (KeyError, TypeError, ValueError):
        return False


def public_urls() -> dict[str, str]:
    base = get_runtime_config()["public_base"]
    return {
        "base": base,
        "issuer": base,
        "basic": f"{base}/mcp/basic",
        "kb": f"{base}/mcp/kb",
        "full": f"{base}/mcp/full",
        "ca": f"{base}/ca.crt",
        "health": f"{base}/health",
    }


def remote_mode_active(mode: str) -> bool:
    config = get_runtime_config()
    return bool(config.get("enabled")) and config.get("mode") == mode


def ca_certificate_path() -> Path:
    return _config_dir() / "tls" / "ca.crt"


def ca_fingerprint() -> str:
    try:
        pem = ca_certificate_path().read_text(encoding="ascii")
        der = ssl.PEM_cert_to_DER_cert(pem)
        digest = hashlib.sha256(der).hexdigest().upper()
        return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
    except (OSError, ValueError):
        return ""


request_source_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "centaur_mcp_request_source", default=""
)


class StoredAccessToken(AccessToken):
    pair_id: str = ""
    token_kind: str = "access"


class StoredRefreshToken(RefreshToken):
    pair_id: str = ""
    resource: str | None = None


class AccessStore:
    def __init__(self, data_dir: Path | None = None, config_dir: Path | None = None):
        self.data_dir = data_dir or _data_dir()
        self.config_dir = config_dir or _config_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "mcp_auth.db"
        self.key_path = self.config_dir / "mcp_client_secret.key"
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=15)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    client_json TEXT,
                    client_secret_encrypted TEXT,
                    approved INTEGER NOT NULL DEFAULT 0,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token_hash TEXT PRIMARY KEY,
                    token_suffix TEXT NOT NULL,
                    token_type TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    resource TEXT,
                    expires_at INTEGER,
                    pair_id TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER,
                    FOREIGN KEY(client_id) REFERENCES clients(client_id)
                );
                CREATE INDEX IF NOT EXISTS idx_tokens_client ON tokens(client_id);
                CREATE INDEX IF NOT EXISTS idx_tokens_pair ON tokens(pair_id);
                CREATE TABLE IF NOT EXISTS authorization_requests (
                    request_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS authorization_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    code_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    source_ip TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_created ON audit(created_at DESC);
                """
            )
        self.db_path.chmod(0o600)

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _fernet(self) -> Fernet:
        if not self.key_path.exists():
            self.key_path.write_bytes(Fernet.generate_key())
            self.key_path.chmod(0o600)
        return Fernet(self.key_path.read_bytes().strip())

    @staticmethod
    def _validate_redirect_uris(client: OAuthClientInformationFull) -> None:
        for raw_uri in client.redirect_uris or []:
            parsed = urlparse(str(raw_uri))
            host = (parsed.hostname or "").lower()
            loopback = host in {"localhost", "127.0.0.1", "::1"}
            if parsed.fragment or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "Redirect URIs must use HTTPS, except exact HTTP loopback addresses.",
                )

    def save_oauth_client(self, client: OAuthClientInformationFull) -> None:
        self._validate_redirect_uris(client)
        if not client.client_id:
            raise RegistrationError("invalid_client_metadata", "client_id is required")
        payload = client.model_dump(mode="json")
        secret = str(payload.pop("client_secret") or "")
        encrypted = self._fernet().encrypt(secret.encode()).decode() if secret else ""
        scopes = set((client.scope or "").split())
        tier = "full" if "memory:read" in scopes else "kb"
        now = int(time.time())
        with self._connect() as db:
            # DCR is public by design. Bound unapproved registrations so an
            # unauthenticated LAN peer cannot grow the database indefinitely.
            db.execute(
                "DELETE FROM clients WHERE kind='oauth' AND approved=0 AND created_at<? "
                "AND client_id NOT IN (SELECT DISTINCT client_id FROM tokens)",
                (now - 86400,),
            )
            pending_count = db.execute(
                "SELECT COUNT(*) AS n FROM clients WHERE kind='oauth' AND approved=0 AND revoked=0"
            ).fetchone()["n"]
            if pending_count >= 100:
                raise RegistrationError(
                    "invalid_client_metadata",
                    "Too many pending clients; the owner must approve or revoke existing registrations.",
                )
            db.execute(
                """
                INSERT INTO clients (
                    client_id, kind, label, tier, client_json,
                    client_secret_encrypted, approved, revoked,
                    created_at, updated_at
                ) VALUES (?, 'oauth', ?, ?, ?, ?, 0, 0, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    label=excluded.label,
                    tier=excluded.tier,
                    client_json=excluded.client_json,
                    client_secret_encrypted=excluded.client_secret_encrypted,
                    updated_at=excluded.updated_at,
                    revoked=0
                """,
                (
                    client.client_id,
                    (client.client_name or "OAuth MCP client")[:120],
                    tier,
                    json.dumps(payload, ensure_ascii=False),
                    encrypted,
                    now,
                    now,
                ),
            )

    def get_oauth_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM clients WHERE client_id=? AND kind='oauth' AND revoked=0",
                (client_id,),
            ).fetchone()
        if not row or not row["client_json"]:
            return None
        try:
            payload = json.loads(row["client_json"])
            encrypted = row["client_secret_encrypted"] or ""
            payload["client_secret"] = self._fernet().decrypt(encrypted.encode()).decode() if encrypted else None
            return OAuthClientInformationFull.model_validate(payload)
        except Exception:
            return None

    def create_authorization_request(self, client_id: str, params: AuthorizationParams) -> str:
        request_id = "ar_" + secrets.token_urlsafe(32)
        now = int(time.time())
        with self._connect() as db:
            db.execute("DELETE FROM authorization_requests WHERE expires_at < ?", (now,))
            db.execute(
                "INSERT INTO authorization_requests VALUES (?, ?, ?, ?, ?)",
                (
                    request_id,
                    client_id,
                    params.model_dump_json(),
                    now + AUTH_REQUEST_SECONDS,
                    now,
                ),
            )
        return request_id

    def get_authorization_request(self, request_id: str) -> tuple[dict[str, Any], AuthorizationParams] | None:
        now = int(time.time())
        with self._connect() as db:
            row = db.execute(
                """
                SELECT r.*, c.label, c.tier, c.revoked
                FROM authorization_requests r
                JOIN clients c ON c.client_id=r.client_id
                WHERE r.request_id=? AND r.expires_at>=?
                """,
                (request_id, now),
            ).fetchone()
        if not row or row["revoked"]:
            return None
        return dict(row), AuthorizationParams.model_validate_json(row["params_json"])

    def delete_authorization_request(self, request_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM authorization_requests WHERE request_id=?", (request_id,))

    def approve_authorization_request(self, request_id: str) -> tuple[str, str]:
        pending = self.get_authorization_request(request_id)
        if not pending:
            raise ValueError("Authorization request is missing or expired")
        row, params = pending
        code = "ac_" + secrets.token_urlsafe(32)
        expires_at = int(time.time()) + AUTH_CODE_SECONDS
        auth_code = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=expires_at,
            client_id=row["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="owner",
        )
        with self._connect() as db:
            db.execute("DELETE FROM authorization_requests WHERE request_id=?", (request_id,))
            db.execute(
                "INSERT INTO authorization_codes VALUES (?, ?, ?, ?, ?)",
                (
                    self.token_hash(code),
                    row["client_id"],
                    auth_code.model_dump_json(),
                    expires_at,
                    int(time.time()),
                ),
            )
            approved_tier = "full" if "memory:read" in (params.scopes or []) else "kb"
            db.execute(
                "UPDATE clients SET approved=1, tier=?, updated_at=? WHERE client_id=?",
                (approved_tier, int(time.time()), row["client_id"]),
            )
        redirect = construct_redirect_uri(
            str(params.redirect_uri),
            code=code,
            state=params.state,
        )
        return code, redirect

    def deny_authorization_request(self, request_id: str) -> str:
        pending = self.get_authorization_request(request_id)
        if not pending:
            raise ValueError("Authorization request is missing or expired")
        _, params = pending
        self.delete_authorization_request(request_id)
        return construct_redirect_uri(
            str(params.redirect_uri),
            error="access_denied",
            error_description="The resource owner denied access.",
            state=params.state,
        )

    def load_authorization_code(self, code: str, client_id: str) -> AuthorizationCode | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM authorization_codes WHERE code_hash=? AND client_id=?",
                (self.token_hash(code), client_id),
            ).fetchone()
        if not row:
            return None
        try:
            return AuthorizationCode.model_validate_json(row["code_json"])
        except ValueError:
            return None

    def consume_authorization_code(self, code: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM authorization_codes WHERE code_hash=?", (self.token_hash(code),))

    def _save_token(
        self,
        token: str,
        token_type: str,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        expires_at: int | None,
        pair_id: str,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO tokens (
                    token_hash, token_suffix, token_type, client_id,
                    scopes_json, resource, expires_at, pair_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.token_hash(token),
                    token[-6:],
                    token_type,
                    client_id,
                    json.dumps(scopes),
                    resource,
                    expires_at,
                    pair_id,
                    int(time.time()),
                ),
            )

    def issue_oauth_tokens(
        self,
        *,
        client_id: str,
        scopes: list[str],
        resource: str | None,
    ) -> OAuthToken:
        access = "at_" + secrets.token_urlsafe(32)
        refresh = "rt_" + secrets.token_urlsafe(40)
        now = int(time.time())
        pair_id = uuid.uuid4().hex
        self._save_token(access, "access", client_id, scopes, resource, now + ACCESS_TOKEN_SECONDS, pair_id)
        self._save_token(refresh, "refresh", client_id, scopes, resource, now + REFRESH_TOKEN_SECONDS, pair_id)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def _get_token_row(self, token: str, types: tuple[str, ...]) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in types)
        with self._connect() as db:
            row = db.execute(
                f"""
                SELECT t.*, c.revoked AS client_revoked, c.kind AS client_kind
                FROM tokens t JOIN clients c ON c.client_id=t.client_id
                WHERE t.token_hash=? AND t.token_type IN ({placeholders})
                """,
                (self.token_hash(token), *types),
            ).fetchone()
        if not row or row["revoked"] or row["client_revoked"]:
            return None
        if row["expires_at"] and int(row["expires_at"]) < int(time.time()):
            return None
        return row

    def load_access_token(self, token: str) -> StoredAccessToken | None:
        config = get_runtime_config()
        if not config.get("enabled", False):
            return None
        row = self._get_token_row(token, ("access", "compat", "basic"))
        if not row:
            return None
        mode = config.get("mode", "basic")
        if mode == "basic" and row["client_kind"] != "basic":
            return None
        if mode == "advanced" and row["client_kind"] == "basic":
            return None
        now = int(time.time())
        with self._connect() as db:
            db.execute("UPDATE tokens SET last_used_at=? WHERE token_hash=?", (now, self.token_hash(token)))
            db.execute("UPDATE clients SET last_used_at=? WHERE client_id=?", (now, row["client_id"]))
        return StoredAccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject="owner",
            claims={"iss": public_urls()["issuer"]},
            pair_id=row["pair_id"],
            token_kind=row["token_type"],
        )

    def load_refresh_token(self, token: str, client_id: str) -> StoredRefreshToken | None:
        if not remote_mode_active("advanced"):
            return None
        row = self._get_token_row(token, ("refresh",))
        if not row or row["client_id"] != client_id:
            return None
        return StoredRefreshToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            subject="owner",
            pair_id=row["pair_id"],
            resource=row["resource"],
        )

    def revoke_pair(self, pair_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE tokens SET revoked=1 WHERE pair_id=?", (pair_id,))

    def clear_pending_oauth(self) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM authorization_requests")
            db.execute("DELETE FROM authorization_codes")

    def get_basic_client(self) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT client_id FROM clients WHERE client_id=? AND kind='basic' AND revoked=0",
                (BASIC_CLIENT_ID,),
            ).fetchone()
        return self.get_client_public(BASIC_CLIENT_ID) if row else None

    def create_basic_token(self) -> tuple[dict[str, Any], str]:
        existing = self.get_basic_client()
        if existing and existing.get("token_suffix"):
            raise ValueError("Basic connection key already exists; rotate it instead")
        now = int(time.time())
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO clients (
                    client_id, kind, label, tier, client_json,
                    client_secret_encrypted, approved, revoked,
                    created_at, updated_at, last_used_at
                ) VALUES (?, 'basic', '普通模式共享密钥', 'basic', NULL, NULL, 1, 0, ?, ?, NULL)
                ON CONFLICT(client_id) DO UPDATE SET
                    kind='basic', label='普通模式共享密钥', tier='basic',
                    approved=1, revoked=0, updated_at=excluded.updated_at, last_used_at=NULL
                """,
                (BASIC_CLIENT_ID, now, now),
            )
        token = "cmcp_basic_" + secrets.token_urlsafe(32)
        self._save_token(
            token,
            "basic",
            BASIC_CLIENT_ID,
            [BASIC_SCOPE],
            public_urls()["basic"],
            None,
            uuid.uuid4().hex,
        )
        return self.get_basic_client() or {}, token

    def rotate_basic_token(self) -> tuple[dict[str, Any], str]:
        if not self.get_basic_client():
            raise ValueError("Basic connection key does not exist")
        now = int(time.time())
        with self._connect() as db:
            db.execute("UPDATE tokens SET revoked=1 WHERE client_id=?", (BASIC_CLIENT_ID,))
            db.execute(
                "UPDATE clients SET last_used_at=NULL, updated_at=? WHERE client_id=?",
                (now, BASIC_CLIENT_ID),
            )
        token = "cmcp_basic_" + secrets.token_urlsafe(32)
        self._save_token(
            token,
            "basic",
            BASIC_CLIENT_ID,
            [BASIC_SCOPE],
            public_urls()["basic"],
            None,
            uuid.uuid4().hex,
        )
        return self.get_basic_client() or {}, token

    def create_compat_client(self, label: str, tier: str) -> tuple[dict[str, Any], str]:
        if tier not in {"kb", "full"}:
            raise ValueError("tier must be kb or full")
        label = label.strip()[:120]
        if not label:
            raise ValueError("client label is required")
        client_id = "compat_" + uuid.uuid4().hex
        now = int(time.time())
        with self._connect() as db:
            db.execute(
                "INSERT INTO clients VALUES (?, 'compat', ?, ?, NULL, NULL, 1, 0, ?, ?, NULL)",
                (client_id, label, tier, now, now),
            )
        token = "cmcp_" + secrets.token_urlsafe(32)
        urls = public_urls()
        scope = "memory:read" if tier == "full" else "kb:read"
        resource = urls["full"] if tier == "full" else urls["kb"]
        self._save_token(token, "compat", client_id, [scope], resource, None, uuid.uuid4().hex)
        return self.get_client_public(client_id) or {}, token

    def rotate_compat_client(self, client_id: str) -> str:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM clients WHERE client_id=? AND kind='compat' AND revoked=0",
                (client_id,),
            ).fetchone()
        if not row:
            raise ValueError("Compatibility client not found")
        with self._connect() as db:
            db.execute("UPDATE tokens SET revoked=1 WHERE client_id=?", (client_id,))
        token = "cmcp_" + secrets.token_urlsafe(32)
        tier = row["tier"]
        scope = "memory:read" if tier == "full" else "kb:read"
        resource = public_urls()["full" if tier == "full" else "kb"]
        self._save_token(token, "compat", client_id, [scope], resource, None, uuid.uuid4().hex)
        return token

    def revoke_client(self, client_id: str) -> bool:
        with self._connect() as db:
            found = db.execute("SELECT 1 FROM clients WHERE client_id=?", (client_id,)).fetchone()
            if not found:
                return False
            now = int(time.time())
            db.execute("UPDATE clients SET revoked=1, updated_at=? WHERE client_id=?", (now, client_id))
            db.execute("UPDATE tokens SET revoked=1 WHERE client_id=?", (client_id,))
            db.execute("DELETE FROM authorization_requests WHERE client_id=?", (client_id,))
            db.execute("DELETE FROM authorization_codes WHERE client_id=?", (client_id,))
        return True

    def get_client_public(self, client_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
            token = db.execute(
                """
                SELECT token_suffix, last_used_at FROM tokens
                WHERE client_id=? AND revoked=0 ORDER BY created_at DESC LIMIT 1
                """,
                (client_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "client_id": row["client_id"],
            "kind": row["kind"],
            "label": row["label"],
            "tier": row["tier"],
            "approved": bool(row["approved"]),
            "revoked": bool(row["revoked"]),
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"] or (token["last_used_at"] if token else None),
            "token_suffix": token["token_suffix"] if token and row["kind"] in {"compat", "basic"} else "",
        }

    def list_clients(self, include_revoked: bool = False) -> list[dict[str, Any]]:
        clause = "WHERE kind!='basic'" if include_revoked else "WHERE revoked=0 AND kind!='basic'"
        with self._connect() as db:
            ids = [
                row["client_id"]
                for row in db.execute(
                    f"SELECT client_id FROM clients {clause} ORDER BY created_at DESC"
                ).fetchall()
            ]
        return [item for client_id in ids if (item := self.get_client_public(client_id))]

    def record_audit(self, client_id: str, tool_name: str, success: bool, detail: str, source_ip: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO audit (client_id, tool_name, success, detail, source_ip, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (client_id, tool_name[:100], int(success), detail[:160], source_ip[:80], int(time.time())),
            )
            # Retain a bounded local audit history.
            db.execute("DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 5000)")

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT a.*, c.label FROM audit a
                LEFT JOIN clients c ON c.client_id=a.client_id
                ORDER BY a.id DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]


_STORE: AccessStore | None = None


def get_store() -> AccessStore:
    global _STORE
    if _STORE is None:
        _STORE = AccessStore()
    return _STORE


class CentaurOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, StoredRefreshToken, StoredAccessToken]
):
    def __init__(self, store: AccessStore | None = None):
        self.store = store or get_store()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not remote_mode_active("advanced"):
            return None
        return self.store.get_oauth_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not remote_mode_active("advanced"):
            raise RegistrationError("invalid_client_metadata", "Advanced MCP access is disabled.")
        self.store.save_oauth_client(client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        config = get_runtime_config()
        if not config.get("enabled") or config.get("mode") != "advanced":
            raise AuthorizeError("temporarily_unavailable", "Advanced MCP access is disabled.")
        urls = public_urls()
        resource = (params.resource or "").rstrip("/")
        requested = set(params.scopes or [])
        if not resource:
            resource = urls["full"] if "memory:read" in requested else urls["kb"]
        if resource == urls["full"]:
            if "memory:read" not in requested:
                raise AuthorizeError("invalid_scope", "The full memory resource requires memory:read.")
            params.scopes = ["memory:read"]
        elif resource == urls["kb"]:
            if "kb:read" not in requested:
                raise AuthorizeError("invalid_scope", "The knowledge resource requires kb:read.")
            params.scopes = ["kb:read"]
        else:
            raise AuthorizeError("invalid_request", "Unknown MCP resource indicator.")
        params.resource = resource
        request_id = self.store.create_authorization_request(str(client.client_id), params)
        return f"{urls['issuer']}/oauth/consent?request={request_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        if not remote_mode_active("advanced"):
            return None
        return self.store.load_authorization_code(authorization_code, str(client.client_id))

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if not remote_mode_active("advanced"):
            raise TokenError("invalid_grant", "Advanced MCP access is disabled.")
        loaded = self.store.load_authorization_code(authorization_code.code, str(client.client_id))
        if not loaded:
            raise TokenError("invalid_grant", "Authorization code is invalid or already used.")
        self.store.consume_authorization_code(authorization_code.code)
        return self.store.issue_oauth_tokens(
            client_id=str(client.client_id),
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> StoredRefreshToken | None:
        if not remote_mode_active("advanced"):
            return None
        return self.store.load_refresh_token(refresh_token, str(client.client_id))

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: StoredRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if not remote_mode_active("advanced"):
            raise TokenError("invalid_grant", "Advanced MCP access is disabled.")
        self.store.revoke_pair(refresh_token.pair_id)
        return self.store.issue_oauth_tokens(
            client_id=str(client.client_id),
            scopes=scopes,
            resource=refresh_token.resource,
        )

    async def load_access_token(self, token: str) -> StoredAccessToken | None:
        return self.store.load_access_token(token)

    async def revoke_token(self, token: StoredAccessToken | StoredRefreshToken) -> None:
        self.store.revoke_pair(token.pair_id)


class ResourceTokenVerifier:
    def __init__(self, resource_url: str, store: AccessStore | None = None):
        self.resource_url = resource_url.rstrip("/")
        self.store = store or get_store()

    async def verify_token(self, token: str) -> AccessToken | None:
        access = self.store.load_access_token(token)
        if not access or (access.resource or "").rstrip("/") != self.resource_url:
            return None
        return access


def record_tool_audit(tool_name: str, success: bool, detail: str = "") -> None:
    access = get_access_token()
    if not access:
        return
    get_store().record_audit(
        access.client_id,
        tool_name,
        success,
        detail,
        request_source_var.get(),
    )
