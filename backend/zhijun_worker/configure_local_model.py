"""Offline, workspace-bound configuration of an explicitly supplied NPU service.

Run from backend: python -m zhijun_worker.configure_local_model --help
The existing worker must be stopped; this command never starts a model service.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat

from .auth import strict_json
from .workspace import Workspace, WorkspaceLock, secure_read


def _existing_workspace(data_root: str, workspace_id: str) -> Workspace:
    root = Path(data_root)
    if (not root.is_absolute() or ".." in root.parts
            or any(path.is_symlink() for path in (root, *root.parents))):
        raise ValueError("WORKSPACE_PATH_INVALID")
    info = root.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        raise ValueError("WORKSPACE_PERMISSIONS_INVALID")
    identity = strict_json(secure_read(root / ".workspace.json", 4096))
    if (type(identity) is not dict
            or set(identity) != {"workspaceId", "accountId", "deviceId", "ownershipEpoch"}
            or identity["workspaceId"] != workspace_id):
        raise ValueError("WORKSPACE_IDENTITY_MISMATCH")
    # This maintenance command does not dispatch or sign requests and never
    # reads the real workspace signing key. WorkspaceLock only needs identity.
    workspace = Workspace(workspace_id, identity["accountId"], identity["deviceId"],
                          identity["ownershipEpoch"], root, bytes(32), root / "worker.sock")
    workspace.validate()
    database = root / "db" / "runtime_settings.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("WORKSPACE_MODEL_PATH_INVALID")
    return workspace


def configure(args) -> dict:
    workspace = _existing_workspace(args.data_root, args.workspace_id)
    lock = WorkspaceLock(workspace)
    lock.acquire()
    try:
        # Explicit paths are set before imports, and passed to the store too.
        # No default data directory, DE config, or secret backend is consulted.
        os.environ["CENTAURAI_DATABASE_DATA_ROOT"] = str(workspace.data_root)
        os.environ["ZHIJUN_WORKSPACE_ID"] = workspace.workspace_id
        from mindos.runtime_config_provider import validate_model_name, validate_ollama_base_url, validate_timeout
        from mindos.stores.runtime_settings_store import RuntimeSettingsStore, RevisionConflictError, SECTION_CHAT, SECTION_MATERIAL

        store = RuntimeSettingsStore(workspace.data_root / "db" / "runtime_settings.db")
        local = store.get_section(SECTION_MATERIAL)
        chat = store.get_section(SECTION_CHAT)
        if args.show:
            # Never return entire rows, profile references, or secret columns.
            return {"workspaceId": workspace.workspace_id,
                    "localRevision": local["revision"] if local else 0,
                    "chatRevision": chat["revision"] if chat else 0,
                    "local": {key: local["payload"].get(key) for key in ("baseUrl", "model", "timeoutSeconds")} if local else None,
                    "chatProvider": chat["payload"].get("provider") if chat else None}
        base_url = validate_ollama_base_url(args.npu_base_url.rstrip("/"))
        model = validate_model_name(args.npu_model)
        timeout = validate_timeout(args.timeout_seconds, 10, 600)
        # Check both revisions before writing either section. The exclusive
        # worker lock prevents live-worker/maintenance configuration races.
        if args.revision != (local["revision"] if local else 0):
            raise RevisionConflictError(local or {"revision": 0})
        if args.activate and args.chat_revision != (chat["revision"] if chat else 0):
            raise RevisionConflictError(chat or {"revision": 0})
        result = store.put_section(SECTION_MATERIAL, args.revision,
            {"baseUrl": base_url, "model": model, "timeoutSeconds": timeout}, secret_ref=None)
        chat_revision = chat["revision"] if chat else 0
        if args.activate:
            # Keep the old secret ledger for normal worker-owned orphan
            # retention. This tool neither reads nor removes credentials.
            active = store.put_section(SECTION_CHAT, args.chat_revision,
                {"provider": "ollama", "externalEnabled": False, "baseUrl": None,
                 "model": None, "timeoutSeconds": 60, "totalBudgetSeconds": 90,
                 "fallbackOllama": False}, secret_ref=None)
            chat_revision = active["revision"]
        return {"workspaceId": workspace.workspace_id, "localRevision": result["revision"],
                "chatRevision": chat_revision, "activated": args.activate,
                "restartWorkerRequired": True}
    finally:
        lock.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Absolute existing workspace data root")
    parser.add_argument("--workspace-id", required=True, help="Expected workspace ID (never inferred)")
    parser.add_argument("--show", action="store_true", help="Show only model settings and revisions")
    parser.add_argument("--npu-base-url", help="Explicit existing Ollama-compatible NPU service URL")
    parser.add_argument("--npu-model", help="Explicit model already available on that NPU service")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--revision", type=int, help="Expected local configuration revision, 0 if absent")
    parser.add_argument("--activate", action="store_true", help="Also select this local service for chat")
    parser.add_argument("--chat-revision", type=int, help="Expected chat revision; required with --activate")
    args = parser.parse_args(argv)
    if args.show and (args.npu_base_url is not None or args.npu_model is not None
                      or args.revision is not None or args.activate or args.chat_revision is not None):
        parser.error("--show cannot be combined with configuration arguments")
    if not args.show and (not args.npu_base_url or not args.npu_model or args.revision is None):
        parser.error("configuration requires --npu-base-url, --npu-model and --revision")
    if args.activate != (args.chat_revision is not None):
        parser.error("--activate and --chat-revision must be supplied together")
    try:
        result = configure(args)
    except BlockingIOError:
        print(json.dumps({"ok": False, "code": "WORKER_RUNNING", "message": "Stop this workspace worker before changing configuration."}))
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        # Do not print DB records/paths/exception payloads that could include
        # restored credentials. Operators can inspect --show after validation.
        print(json.dumps({"ok": False, "code": "WORKSPACE_CONFIGURATION_REJECTED",
                          "message": "Check workspace identity, private absolute paths, model parameters and current revisions."}))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
