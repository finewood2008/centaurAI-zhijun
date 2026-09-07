#!/usr/bin/env python3
"""Build a reviewable source-only DE/worker/Agent release; never deploy or read runtime roots."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import subprocess
import tarfile
import tempfile

MAX_SOURCE = 8 * 1024 * 1024
MAX_BINARY = 256 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
MAX_FILES = 10000
CATALOG = "frontend/shared/product-operations.json"
DENIED_DIRS = {"data", "secrets", "secret", "uploads", "downloads", "logs",
    "tests", "fixtures", "consumer_api", "node_modules", "__pycache__", "vendor", "native",
    "weights", "chroma_data", "watch_folder", "wiki", "gbrain_data", "video_frames", "video_work"}
RUNTIME_RESOURCE_DIRS = {"config", "configs", "runtime", "memory", "models", "model"}
NEW_SOURCE = {
    "zhijun": ("backend/zhijun_worker/", "backend/mindos/domain_scope.py", CATALOG),
    "de": ("backend/mindos/zhijun_capabilities/", "backend/mindos/zhijun_gateway/",
           "backend/mindos/services/protected_material_extract.py"),
}
PRIVATE_PEM = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
KEY_ASSIGNMENT = re.compile(rb'''(?i)(?:api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|password)["']?\s*[:=]\s*["']([A-Za-z0-9+/_.=-]{16,})["']''')


class BuildError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def safe_relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value) or str(path) != value:
        raise BuildError("unsafe relative path")
    return path


def safe_read(path, maximum):
    """Open each component without following links, including racing directory substitutions."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise BuildError("input paths must be absolute and canonical")
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
                raise BuildError("non-regular, hard-linked, or oversized input: " + path.name)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(maximum + 1)
            after = os.fstat(fd)
            if len(data) > maximum or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise BuildError("input changed while reading: " + path.name)
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode:
        raise BuildError("git metadata query failed: " + args[0])
    return result.stdout


def paths(root, *args):
    return {value.decode("utf-8") for value in git(root, *args).split(b"\0") if value}


def permitted(relative):
    path = safe_relative(relative)
    if any(part.startswith(".") or part.lower() in DENIED_DIRS or part.startswith(("_tmp", "_smoke")) for part in path.parts):
        return False
    if path.name in {"conftest.py", "pytest.ini"} or path.name.startswith(("test_", "_seed_", "_tmp_")):
        return False
    if path.suffix != ".py" and (path.stem.lower() in {"config", "settings", "runtime-config", "runtime_config", "credentials", "secrets"}
                               or path.name.endswith((".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3"))):
        return False
    if path.suffix != ".py" and any(part in RUNTIME_RESOURCE_DIRS for part in path.parts):
        return False
    if relative == CATALOG:
        return True
    if relative.startswith("governance/"):
        return path.suffix == ".json"
    if not relative.startswith("backend/"):
        return False
    if path.suffix == ".py":
        return True
    if path.name in {"requirements.txt", "requirements-lock.txt", "pyproject.toml"}:
        return True
    return (any(part in {"resources", "prompts", "templates", "schemas"} for part in path.parts)
            and path.suffix in {".json", ".yaml", ".yml", ".txt", ".md", ".html", ".css", ".sql", ".j2"})


def secret_check(data, name):
    if PRIVATE_PEM.search(data) or KEY_ASSIGNMENT.search(data):
        raise BuildError("private key or embedded credential rejected: " + name)
    if b"\x00" in data:
        raise BuildError("binary content masquerading as source: " + name)
    try:
        data.decode("utf-8")
    except UnicodeError:
        raise BuildError("non-UTF8 source/resource: " + name) from None


def state(root, selected):
    tracked_diff = git(root, "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD", "--", *sorted(selected))
    status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    return {"head": git(root, "rev-parse", "HEAD").decode().strip(), "dirty": bool(status),
            "workingTreeStatusSha256": digest(status), "packagedTrackedDiffSha256": digest(tracked_diff),
            "diffScope": "Only selected tracked source paths; excluded runtime files are never read for diff hashing."}


def audit_local_dependencies(files, role, repository_names):
    """Check repository-local imports and literal __file__-relative resources without importing application code."""
    import ast
    prefix = role + "/"
    included = {name.removeprefix(prefix) for name in files}
    available = {name for name in repository_names if name.startswith("backend/") and name.endswith(".py")}
    verified_resources, imports, python_files = set(), 0, 0

    def resolve_module(parts):
        return {"backend/" + "/".join(parts) + ".py", "backend/" + "/".join(parts) + "/__init__.py"} & available

    for name, (data, _, _) in files.items():
        relative = name.removeprefix(prefix)
        if not relative.endswith(".py"):
            continue
        python_files += 1
        tree = ast.parse(data, filename=relative)
        package = list(PurePosixPath(relative).parts[1:-1])
        for node in ast.walk(tree):
            candidates = set()
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    for length in range(1, len(parts) + 1):
                        candidates |= resolve_module(parts[:length])
            elif isinstance(node, ast.ImportFrom):
                base = (package[:len(package) - node.level + 1] if node.level else []) + ((node.module or "").split(".") if node.module else [])
                candidates |= resolve_module(base)
                for alias in node.names:
                    candidates |= resolve_module(base + alias.name.split("."))
            missing = candidates - included
            if missing:
                raise BuildError("excluded repository import used by " + name + ": " + ",".join(sorted(missing)))
            imports += len(candidates)

        environment = {}
        def literal_path(node):
            if isinstance(node, ast.Name):
                return environment.get(node.id)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "Path" and len(node.args) == 1 and isinstance(node.args[0], ast.Name) and node.args[0].id == "__file__":
                    return PurePosixPath(relative)
                if isinstance(node.func, ast.Attribute) and node.func.attr in {"resolve", "absolute"}:
                    return literal_path(node.func.value)
            if isinstance(node, ast.Attribute) and node.attr == "parent":
                value = literal_path(node.value)
                return value.parent if value is not None else None
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents" and isinstance(node.slice, ast.Constant) and type(node.slice.value) is int:
                value = literal_path(node.value.value)
                if value is not None and 0 <= node.slice.value < len(value.parents):
                    return value.parents[node.slice.value]
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
                value = literal_path(node.left)
                return value / node.right.value if value is not None else None
            return None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value = literal_path(node.value)
                for target in node.targets:
                    if isinstance(target, ast.Name) and value is not None:
                        environment[target.id] = value
            if isinstance(node, ast.BinOp):
                value = literal_path(node)
                if value is not None and str(value) in repository_names and str(value) not in included:
                    if not any(part.startswith(".") or part in DENIED_DIRS | RUNTIME_RESOURCE_DIRS for part in value.parts):
                        raise BuildError("missing repository resource used by " + name + ": " + str(value))
                if value is not None and str(value) in included:
                    verified_resources.add(str(value))
    return {"pythonFilesParsed": python_files, "localImportTargetsVerified": imports,
            "literalResourcesVerified": sorted(verified_resources),
            "scope": "Static repository imports and literal paths only; third-party dependencies and dynamic runtime paths require target-host validation."}


def collect(root, role, explicit_new):
    root = Path(root)
    # Preserve the supplied path rather than resolving away symlinks.
    if not root.is_absolute() or ".." in root.parts or any(item.is_symlink() for item in (root, *root.parents)):
        raise BuildError("repository root must be an absolute non-symlink directory")
    if git(root, "rev-parse", "--show-toplevel").decode().strip() != str(root):
        raise BuildError("repository input must name its Git top-level")
    tracked = paths(root, "ls-files", "-z")
    untracked = paths(root, "ls-files", "--others", "--exclude-standard", "-z")
    explicit = set(explicit_new)
    for name in explicit:
        safe_relative(name)
        if name not in untracked or not permitted(name):
            raise BuildError("explicit new source is not an eligible untracked file: " + name)
    approved_new = {name for name in untracked if permitted(name) and (name in explicit or any(
        name.startswith(rule) if rule.endswith("/") else name == rule for rule in NEW_SOURCE[role]))}
    selected = {name for name in tracked if permitted(name)} | approved_new
    if len(selected) > MAX_FILES:
        raise BuildError("too many selected source files")
    required = {"backend/server.py", "backend/runtime_paths.py", "backend/requirements.txt"}
    if role == "zhijun":
        required |= {CATALOG, "backend/zhijun_worker/app.py", "backend/zhijun_worker/__main__.py"}
    else:
        required.add("governance/policy-manifest.json")
    if not required.issubset(selected):
        raise BuildError("required source missing from selected " + role + " files: " + ",".join(sorted(required - selected)))
    before = state(root, selected & tracked)
    files, total = {}, 0
    for relative in sorted(selected):
        source = root / relative
        if not source.exists() and not source.is_symlink():
            if relative in required:
                raise BuildError("required source deleted: " + relative)
            continue
        data = safe_read(source, MAX_SOURCE)
        total += len(data)
        if total > MAX_TOTAL:
            raise BuildError("selected source exceeds size limit")
        secret_check(data, role + "/" + relative)
        if relative.endswith(".py"):
            import ast
            try:
                ast.parse(data, filename=relative)
            except SyntaxError:
                raise BuildError("invalid Python source: " + relative) from None
        files[role + "/" + relative] = (data, 0o644, "tracked" if relative in tracked else "explicit-candidate")
    after = state(root, selected & tracked)
    if before != after:
        raise BuildError("repository changed during source capture: " + role)
    for name, (data, _, _) in files.items():
        if digest(safe_read(root / name.removeprefix(role + "/"), MAX_SOURCE)) != digest(data):
            raise BuildError("source changed during capture: " + name)
    audit = audit_local_dependencies(files, role, tracked | approved_new)
    return files, {**after, "sourceAudit": audit, "includedNewSources": sorted(approved_new),
                   "excludedUntrackedSourceCount": sum(permitted(name) and name not in approved_new for name in untracked)}


def agent_inputs(binary, manifest):
    data = safe_read(Path(binary), MAX_BINARY)
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] not in (1, 2):
        raise BuildError("Agent must be a 64-bit Linux ELF binary")
    order = "<" if data[5] == 1 else ">"
    if data[6] != 1 or struct.unpack(order + "H", data[16:18])[0] not in {2, 3} or struct.unpack(order + "H", data[52:54])[0] != 64:
        raise BuildError("invalid Agent ELF executable header")
    machine = struct.unpack(order + "H", data[18:20])[0]
    if machine not in {62, 183, 243}:
        raise BuildError("unsupported Agent ELF machine")
    manifest = Path(manifest)
    raw = safe_read(manifest, 1024 * 1024)
    manifest_name = "agent/manifest.yaml" if manifest.suffix.lower() in {".yaml", ".yml"} else "agent/manifest.json"
    secret_check(raw, manifest_name)
    try:
        if manifest_name.endswith(".yaml"):
            try:
                import yaml
            except ImportError:
                raise BuildError("YAML manifest requires PyYAML; use the existing backend virtual environment") from None
            try:
                parsed = yaml.safe_load(raw)
            except yaml.YAMLError:
                raise BuildError("invalid Agent YAML manifest") from None
        else:
            parsed = json.loads(raw)
    except BuildError:
        raise
    except (ValueError, UnicodeError):
        raise BuildError("Agent manifest must be valid UTF-8 JSON or safe YAML") from None
    if not isinstance(parsed, dict):
        raise BuildError("Agent manifest must be an object")
    def check_fields(value, seen):
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                raise BuildError("recursive Agent manifest aliases are forbidden")
            seen = {*seen, id(value)}
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub("[^a-z]", "", str(key).lower())
                if normalized in {"privatekey", "privatekeypem", "password", "secret", "clientsecret", "apikey", "accesstoken", "refreshtoken"} and isinstance(child, str) and child:
                    raise BuildError("credential-valued Agent manifest field rejected")
                check_fields(child, seen)
        elif isinstance(value, list):
            for child in value:
                check_fields(child, seen)
    check_fields(parsed, set())
    repository = None
    try:
        agent_root = Path(git(manifest.parent, "rev-parse", "--show-toplevel").decode().strip())
    except BuildError:
        agent_root = None
    if agent_root is not None:
        names = paths(agent_root, "ls-files", "-z")
        selected = {name for name in names if not any(part.startswith(".") or part in DENIED_DIRS for part in PurePosixPath(name).parts)
                    and (PurePosixPath(name).suffix in {".go", ".rs", ".proto"}
                         or PurePosixPath(name).name in {"go.mod", "go.sum", "Cargo.toml", "Cargo.lock"})}
        selected.add(str(manifest.relative_to(agent_root)))
        repository = state(agent_root, selected)
    return {"agent/nexusagent": (data, 0o755, "explicit-binary"),
            manifest_name: (raw, 0o600, "explicit-manifest")}, {"elfClass": 64, "elfMachine": machine,
                "manifestPath": manifest_name, "repository": repository,
                "binaryProvenance": "Explicit prebuilt ELF; its content hash is recorded, but no source-build attestation was supplied."}


def verify_archive(path, expected):
    seen, total = set(), 0
    directories = {str(parent) for name in expected for parent in PurePosixPath(name).parents if str(parent) != "."}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            safe_relative(member.name)
            if member.name in seen or member.uid != 0 or member.gid != 0 or member.mtime != 0:
                raise BuildError("invalid or duplicate archive member")
            seen.add(member.name)
            if member.isdir():
                if member.mode != 0o700 or member.name not in directories:
                    raise BuildError("unsafe archive directory mode")
                continue
            if not member.isfile() or member.name not in expected:
                raise BuildError("unlisted or linked archive member")
            record = expected[member.name]
            if member.size != record["size"] or member.mode != int(record["mode"], 8):
                raise BuildError("archive metadata mismatch")
            total += member.size
            if total > MAX_TOTAL or len(seen) > MAX_FILES * 2:
                raise BuildError("archive exceeds package limits")
            if digest(archive.extractfile(member).read()) != record["sha256"]:
                raise BuildError("archive content hash mismatch")
    if set(expected) - seen:
        raise BuildError("archive missing source files")


def build(args):
    files, repositories = {}, {}
    for role, root, additions in (("zhijun", args.zhijun_root, args.include_zhijun_new), ("de", args.data_engine_root, args.include_de_new)):
        selected, repositories[role] = collect(root, role, additions)
        files.update(selected)
    agent_files, agent_metadata = agent_inputs(args.agent_binary, args.agent_manifest)
    files.update(agent_files)
    if len(files) > MAX_FILES or sum(len(value[0]) for value in files.values()) > MAX_TOTAL:
        raise BuildError("release exceeds package limits")
    inventory = {name: {"sha256": digest(data), "size": len(data), "mode": f"{mode:04o}", "origin": origin}
                 for name, (data, mode, origin) in sorted(files.items())}
    source_receipt = {"formatVersion": 1, "deployed": False,
                      "candidate": any(repo["dirty"] for repo in repositories.values()) or bool((agent_metadata["repository"] or {}).get("dirty")),
                      "repositories": repositories, "agent": agent_metadata, "files": inventory,
                      "policy": {"sourceOnly": True, "symlinks": "rejected", "runtimeRoots": "excluded",
                                 "allowedNewSourcePrefixes": NEW_SOURCE}}
    source_bytes = encoded(source_receipt)
    files["SOURCES.json"] = (source_bytes, 0o600, "generated-receipt")
    expected = {**inventory, "SOURCES.json": {"sha256": digest(source_bytes), "size": len(source_bytes), "mode": "0600"}}
    output = Path(args.output)
    if not output.is_absolute() or ".." in output.parts or any(p.is_symlink() for p in (output, *output.parents)):
        raise BuildError("output must be an absolute non-symlink directory")
    if output.exists():
        raise BuildError("output already exists; use a fresh destination")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".full-product-build-", dir=output.parent) as temporary:
        stage = Path(temporary)
        stage.chmod(0o700)
        archive_path = stage / "full-product-release.tar.gz"
        directories = sorted({str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != "."})
        with archive_path.open("xb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for name in directories:
                        info = tarfile.TarInfo(name)
                        info.type, info.mode = tarfile.DIRTYPE, 0o700
                        archive.addfile(info)
                    for name, (data, mode, _) in sorted(files.items()):
                        info = tarfile.TarInfo(name)
                        info.mode, info.size = mode, len(data)
                        archive.addfile(info, io.BytesIO(data))
        archive_path.chmod(0o600)
        verify_archive(archive_path, expected)
        archive_digest = digest(archive_path.read_bytes())
        receipt = {**source_receipt, "archive": {"name": archive_path.name, "sha256": archive_digest,
                   "size": archive_path.stat().st_size}, "embeddedReceiptSha256": digest(source_bytes)}
        receipt_path = stage / "receipt.json"
        receipt_path.write_bytes(encoded(receipt))
        receipt_path.chmod(0o600)
        sums = stage / "SHA256SUMS"
        sums.write_text(archive_digest + "  " + archive_path.name + "\n" + digest(receipt_path.read_bytes()) + "  receipt.json\n")
        sums.chmod(0o600)
        # Rename publishes an entirely verified directory; no partial artifact is exposed on failure.
        os.rename(stage, output)
    return receipt


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    for name in ("zhijun-root", "data-engine-root", "agent-binary", "agent-manifest"):
        result.add_argument("--" + name, type=Path, required=True)
    result.add_argument("--output", type=Path, required=True, help="Fresh absolute output directory; must not already exist")
    result.add_argument("--include-zhijun-new", action="append", default=[], metavar="RELATIVE_FILE")
    result.add_argument("--include-de-new", action="append", default=[], metavar="RELATIVE_FILE")
    return result


def main():
    args = parser().parse_args()
    try:
        receipt = build(args)
    except (BuildError, OSError) as exc:
        raise SystemExit("release build rejected: " + str(exc)) from None
    print(json.dumps({"output": str(args.output), "archiveSha256": receipt["archive"]["sha256"],
                      "fileCount": len(receipt["files"]), "candidate": receipt["candidate"]}))


if __name__ == "__main__":
    main()
