"""Restricted reverse TCP tunnel. Inner Agent-to-box TLS is never terminated here.

One provisioned public listener per box; a separate mTLS listener authenticates
outbound box channels by pinned certificate fingerprint. No target URL, filesystem
path, HTTP request or query can be submitted as a tunnel parameter.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import ssl
from dataclasses import dataclass


async def close(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 2)
    except (OSError, ssl.SSLError, asyncio.TimeoutError):
        pass


async def read_message(reader):
    raw = await asyncio.wait_for(reader.readuntil(b"\n"), 15)
    if len(raw) > 512:
        raise ValueError("TUNNEL_FRAME_TOO_LARGE")
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise ValueError("TUNNEL_FRAME_INVALID")
    return message


async def write_message(writer, message):
    writer.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
    await writer.drain()


async def pipe(reader, writer):
    while True:
        chunk = await asyncio.wait_for(reader.read(65536), 120)
        if not chunk:
            return
        writer.write(chunk)
        await asyncio.wait_for(writer.drain(), 30)


async def bridge(left_reader, left_writer, right_reader, right_writer):
    tasks = [asyncio.create_task(pipe(left_reader, right_writer)),
             asyncio.create_task(pipe(right_reader, left_writer))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(close(left_writer), close(right_writer), return_exceptions=True)


@dataclass(frozen=True)
class Entry:
    box_id: str
    fingerprint: str
    host: str
    port: int


class Relay:
    def __init__(self, entries, *, capacity=32):
        if len({e.fingerprint for e in entries}) != len(entries):
            raise ValueError("TUNNEL_DUPLICATE_IDENTITY")
        self.entries = list(entries)
        self.identities = {e.fingerprint: e.box_id for e in entries}
        self.controls, self.pending, self.servers = {}, {}, []
        self.active = {e.box_id: 0 for e in entries}
        self.capacity = capacity

    async def accept_tunnel(self, reader, writer):
        box = None
        handed_off = False
        try:
            certificate = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            box = self.identities.get(hashlib.sha256(certificate).hexdigest())
            if not box:
                return
            message = await read_message(reader)
            if message == {"kind": "control"}:
                # A new control connection cannot hijack live ownership.
                if box in self.controls:
                    return
                self.controls[box] = writer
                while True:
                    message = await read_message(reader)
                    if message != {"kind": "ping"}:
                        return
                    await write_message(writer, {"kind": "pong"})
            elif set(message) == {"kind", "nonce"} and message["kind"] == "data":
                key = (box, message["nonce"])
                future = self.pending.pop(key, None)
                if future is None or future.done():
                    return
                future.set_result((reader, writer))
                handed_off = True
        except (ValueError, TypeError, KeyError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # Never log frame bytes, private requests, or exception details.
        finally:
            if box and self.controls.get(box) is writer:
                self.controls.pop(box, None)
            if not handed_off:
                await close(writer)

    async def accept_public(self, box, reader, writer):
        control = self.controls.get(box)
        if not control or self.active[box] >= self.capacity:
            await close(writer)
            return
        nonce = secrets.token_hex(24)
        key = (box, nonce)
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        self.active[box] += 1
        remote = None
        try:
            await write_message(control, {"kind": "open", "nonce": nonce})
            remote = await asyncio.wait_for(future, 10)
            await bridge(reader, writer, *remote)
        except (OSError, ValueError, asyncio.TimeoutError):
            pass
        finally:
            self.pending.pop(key, None)
            self.active[box] -= 1
            await close(writer)
            if remote:
                await close(remote[1])

    async def start(self, host, port, context):
        if context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("TUNNEL_MTLS_REQUIRED")
        self.servers.append(await asyncio.start_server(self.accept_tunnel, host, port, ssl=context, limit=1024,
                                                       ssl_handshake_timeout=10))
        for entry in self.entries:
            async def accept(reader, writer, box=entry.box_id):
                await self.accept_public(box, reader, writer)
            self.servers.append(await asyncio.start_server(accept, entry.host, entry.port))
        return self.servers

    async def stop(self):
        for server in self.servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in self.servers))
        await asyncio.gather(*(close(writer) for writer in list(self.controls.values())), return_exceptions=True)


class Connector:
    def __init__(self, relay_host, relay_port, context, *, local_port=8643, capacity=32):
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError("TUNNEL_SERVER_VERIFICATION_REQUIRED")
        self.host, self.port, self.context, self.local_port = relay_host, relay_port, context, local_port
        self.capacity, self.tasks = capacity, set()

    async def connect_data(self, nonce):
        remote_writer, local_writer = None, None
        try:
            remote_reader, remote_writer = await asyncio.wait_for(asyncio.open_connection(
                self.host, self.port, ssl=self.context, server_hostname=self.host, limit=1024), 10)
            await write_message(remote_writer, {"kind": "data", "nonce": nonce})
            # The inner TLS bytes are copied to the fixed box listener unchanged.
            local_reader, local_writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.local_port), 10)
            await bridge(remote_reader, remote_writer, local_reader, local_writer)
        except (OSError, ValueError, asyncio.TimeoutError):
            pass
        finally:
            if remote_writer:
                await close(remote_writer)
            if local_writer:
                await close(local_writer)

    async def run_once(self):
        reader, writer = await asyncio.open_connection(self.host, self.port, ssl=self.context, server_hostname=self.host, limit=1024)
        async def ping():
            while True:
                await write_message(writer, {"kind": "ping"})
                await asyncio.sleep(5)
        heartbeat = None
        try:
            await write_message(writer, {"kind": "control"})
            heartbeat = asyncio.create_task(ping())
            while True:
                message = await read_message(reader)
                if message == {"kind": "pong"}:
                    continue
                if (set(message) != {"kind", "nonce"} or message["kind"] != "open"
                        or not isinstance(message["nonce"], str) or len(message["nonce"]) != 48):
                    raise ValueError("TUNNEL_FRAME_INVALID")
                if len(self.tasks) >= self.capacity:
                    continue
                task = asyncio.create_task(self.connect_data(message["nonce"]))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await close(writer)
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def run(self):
        while True:
            try:
                await self.run_once()
            except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                await asyncio.sleep(5)


def main():
    import argparse
    from zhijun_worker.workspace import secure_read
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("relay", "connector"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(secure_read(args.config, 65536))
    if config.get("enabled") is not True:
        raise SystemExit("TUNNEL_DISABLED")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH if args.mode == "relay" else ssl.Purpose.SERVER_AUTH,
                                        cafile=config["caFile"])
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    secure_read(config["keyFile"], 32768)
    context.load_cert_chain(config["certFile"], config["keyFile"])
    if args.mode == "connector":
        connector = Connector(config["relayHost"], config["relayPort"], context, local_port=config.get("localPort", 8643))
        asyncio.run(connector.run())
    else:
        async def serve():
            relay = Relay([Entry(**item) for item in config["entries"]])
            await relay.start(config["host"], config["port"], context)
            try:
                await asyncio.Event().wait()
            finally:
                await relay.stop()
        asyncio.run(serve())


if __name__ == "__main__":
    main()
