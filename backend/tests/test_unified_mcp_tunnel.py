"""Real loopback sockets and nested TLS, using ephemeral test-only certificates."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from zhijun_mcp import tunnel


def certificates(root):
    now = datetime.now(timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .sign(key, hashes.SHA256()))
    (root / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    certs = {}
    for host, client in (("localhost", False), ("box.test", False), ("connector", True)):
        private = ec.generate_private_key(ec.SECP256R1())
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(name).public_key(private.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .sign(key, hashes.SHA256()))
        (root / (host + ".key")).write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        (root / (host + ".pem")).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        certs[host] = cert
    return certs


def test_relay_transports_inner_tls_without_seeing_query_or_result(tmp_path, monkeypatch):
    certs = certificates(tmp_path)
    wire = bytearray()

    async def capture(reader, writer):
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), 10)
            if not chunk:
                return
            wire.extend(chunk)
            writer.write(chunk)
            await writer.drain()

    monkeypatch.setattr(tunnel, "pipe", capture)

    def context(host=None, server=False, mtls=False):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH if server else ssl.Purpose.SERVER_AUTH,
                                        cafile=str(tmp_path / "ca.pem"))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        if host:
            ctx.load_cert_chain(str(tmp_path / (host + ".pem")), str(tmp_path / (host + ".key")))
        if mtls:
            ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    async def scenario():
        received = []

        async def box(reader, writer):
            received.append(await reader.readexactly(len(b"QUERY_ONLY_FOR_BOX")))
            writer.write(b"RESULT_ONLY_FOR_AGENT")
            await writer.drain()
            await tunnel.close(writer)

        box_server = await asyncio.start_server(box, "127.0.0.1", 0, ssl=context("box.test", True))
        box_port = box_server.sockets[0].getsockname()[1]
        fingerprint = hashlib.sha256(certs["connector"].public_bytes(serialization.Encoding.DER)).hexdigest()
        relay = tunnel.Relay([tunnel.Entry("box-a", fingerprint, "127.0.0.1", 0)])
        servers = await relay.start("127.0.0.1", 0, context("localhost", True, True))
        relay_port = servers[0].sockets[0].getsockname()[1]
        public_port = servers[1].sockets[0].getsockname()[1]
        # Offline is a closed connection, never plaintext HTTP or another box's data.
        offline_reader, offline_writer = await asyncio.open_connection("127.0.0.1", public_port)
        assert await asyncio.wait_for(offline_reader.read(), 1) == b""
        await tunnel.close(offline_writer)
        connector = tunnel.Connector("localhost", relay_port, context("connector"), local_port=box_port)
        task = asyncio.create_task(connector.run_once())
        try:
            for _ in range(100):
                if "box-a" in relay.controls:
                    break
                await asyncio.sleep(.01)
            assert "box-a" in relay.controls
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", public_port,
                ssl=context(), server_hostname="box.test"), 5)
            # Agent sees BOX certificate, not the cloud relay's tunnel certificate.
            peer = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            assert peer == certs["box.test"].public_bytes(serialization.Encoding.DER)
            writer.write(b"QUERY_ONLY_FOR_BOX")
            await writer.drain()
            result = await asyncio.wait_for(reader.readexactly(len(b"RESULT_ONLY_FOR_AGENT")), 5)
            assert result == b"RESULT_ONLY_FOR_AGENT"
            assert received == [b"QUERY_ONLY_FOR_BOX"]
            await tunnel.close(writer)
            assert wire and b"QUERY_ONLY_FOR_BOX" not in wire and b"RESULT_ONLY_FOR_AGENT" not in wire
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await relay.stop()
            box_server.close()
            await box_server.wait_closed()
    asyncio.run(scenario())
