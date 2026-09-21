"""Reference stdio adapter to the SAME HTTPS service, using SDK OAuth + PKCE.

Tokens live only in this process. Reconnecting after exit opens OAuth again.
stdout is exclusively MCP; credentials and data are never written to logs.
"""
import argparse
import asyncio
import logging
from urllib.parse import parse_qs, urlsplit
import webbrowser

import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.auth import OAuthClientMetadata


class MemoryTokens:
    def __init__(self):
        self.tokens, self.client = None, None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, client_info):
        self.client = client_info


async def run(resource):
    if urlsplit(resource).scheme != "https" or urlsplit(resource).path != "/mcp":
        raise ValueError("HTTPS_MCP_REQUIRED")
    callback_result = asyncio.get_running_loop().create_future()

    async def callback(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readuntil(b"\r\n"), 5)
            method, target, _ = line.decode("ascii").strip().split(" ")
            parsed = urlsplit(target)
            query = parse_qs(parsed.query)
            if method != "GET" or parsed.path != "/callback" or callback_result.done():
                raise ValueError()
            if len(query.get("code", [])) != 1 or len(query.get("state", [])) != 1:
                raise ValueError()
            callback_result.set_result((query["code"][0], query["state"][0]))
            body = b"Authorization received. You can close this window."
            writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nCache-Control: no-store\r\nContent-Type: text/plain\r\n\r\n" + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    listener = await asyncio.start_server(callback, "127.0.0.1", 0, limit=8192)
    port = listener.sockets[0].getsockname()[1]

    async def redirect(url):
        # SDK validates OAuth discovery/PKCE. Do not print authorization URLs.
        if urlsplit(url).scheme != "https" or not webbrowser.open(url):
            raise ValueError("BROWSER_AUTHORIZATION_REQUIRED")

    async def receive_callback():
        return await asyncio.wait_for(callback_result, 300)

    auth = OAuthClientProvider(resource, OAuthClientMetadata(client_name="知君 stdio 适配器",
        redirect_uris=[f"http://127.0.0.1:{port}/callback"], token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"], response_types=["code"], scope="zhijun:read"),
        MemoryTokens(), redirect_handler=redirect, callback_handler=receive_callback)
    try:
        async with httpx.AsyncClient(auth=auth, timeout=60, trust_env=False, follow_redirects=False) as http:
            async with streamable_http_client(resource, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    server = Server("zhijun-stdio-adapter")

                    @server.list_tools()
                    async def tools():
                        return (await session.list_tools()).tools

                    @server.call_tool()
                    async def call(name, arguments):
                        return await session.call_tool(name, arguments)

                    async with stdio_server() as (local_read, local_write):
                        await server.run(local_read, local_write, server.create_initialization_options())
    finally:
        listener.close()
        await listener.wait_closed()


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument("resource")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.resource))
    except Exception:
        raise SystemExit("知君连接未完成：请检查盒子在线状态、授权期限和 MCP 地址。") from None


if __name__ == "__main__":
    main()
