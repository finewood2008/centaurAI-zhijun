"""CentaurOS-managed entry point. Root-provisioned config, TLS ends on this box."""
import argparse
import json
from pathlib import Path
import ssl
from urllib.parse import urlsplit
import uvicorn
from jwt import PyJWK
from zhijun_worker.workspace import secure_read

from .auth import TokenVerifier
from .models import Subject
from .gateway import Gateway
from .server import create_server
from .account import AccountBroker
from .browser import ConsentBrowser
from .http import protect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(secure_read(args.config, 16384))
    if config.get("enabled") is not True:
        raise SystemExit("MCP_DISABLED")
    binding = Subject.model_validate(config["subject"])
    resource, issuer = config["resource"], config["issuer"]
    # JWKS provisioned by the box manager; no untrusted jku/x5u URLs or token-driven fetches.
    def key_resolver(token):
        import jwt
        kid = jwt.get_unverified_header(token).get("kid")
        keys = json.loads(secure_read(config["issuerJwksFile"], 65536))["keys"]
        selected = [key for key in keys if key.get("kid") == kid and key.get("use", "sig") == "sig"]
        if len(selected) != 1:
            raise ValueError("ISSUER_KEY_UNKNOWN")
        return PyJWK.from_dict(selected[0]).key
    gateway = Gateway(Path(config["gatewaySocket"]), secure_read(config["gatewayKeyFile"], 32), binding)
    server = create_server(resource=resource, issuer=issuer,
        verifier=TokenVerifier(issuer, resource, key_resolver), gateway=gateway)
    context = ssl.create_default_context(cafile=config.get("accountCaFile"))
    secure_read(config["accountClientKeyFile"], 32768)
    context.load_cert_chain(config["accountClientCertFile"], config["accountClientKeyFile"])
    broker = AccountBroker(issuer, resource, context, key_resolver)
    origin = "https://" + urlsplit(resource).netloc
    browser = ConsentBrowser(gateway, broker, origin)
    app = server.streamable_http_app()
    app.router.routes.extend(browser.routes())
    protect(app, resource)
    # Listener remains loopback; authenticated outbound connector transports raw TLS here.
    # Access logs disabled: consent tickets and request URLs must never reach logs.
    secure_read(config["tlsKeyFile"], 32768)
    uvicorn.run(app, host="127.0.0.1", port=config.get("port", 8643),
        ssl_certfile=config["tlsCertFile"], ssl_keyfile=config["tlsKeyFile"],
        access_log=False, proxy_headers=False, log_level="warning")


if __name__ == "__main__":
    main()
