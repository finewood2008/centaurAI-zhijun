"""Shared read-only MCP tools for stdio and remote transports."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field


API_BASE = "http://127.0.0.1:8618"
TIMEOUT = httpx.Timeout(60.0, connect=5.0)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _audit(tool_name: str, success: bool, detail: str = "") -> None:
    """Record remote calls when an authenticated HTTP request is active."""
    try:
        from mcp_access import record_tool_audit

        record_tool_audit(tool_name, success, detail)
    except Exception:
        # Auditing must never make a read-only retrieval fail.
        pass


def _request(
    tool_name: str,
    method: Literal["GET", "POST"],
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
            response = client.request(
                method,
                f"{API_BASE}{path}",
                params=params,
                json=payload,
            )
        if response.status_code >= 400:
            _audit(tool_name, False, f"backend_http_{response.status_code}")
            raise ToolError(f"Memory backend rejected the request (HTTP {response.status_code}).")
        result = response.json()
        _audit(tool_name, True)
        return result
    except ToolError:
        raise
    except httpx.TimeoutException as exc:
        _audit(tool_name, False, "backend_timeout")
        raise ToolError("Memory backend timed out.") from exc
    except (httpx.HTTPError, ValueError) as exc:
        _audit(tool_name, False, "backend_unavailable")
        raise ToolError("Memory backend is unavailable or returned an invalid response.") from exc


def memory_get_user_profile() -> dict[str, Any]:
    return _request("memory_get_user_profile", "GET", "/api/memory/files/USER.md")


def memory_get_context(
    agent: Annotated[str, Field(min_length=1, max_length=80)] = "default",
    limit_chars: Annotated[int, Field(ge=500, le=30000)] = 4000,
) -> dict[str, Any]:
    return _request(
        "memory_get_context",
        "GET",
        "/api/memory/context",
        params={"agent": agent, "limit_chars": limit_chars},
    )


def memory_search(
    query: Annotated[str, Field(min_length=1, max_length=2000)],
    n_results: Annotated[int, Field(ge=1, le=50)] = 5,
    memory_type: Annotated[str | None, Field(max_length=80)] = None,
    date_from: Annotated[str | None, Field(max_length=32)] = None,
    date_to: Annotated[str | None, Field(max_length=32)] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query, "n_results": n_results}
    if memory_type:
        payload["memory_type"] = memory_type
    if date_from:
        payload["date_from"] = date_from
    if date_to:
        payload["date_to"] = date_to
    return _request("memory_search", "POST", "/api/memory/search", payload=payload)


def memory_list_files() -> dict[str, Any]:
    return _request("memory_list_files", "GET", "/api/memory/files")


def memory_read_file(
    path: Annotated[str, Field(min_length=1, max_length=500)],
) -> dict[str, Any]:
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ToolError("Invalid memory file path.")
    safe_path = "/".join(parts)
    return _request("memory_read_file", "GET", f"/api/memory/files/{safe_path}")


def kb_search(
    query: Annotated[str, Field(min_length=1, max_length=2000)],
    n_results: Annotated[int, Field(ge=1, le=50)] = 5,
    mode: Literal["text", "visual", "hybrid"] = "text",
    file_type: Annotated[str | None, Field(max_length=80)] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": query,
        "n_results": n_results,
        "mode": mode,
    }
    if file_type:
        payload["file_type"] = file_type
    return _request("kb_search", "POST", "/api/search", payload=payload)


def kb_get_stats() -> dict[str, Any]:
    return _request("kb_get_stats", "GET", "/api/stats")


def kb_list_documents(
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
    offset: Annotated[int, Field(ge=0, le=1000000)] = 0,
) -> dict[str, Any]:
    return _request(
        "kb_list_documents",
        "GET",
        "/api/documents",
        params={"limit": limit, "offset": offset},
    )


def kb_health() -> dict[str, Any]:
    return _request("kb_health", "GET", "/api/health")


KB_TOOLS = [
    (
        kb_search,
        "Search the local vector knowledge base, including documents, OCR text, images, audio, and video.",
    ),
    (kb_get_stats, "Return index and document statistics for the local knowledge base."),
    (kb_list_documents, "List indexed documents from the local knowledge base."),
    (kb_health, "Return backend health and capability flags."),
]

MEMORY_TOOLS = [
    (
        memory_get_user_profile,
        "Return the owner's shared profile for identity, preferred address, timezone, role, and preferences.",
    ),
    (
        memory_get_context,
        "Return startup-style context containing shared rules, profile, long-term memory, recent journals, and agent imports.",
    ),
    (
        memory_search,
        "Semantically search memory files and journals for preferences, prior decisions, and ongoing work.",
    ),
    (memory_list_files, "List memory files available for read-only access."),
    (memory_read_file, "Read one memory file by its relative path."),
]

BASIC_TOOLS = [MEMORY_TOOLS[1], MEMORY_TOOLS[2], *KB_TOOLS]


def create_mcp_server(*, profile: Literal["basic", "kb", "full"], **kwargs: Any) -> FastMCP:
    """Create a vendor-neutral MCP server with a stable tool schema."""
    if profile == "kb":
        instructions = "Use these read-only tools to retrieve information from the owner's local knowledge base."
        name = "centaurai-knowledge-base"
        selected = KB_TOOLS
    elif profile == "basic":
        instructions = (
            "Use these read-only tools to search the owner's memory and knowledge base, "
            "and retrieve a bounded agent context. Raw profile and file access are not available."
        )
        name = "centaurai-personal-memory-basic"
        selected = BASIC_TOOLS
    else:
        instructions = (
            "Use these read-only tools to retrieve the owner's profile, long-term memory, "
            "agent context, and local knowledge. Search memory for prior preferences and "
            "decisions, and search the knowledge base for indexed local material."
        )
        name = "centaurai-personal-memory"
        selected = MEMORY_TOOLS + KB_TOOLS
    server = FastMCP(
        name,
        instructions=instructions,
        **kwargs,
    )
    for function, description in selected:
        server.add_tool(
            function,
            description=description,
            annotations=READ_ONLY,
            structured_output=True,
        )
    return server
