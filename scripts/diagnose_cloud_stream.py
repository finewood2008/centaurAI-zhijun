"""Content-free SSE diagnostics; no provider requests on import.

The optional box probe uses a fixed synthetic prompt and the installed DE
transport/parser. It never sends conversation history or modifies live settings.
"""
from __future__ import annotations

import json
from collections import Counter


EVENT_TYPES = frozenset({
    "chat.completion.chunk", "response.created", "response.in_progress",
    "response.output_text.delta", "response.output_text.done",
    "response.output_item.added", "response.output_item.done",
    "response.content_part.added", "response.content_part.done",
    "response.reasoning_text.delta", "response.reasoning_summary_text.delta",
    "response.refusal.delta", "response.function_call_arguments.delta",
    "response.completed", "response.done", "response.incomplete",
    "response.failed", "response.cancelled", "error",
})
FINISH_REASONS = frozenset({
    "stop", "length", "max_output_tokens", "tool_calls", "function_call",
    "content_filter", "completed", "failed", "cancelled",
})


def text_size(value):
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(len(part["text"]) for part in value
                   if isinstance(part, dict) and isinstance(part.get("type"), str)
                   and part["type"] in {"text", "output_text"}
                   and isinstance(part.get("text"), str))
    return 0


class StreamAudit:
    """Retain counts only; bounded unfinished SSE frame, never return raw values."""

    def __init__(self):
        self.data = []
        self.name = None
        self.frame_bytes = 0
        self.dropping = False
        self.frames = self.invalid_frames = 0
        self.event_types = Counter()
        self.delta_chars = self.final_chars = self.reasoning_chars = 0
        self.refusal_seen = self.tool_calls_seen = self.terminal_seen = False
        self.finish_reason = None
        self.usage = {}

    def feed(self, line):
        line = line.rstrip("\r\n")
        if not line:
            self.finish()
        elif line.startswith("event:"):
            name = line[6:].strip()
            self.name = name if name in EVENT_TYPES else "unknown"
        elif line.startswith("data:") and not self.dropping:
            self.frame_bytes += len(line.encode("utf-8"))
            if self.frame_bytes > 1024 * 1024:
                self.invalid_frames += 1
                self.data.clear()
                self.dropping = True
            else:
                self.data.append(line[5:].lstrip())

    def finish(self):
        data, name, dropping = self.data, self.name, self.dropping
        self.data, self.name, self.frame_bytes, self.dropping = [], None, 0, False
        if not data or dropping:
            return
        raw = "\n".join(data)
        if raw == "[DONE]":
            self.terminal_seen = True
            return
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError):
            self.invalid_frames += 1
            return
        if not isinstance(event, dict):
            self.invalid_frames += 1
            return
        self.frames += 1
        kind = event.get("type") or name or "chat.completion.chunk"
        kind = kind if isinstance(kind, str) and kind in EVENT_TYPES else "unknown"
        self.event_types[kind] += 1
        if kind == "response.output_text.delta":
            self.delta_chars += text_size(event.get("delta"))
        if kind in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
            self.reasoning_chars += text_size(event.get("delta"))
        if kind == "response.output_text.done":
            self.final_chars = max(self.final_chars, text_size(event.get("text")))
        if kind == "response.refusal.delta":
            self.refusal_seen = True
        if kind == "response.function_call_arguments.delta":
            self.tool_calls_seen = True
        if kind == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "message":
                self.final_chars = max(self.final_chars, text_size(item.get("content")))
        if kind == "response.content_part.done":
            self.final_chars = max(self.final_chars, text_size([event.get("part")]))
        choices = event.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            delta = choice.get("delta")
            if isinstance(delta, dict):
                self.delta_chars += text_size(delta.get("content"))
                self.reasoning_chars += text_size(delta.get("reasoning_content"))
                self.refusal_seen |= bool(delta.get("refusal"))
                self.tool_calls_seen |= bool(delta.get("tool_calls") or delta.get("function_call"))
            message = choice.get("message")
            if isinstance(message, dict):
                self.final_chars = max(self.final_chars, text_size(message.get("content")))
                self.refusal_seen |= bool(message.get("refusal"))
                self.tool_calls_seen |= bool(message.get("tool_calls"))
            if choice.get("finish_reason") is not None:
                self._terminal(choice["finish_reason"])
        response = event.get("response")
        if not isinstance(response, dict):
            response = {}
        self.final_chars = max(self.final_chars, text_size(response.get("output_text")))
        output = response.get("output")
        if isinstance(output, list):
            size = 0
            for item in output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "message":
                    size += text_size(item.get("content"))
                    parts = item.get("content")
                    if isinstance(parts, list):
                        self.refusal_seen |= any(isinstance(p, dict) and p.get("type") == "refusal" for p in parts)
                self.tool_calls_seen |= item.get("type") == "function_call"
            self.final_chars = max(self.final_chars, size)
        if kind in {"response.completed", "response.done", "response.incomplete", "response.failed", "response.cancelled"}:
            details = response.get("incomplete_details") or event.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            self._terminal(reason or kind.split(".")[-1])
        self._usage(response.get("usage"))
        self._usage(event.get("usage"))

    def _terminal(self, reason):
        self.terminal_seen = True
        self.finish_reason = reason if isinstance(reason, str) and reason in FINISH_REASONS else "unknown"

    def _usage(self, usage):
        if not isinstance(usage, dict):
            return
        values = {
            "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
            "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
        }
        details = usage.get("output_tokens_details", usage.get("completion_tokens_details"))
        if isinstance(details, dict):
            values["reasoning_tokens"] = details.get("reasoning_tokens")
        for key, value in values.items():
            if type(value) is int and 0 <= value <= 10000000:
                self.usage[key] = value

    def summary(self):
        classification = ("visible_delta" if self.delta_chars else "final_only" if self.final_chars
                          else "reasoning_only" if self.reasoning_chars else "empty")
        return {"frames": self.frames, "event_types": dict(self.event_types),
                "delta_chars": self.delta_chars, "final_chars": self.final_chars,
                "reasoning_chars": self.reasoning_chars, "refusal_seen": self.refusal_seen,
                "tool_calls_seen": self.tool_calls_seen, "finish_reason": self.finish_reason,
                "usage": dict(self.usage), "terminal_seen": self.terminal_seen,
                "invalid_frames": self.invalid_frames, "classification": classification}


def probe(args):
    """One paid synthetic request; observe the very stream DE parses, not a retry."""
    import contextlib
    import hashlib
    import os
    import re
    from pathlib import Path
    import sqlite3
    import sys
    import tempfile
    import time
    from unittest.mock import patch

    if not args.allow_network:
        raise RuntimeError("NETWORK_OPT_IN_REQUIRED")
    if os.environ.get("CENTAUR_MANAGED_MODEL_CONFIG"):
        # Managed routing requires real subject/lease context; never fake it.
        raise RuntimeError("MANAGED_ROUTE_NOT_SUPPORTED")
    source = Path(args.runtime_db).absolute()
    if (source.name != "runtime.db" or source.is_symlink() or not source.is_file()
            or source.parent.parent.name != "zhijun_models"
            or not re.fullmatch(r"[a-f0-9]{64}", source.parent.name)):
        raise RuntimeError("RUNTIME_SOURCE_INVALID")
    if any(p.is_symlink() for p in source.parents):
        raise RuntimeError("RUNTIME_SOURCE_INVALID")
    sys.path.insert(0, args.de_backend)
    from mindos.zhijun_capabilities import model_stream
    from mindos.zhijun_capabilities.workspace_models_store import WorkspaceModelsStore, WorkspaceRuntimeProvider
    from mindos.secret_store import EncryptedSQLiteSecretStore
    from mindos.runtime_config_provider import LocalOllamaSnapshot

    # No live store constructor: all DDL/chmod occur on an ephemeral copy.
    with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
        live.execute("PRAGMA query_only=ON")
        version = live.execute("PRAGMA data_version").fetchone()[0]

        def unchanged():
            if live.execute("PRAGMA data_version").fetchone()[0] != version:
                raise RuntimeError("CONFIGURATION_CHANGED")

        with tempfile.TemporaryDirectory(prefix="zhijun-stream-diagnostic-") as directory:
            target = Path(directory) / "runtime.db"
            with contextlib.closing(sqlite3.connect(target)) as copy:
                live.backup(copy)
                if not os.environ.get("CENTAUR_SECRET_STORE_KEY"):
                    # Reject instead of allowing a loader to invent a missing key.
                    row = copy.execute("SELECT value FROM secret_store_metadata WHERE key='managed_fernet_key_v1'").fetchone()
                    if not row:
                        raise RuntimeError("SECRET_KEY_MISSING")
            unchanged()
            runtime = WorkspaceRuntimeProvider(
                WorkspaceModelsStore(target), EncryptedSQLiteSecretStore(db_path=target),
                LocalOllamaSnapshot("http://127.0.0.1:1", "diagnostic-no-local", 1, 0, 4096))
            snap = runtime.get_chat_snapshot()
            if not snap.external_enabled or snap.provider != "openai":
                raise RuntimeError("ACTIVE_OPENAI_PROFILE_REQUIRED")
            if snap.secret_ref and snap.secret_ref.startswith("managed:"):
                raise RuntimeError("MANAGED_ROUTE_NOT_SUPPORTED")
            key = runtime.resolve_api_key(snap)
            if not key:
                raise RuntimeError("SECRET_KEY_MISSING")
            request = {"system": "This is a synthetic connectivity test. No personal data is included.",
                       "messages": [{"role": "user", "content": (
                           "请用约200个汉字，从行动、关系和体验三个角度讨论如何定义成功。"
                           if args.scenario == "reflection" else "只回答：连接测试成功")}],
                       "max_tokens": args.max_tokens, "temperature": 0, "effort": "low"}
            worker_budget_hash = None
            if getattr(args, "use_worker_budget", False):
                # Read the deployed pure policy without importing the worker's
                # service, opening its domain stores, or faking user consent.
                from dataclasses import dataclass
                import importlib.util
                from types import SimpleNamespace
                budget_path = Path("/opt/acceptance/zhijun/backend/mindos/zhijun/request_budget.py")
                worker_budget_hash = hashlib.sha256(budget_path.read_bytes()).hexdigest()
                spec = importlib.util.spec_from_file_location("diagnostic_worker_budget", budget_path)
                policy = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(policy)
                @dataclass(frozen=True)
                class BudgetRequest:
                    max_tokens: int
                effective = policy.normalize_request_budget(BudgetRequest(request["max_tokens"]),
                    SimpleNamespace(external=True, name="openai", model=snap.model))
                request["max_tokens"] = effective.max_tokens
            request = model_stream.validate_request(request)
            audit = StreamAudit()
            original_lines = model_stream._lines
            started = time.monotonic()
            first_frame = first_text = None
            parsed_chars = 0
            adapter_result = "interrupted"
            adapter_usage = {}
            response_meta = {}

            def observed_lines(response, checked):
                nonlocal first_frame
                status = getattr(response, "status", None)
                response_meta["http_status"] = status if type(status) is int else None
                media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                response_meta["media_type"] = media if media in {"text/event-stream", "application/json"} else "other"
                for line in original_lines(response, checked):
                    audit.feed(line)
                    if audit.frames and first_frame is None:
                        first_frame = round(time.monotonic() - started, 3)
                    yield line
                audit.finish()

            # The DE deadline watcher invokes check in another thread. Give it
            # a fresh read-only connection (sqlite connections are thread-bound).
            last_check = [0.0]
            import threading
            check_lock = threading.Lock()
            source_stat = source.stat()
            def check():
                with check_lock:
                    now = time.monotonic()
                    if now - last_check[0] < .25:
                        return
                    last_check[0] = now
                    stat = source.stat()
                    if (stat.st_dev, stat.st_ino) != (source_stat.st_dev, source_stat.st_ino):
                        raise RuntimeError("CONFIGURATION_CHANGED")
                    # Fresh snapshots compare only configuration and encrypted
                    # credentials, not outbound receipts written by normal chat.
                    with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
                        if configuration_digest(db) != baseline:
                            raise RuntimeError("CONFIGURATION_CHANGED")

            def configuration_digest(db):
                result = hashlib.sha256()
                for table in ("runtime_settings", "external_profiles", "encrypted_secrets", "secret_store_metadata"):
                    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    if exists:
                        for row in db.execute('SELECT * FROM "' + table + '" ORDER BY 1'):
                            result.update(repr(row).encode())
                return result.digest()

            baseline = configuration_digest(live)
            unchanged()
            try:
                with patch.object(model_stream, "_lines", observed_lines):
                    for event in model_stream.stream(protocol="openai", base_url=snap.base_url,
                            model=snap.model, key=key, request=request, check=check,
                            timeout=min(30, snap.timeout_seconds), budget=min(60, snap.total_budget_seconds)):
                        if event.get("type") == "text":
                            parsed_chars += text_size(event.get("text"))
                            if first_text is None:
                                first_text = round(time.monotonic() - started, 3)
                        elif event.get("type") == "usage":
                            adapter_usage = {k: event[k] for k in ("input_tokens", "output_tokens")
                                             if type(event.get(k)) is int}
                        elif event.get("type") == "done":
                            adapter_result = "done"
            except Exception as exc:
                detail = getattr(exc, "detail", None)
                code = detail.get("code") if isinstance(detail, dict) else None
                adapter_result = code if code in {
                    "MODEL_RESPONSE_EMPTY", "MODEL_STREAM_INCOMPLETE", "MODEL_RESPONSE_INVALID",
                    "MODEL_TIMEOUT", "MODEL_TOTAL_TIMEOUT", "MODEL_QUEUE_TIMEOUT", "MODEL_PROVIDER_FAILED",
                    "MODEL_STREAM_MEDIA_TYPE_INVALID", "MODEL_RESPONSE_TOO_LARGE", "MODEL_EVENT_TOO_LARGE",
                } else "diagnostic_failed"
                if isinstance(exc, RuntimeError) and exc.args == ("CONFIGURATION_CHANGED",):
                    adapter_result = "CONFIGURATION_CHANGED"
            finally:
                audit.finish()
            return {"scenario": args.scenario, "max_tokens": request["max_tokens"],
                    "requested_max_tokens": args.max_tokens, "worker_budget_sha256": worker_budget_hash, "effort": "low",
                    "adapter_sha256": hashlib.sha256(Path(model_stream.__file__).read_bytes()).hexdigest(),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "first_frame_seconds": first_frame, "first_text_seconds": first_text,
                    "adapter_result": adapter_result, "adapter_chars": parsed_chars,
                    "adapter_usage": adapter_usage, "response": response_meta, "wire": audit.summary()}


def main():
    import argparse
    import contextlib
    import io
    import logging
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-network", action="store_true", help="One potentially billable synthetic request")
    parser.add_argument("--runtime-db", required=True, help="Exact workspace runtime.db inside the box")
    parser.add_argument("--de-backend", default="/opt/mindos/app/backend")
    parser.add_argument("--scenario", choices=("short", "reflection"), default="short")
    parser.add_argument("--max-tokens", type=int, choices=(1024, 4096), default=1024)
    parser.add_argument("--use-worker-budget", action="store_true",
                        help="Apply the deployed worker policy to the synthetic request before DE validation")
    args = parser.parse_args()
    if not args.allow_network:
        parser.error("--allow-network is required; no request was sent")
    # Third-party import/errors must not bypass the output allowlist.
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            report = probe(args)
    except Exception:
        print(json.dumps({"adapter_result": "diagnostic_setup_failed", "request_may_have_started": True}))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["adapter_result"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
