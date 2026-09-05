#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"
DATABASE_DATA_ROOT="${CENTAURAI_DATABASE_DATA_ROOT:-$PROJECT_DIR/data}"
CONFIG_DIR="${CENTAUR_MCP_CONFIG_DIR:-$DATABASE_DATA_ROOT/mcp/config}"
DATA_DIR="${CENTAUR_MCP_DATA_DIR:-$DATABASE_DATA_ROOT/mcp/data}"
TLS_DIR="$CONFIG_DIR/tls"
BIN_DIR="$HOME/.local/bin"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
LAN_IP="${CENTAUR_MCP_LAN_IP:-192.168.1.86}"
HTTPS_PORT="${CENTAUR_MCP_HTTPS_PORT:-8443}"
LAN_HTTP_PORT="${CENTAUR_LAN_HTTP_PORT:-8080}"
CADDY_VERSION="2.11.4"
CADDY_ARCHIVE="caddy_${CADDY_VERSION}_linux_amd64.tar.gz"
CADDY_URL="https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/${CADDY_ARCHIVE}"
CADDY_SHA512="8220d1f013b6f27510247b2360c9e0ca9f018feebd82515f07635318b34ff9777ccc8fd0b6e6f2486ce3a33fe389fbb7db12d05baa474f4587509fb4f5ebf1c9"

cd "$PROJECT_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing Python environment: $PYTHON_BIN" >&2
  exit 1
fi

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$TLS_DIR" "$BIN_DIR" "$SYSTEMD_DIR"
chmod 700 "$CONFIG_DIR" "$DATA_DIR" "$TLS_DIR"

if [[ ! -x "$BIN_DIR/caddy" ]]; then
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  curl -fL "$CADDY_URL" -o "$temp_dir/$CADDY_ARCHIVE"
  printf '%s  %s\n' "$CADDY_SHA512" "$temp_dir/$CADDY_ARCHIVE" | sha512sum --check --status
  tar -xzf "$temp_dir/$CADDY_ARCHIVE" -C "$temp_dir" caddy
  install -m 0755 "$temp_dir/caddy" "$BIN_DIR/caddy"
fi

NO_NEW_PRIVILEGES="true"
if [[ "$HTTPS_PORT" == "443" ]]; then
  if ! getcap "$BIN_DIR/caddy" 2>/dev/null | grep -q 'cap_net_bind_service'; then
    echo "Port 443 requires a one-time administrator grant:" >&2
    echo "  sudo setcap cap_net_bind_service=+ep $BIN_DIR/caddy" >&2
    echo "Then run this setup command again." >&2
    exit 1
  fi
  # NoNewPrivileges would intentionally suppress the file capability above.
  NO_NEW_PRIVILEGES="false"
fi

if [[ ! -s "$TLS_DIR/ca.key" || ! -s "$TLS_DIR/ca.crt" ]]; then
  openssl genrsa -out "$TLS_DIR/ca.key" 4096
  chmod 600 "$TLS_DIR/ca.key"
  openssl req -x509 -new -sha256 -days 3650 \
    -key "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" \
    -subj "/CN=CentaurAI Personal Memory LAN CA/O=CentaurAI" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash"
fi

regenerate_leaf=false
if [[ ! -s "$TLS_DIR/server.key" || ! -s "$TLS_DIR/server.crt" ]]; then
  regenerate_leaf=true
elif ! openssl x509 -in "$TLS_DIR/server.crt" -noout -checkend 2592000 >/dev/null; then
  regenerate_leaf=true
elif ! openssl x509 -in "$TLS_DIR/server.crt" -noout -ext subjectAltName | grep -Fq "IP Address:$LAN_IP"; then
  regenerate_leaf=true
fi

if [[ "$regenerate_leaf" == true ]]; then
  openssl genrsa -out "$TLS_DIR/server.key" 3072
  chmod 600 "$TLS_DIR/server.key"
  openssl req -new -sha256 -key "$TLS_DIR/server.key" -out "$TLS_DIR/server.csr" \
    -subj "/CN=$LAN_IP/O=CentaurAI" \
    -addext "subjectAltName=IP:$LAN_IP,DNS:localhost" \
    -addext "extendedKeyUsage=serverAuth" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment"
  openssl x509 -req -sha256 -days 730 \
    -in "$TLS_DIR/server.csr" -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" \
    -CAcreateserial -copy_extensions copy -out "$TLS_DIR/server.crt"
  rm -f "$TLS_DIR/server.csr"
fi
chmod 644 "$TLS_DIR/ca.crt" "$TLS_DIR/server.crt"

if [[ "$HTTPS_PORT" == "443" ]]; then
  PUBLIC_BASE="https://$LAN_IP"
else
  PUBLIC_BASE="https://$LAN_IP:$HTTPS_PORT"
fi

CENTAURAI_DATABASE_DATA_ROOT="$DATABASE_DATA_ROOT" \
CENTAUR_MCP_CONFIG_DIR="$CONFIG_DIR" CENTAUR_MCP_DATA_DIR="$DATA_DIR" \
CENTAUR_MCP_LAN_IP="$LAN_IP" CENTAUR_MCP_HTTPS_PORT="$HTTPS_PORT" \
CENTAUR_LAN_HTTP_PORT="$LAN_HTTP_PORT" \
CENTAUR_MCP_PUBLIC_BASE="$PUBLIC_BASE" "$PYTHON_BIN" - <<'PY'
import os, sys
sys.path.insert(0, "backend")
from mcp_access import get_runtime_config, save_runtime_config

defaults = get_runtime_config()
save_runtime_config({
    "enabled": bool(defaults.get("enabled", False)),
    "mode": defaults.get("mode", "basic"),
    "lan_ip": os.environ["CENTAUR_MCP_LAN_IP"],
    "https_port": int(os.environ["CENTAUR_MCP_HTTPS_PORT"]),
    "lan_http_port": int(os.environ["CENTAUR_LAN_HTTP_PORT"]),
    "public_base": os.environ["CENTAUR_MCP_PUBLIC_BASE"],
    "mcp_port": 8620,
})
PY

cat > "$CONFIG_DIR/Caddyfile" <<EOF
{
    admin off
    auto_https off
}

https://$LAN_IP:$HTTPS_PORT {
    bind $LAN_IP
    tls $TLS_DIR/server.crt $TLS_DIR/server.key
    encode zstd gzip

    @mcp path /mcp/basic /mcp/kb /mcp/full
    handle @mcp {
        reverse_proxy 127.0.0.1:8620
    }

    @mcp_public path / /.well-known/oauth-authorization-server /.well-known/oauth-protected-resource/mcp/basic /.well-known/oauth-protected-resource/mcp/kb /.well-known/oauth-protected-resource/mcp/full /authorize /token /register /revoke /oauth/consent /health /ca.crt
    handle @mcp_public {
        reverse_proxy 127.0.0.1:8620
    }

    @mobile_pages path /mobile /mobile/* /assets/*
    handle @mobile_pages {
        reverse_proxy 127.0.0.1:8618
    }

    @mobile_config_get {
        method GET
        path /api/mobile/config
    }
    handle @mobile_config_get {
        reverse_proxy 127.0.0.1:8618
    }

    @mobile_api {
        path /api/mobile/*
        not path /api/mobile/config /api/mobile/pairing
    }
    handle @mobile_api {
        reverse_proxy 127.0.0.1:8618
    }

    @a2a_api path /api/a2a/* /.well-known/agent-card.json
    handle @a2a_api {
        reverse_proxy 127.0.0.1:8618
    }

    respond "Not found" 404
}

http://$LAN_IP:$LAN_HTTP_PORT {
    bind $LAN_IP
    encode zstd gzip

    @lan_pages path / /lan /lan/*
    handle @lan_pages {
        reverse_proxy 127.0.0.1:8618
    }

    respond "Not found" 404
}
EOF
"$BIN_DIR/caddy" fmt --overwrite "$CONFIG_DIR/Caddyfile"
chmod 600 "$CONFIG_DIR/Caddyfile"

cat > "$SYSTEMD_DIR/centaurAI-memory-mcp.service" <<EOF
[Unit]
Description=CentaurAI Personal Memory MCP service
After=network.target centaurAI-vector-db.service
Wants=centaurAI-vector-db.service

[Service]
Type=simple
WorkingDirectory=$BACKEND_DIR
Environment="CENTAURAI_DATABASE_DATA_ROOT=$DATABASE_DATA_ROOT"
Environment="CENTAUR_MCP_CONFIG_DIR=$CONFIG_DIR"
Environment="CENTAUR_MCP_DATA_DIR=$DATA_DIR"
ExecStart=$PYTHON_BIN $BACKEND_DIR/mcp_remote_server.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$CONFIG_DIR $DATA_DIR

[Install]
WantedBy=default.target
EOF

cat > "$SYSTEMD_DIR/centaurAI-memory-edge.service" <<EOF
[Unit]
Description=CentaurAI Personal Memory LAN edge
After=network.target centaurAI-memory-mcp.service centaurAI-vector-db.service
Wants=centaurAI-memory-mcp.service centaurAI-vector-db.service

[Service]
Type=simple
ExecStart=$BIN_DIR/caddy run --config $CONFIG_DIR/Caddyfile --adapter caddyfile
ExecReload=$BIN_DIR/caddy reload --config $CONFIG_DIR/Caddyfile --adapter caddyfile --force
Restart=on-failure
RestartSec=3
NoNewPrivileges=$NO_NEW_PRIVILEGES
PrivateTmp=true
ProtectSystem=strict
ReadOnlyPaths=$CONFIG_DIR

[Install]
WantedBy=default.target
EOF

"$BIN_DIR/caddy" validate --config "$CONFIG_DIR/Caddyfile" --adapter caddyfile
systemctl --user daemon-reload
systemctl --user enable centaurAI-memory-mcp.service centaurAI-memory-edge.service
systemctl --user restart centaurAI-vector-db.service
systemctl --user restart centaurAI-memory-mcp.service centaurAI-memory-edge.service

echo "Remote MCP is configured at:"
echo "  Basic memory: $PUBLIC_BASE/mcp/basic"
echo "  Knowledge: $PUBLIC_BASE/mcp/kb"
echo "  Full memory: $PUBLIC_BASE/mcp/full"
echo "  CA certificate: $PUBLIC_BASE/ca.crt"
echo "  LAN import: http://$LAN_IP:$LAN_HTTP_PORT/lan"
if [[ "$HTTPS_PORT" != "443" ]]; then
  echo "Port 8443 is used because a user service cannot bind privileged port 443 without administrator permission."
fi
