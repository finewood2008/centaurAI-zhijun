from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path, PurePosixPath


BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent


def _module_contract(env: dict[str, str]) -> dict[str, str]:
    code = textwrap.dedent(
        """
        import json
        import annotations
        import config
        import gbrain_store
        import rag_strategy
        import runtime_paths

        values = {
            name: str(getattr(runtime_paths, name))
            for name in (
                "PROJECT_ROOT", "DATA_ROOT", "CONFIG_ROOT", "CHROMA_DATA_DIR",
                "WATCH_FOLDER", "VIDEO_FRAMES_DIR", "VIDEO_WORK_DIR", "MEMORY_DIR",
                "WIKI_DIR", "DB_ROOT", "WIKI_DB_PATH", "FILE_CENTER_DB_PATH",
                "GOVERNANCE_DB_PATH", "JOB_STORE_DB_PATH", "DERIVED_STORE_DB_PATH",
                "LEGACY_ANNOTATIONS_PATH", "LEGACY_GROUPS_PATH", "GBRAIN_HOME",
                "LAN_CONFIG_PATH", "MOBILE_CONFIG_PATH", "CONTEXT_PACKS_CONFIG_PATH",
                "TRASH_DIR", "CONTEXT_SNAPSHOT_JSON_PATH", "RAG_CONFIG_PATH",
                "TOKENMANAGER_CONFIG_DIR", "MCP_DATA_DIR", "MCP_CONFIG_DIR",
            )
        }
        values.update({
            "config.CHROMA_DATA_DIR": config.CHROMA_DATA_DIR,
            "config.WATCH_FOLDER": config.WATCH_FOLDER,
            "config.VIDEO_FRAMES_DIR": config.VIDEO_FRAMES_DIR,
            "config.VIDEO_WORK_DIR": config.VIDEO_WORK_DIR,
            "config.MEMORY_DIR": config.MEMORY_DIR,
            "config.WIKI_DIR": config.WIKI_DIR,
            "config.WIKI_DB_PATH": config.WIKI_DB_PATH,
            "config.MODELS_CACHE": config.MODELS_CACHE,
            "annotations._DB_PATH": str(annotations._DB_PATH),
            "gbrain_store.GBRAIN_HOME": str(gbrain_store.GBRAIN_HOME),
            "gbrain_store.WIKI_ROOT": str(gbrain_store.WIKI_ROOT),
            "rag_strategy.RAG_CONFIG_PATH": str(rag_strategy.RAG_CONFIG_PATH),
        })
        print(json.dumps(values, sort_keys=True))
        """
    )
    process_env = os.environ.copy()
    for name in (
        "CENTAURAI_DATABASE_DATA_ROOT",
        "CENTAUR_METADATA_DB",
        "CENTAUR_GBRAIN_HOME",
        "CENTAUR_MCP_DATA_DIR",
        "CENTAUR_MCP_CONFIG_DIR",
    ):
        process_env.pop(name, None)
    process_env.update(env)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env=process_env,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


class DataRootContractTests(unittest.TestCase):
    def test_mutable_paths_follow_database_data_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary).resolve()
            values = _module_contract({"CENTAURAI_DATABASE_DATA_ROOT": str(data_root)})

        mutable_names = {
            name for name in values
            if name not in {"PROJECT_ROOT", "config.MODELS_CACHE"}
        }
        for name in mutable_names:
            path = Path(values[name]).resolve()
            self.assertTrue(path == data_root or data_root in path.parents, f"{name}: {path}")
        self.assertEqual(Path(values["DATA_ROOT"]), data_root)
        model_cache = Path(values["config.MODELS_CACHE"])
        self.assertEqual(model_cache, BACKEND_DIR / "models_cache")
        self.assertNotIn(data_root, model_cache.parents)

    def test_default_and_specific_overrides_remain_supported(self):
        defaults = _module_contract({})
        self.assertEqual(Path(defaults["DATA_ROOT"]), PROJECT_ROOT / "data")
        self.assertEqual(Path(defaults["DB_ROOT"]), PROJECT_ROOT / "data" / "db")
        self.assertEqual(Path(defaults["JOB_STORE_DB_PATH"]), PROJECT_ROOT / "data" / "db" / "job_store.db")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            metadata = root / "metadata.sqlite3"
            gbrain = root / "gbrain"
            mcp_data = root / "mcp-data"
            mcp_config = root / "mcp-config"
            values = _module_contract(
                {
                    "CENTAURAI_DATABASE_DATA_ROOT": str(data_root),
                    "CENTAUR_METADATA_DB": str(metadata),
                    "CENTAUR_GBRAIN_HOME": str(gbrain),
                    "CENTAUR_MCP_DATA_DIR": str(mcp_data),
                    "CENTAUR_MCP_CONFIG_DIR": str(mcp_config),
                }
            )
        self.assertEqual(Path(values["FILE_CENTER_DB_PATH"]), metadata.resolve())
        self.assertEqual(Path(values["GBRAIN_HOME"]), gbrain.resolve())
        self.assertEqual(Path(values["MCP_DATA_DIR"]), mcp_data.resolve())
        self.assertEqual(Path(values["MCP_CONFIG_DIR"]), mcp_config.resolve())

    def test_data_root_migration_previews_then_moves_without_overwrite(self):
        script = PROJECT_ROOT / "scripts" / "migrate_data_root.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy"
            target = root / "data"
            (legacy / "watch_folder").mkdir(parents=True)
            (legacy / "watch_folder" / "source.md").write_text("source")
            (legacy / ".mindos_upload_staging").mkdir()
            (legacy / ".mindos_upload_staging" / "pending.uploading").write_text("pending")
            (legacy / "wiki").mkdir()
            (legacy / "wiki" / "card.md").write_text("# card")
            (legacy / "wiki" / "wiki.sqlite3").write_text("wiki-db")
            (legacy / "job_store.db").write_text("jobs")

            base = [
                sys.executable, str(script), "--source-root", str(legacy),
                "--data-root", str(target),
            ]
            preview = subprocess.run(base, text=True, capture_output=True, check=True)
            self.assertIn("预览", preview.stdout)
            self.assertTrue((legacy / "job_store.db").exists())
            self.assertFalse(target.exists())

            executed = subprocess.run(base + ["--execute"], text=True, capture_output=True, check=True)
            self.assertIn("已移动", executed.stdout)
            self.assertFalse((legacy / "job_store.db").exists())
            self.assertEqual((target / "db" / "job_store.db").read_text(), "jobs")
            self.assertEqual((target / "db" / "wiki.sqlite3").read_text(), "wiki-db")
            self.assertEqual((target / "wiki" / "card.md").read_text(), "# card")
            self.assertEqual((target / "watch_folder" / "source.md").read_text(), "source")
            self.assertEqual(
                (target / ".mindos_upload_staging" / "pending.uploading").read_text(), "pending"
            )

            repeated = subprocess.run(base + ["--execute"], text=True, capture_output=True, check=True)
            self.assertIn("未发现需要迁移", repeated.stdout)

    def test_heavy_modules_use_runtime_path_contract(self):
        server = (BACKEND_DIR / "server.py").read_text(encoding="utf-8")
        self.assertIn("_LAN_CFG = LAN_CONFIG_PATH", server)
        self.assertIn("_MOBILE_CFG = MOBILE_CONFIG_PATH", server)
        self.assertIn("_CONTEXT_PACKS_CFG = CONTEXT_PACKS_CONFIG_PATH", server)
        self.assertIn("_TRASH_DIR = TRASH_DIR", server)
        self.assertNotIn('Path(PROJECT_ROOT) / ".trash"', server)
        health = server[server.index('@app.get("/api/health")'):server.index("# ========== 文件上传 ==========")]
        self.assertIn("reranker_loadable()", health)
        self.assertIn("whisper_loadable()", health)
        self.assertNotIn("reranker_available()", health)
        self.assertNotIn("whisper_available()", health)
        self.assertNotIn("clip_available()", health)

        tokenmanager = (BACKEND_DIR / "tokenmanager_sync.py").read_text(encoding="utf-8")
        self.assertIn("CONFIG_DIR = TOKENMANAGER_CONFIG_DIR", tokenmanager)
        self.assertNotIn('Path.home() / ".config" / "centaurai-memory"', tokenmanager)

        mcp = (BACKEND_DIR / "mcp_access.py").read_text(encoding="utf-8")
        self.assertIn('str(MCP_DATA_DIR)', mcp)
        self.assertIn('str(MCP_CONFIG_DIR)', mcp)
        self.assertNotIn('Path.home() / ".local"', mcp)

        sync_script = (PROJECT_ROOT / "scripts" / "sync_agent_memories.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("CENTAURAI_DATABASE_DATA_ROOT")', sync_script)
        self.assertNotIn('/home/user/local-vector-db', sync_script)


@unittest.skipUnless(os.name == "posix", "run.sh and runtime archive tests require POSIX")
class RuntimeEntryTests(unittest.TestCase):
    def test_run_entry_exports_data_contract_and_offline_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            python_bin = root / "backend" / ".venv" / "bin" / "python"
            python_bin.parent.mkdir(parents=True)
            shutil.copy2(PROJECT_ROOT / "run.sh", root / "run.sh")
            python_bin.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$PWD\" \"$1\" \"$CENTAURAI_DATABASE_DATA_ROOT\" "
                "\"$HF_HUB_OFFLINE\" \"$TRANSFORMERS_OFFLINE\" \"$PYTHONDONTWRITEBYTECODE\"\n"
            )
            python_bin.chmod(0o755)
            data_root = Path(temporary) / "state"
            result = subprocess.run(
                ["bash", str(root / "run.sh")],
                env={**os.environ, "CENTAURAI_DATABASE_DATA_ROOT": str(data_root)},
                check=True,
                text=True,
                capture_output=True,
            )
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], str(root / "backend"))
        self.assertEqual(lines[1], "server.py")
        self.assertEqual(lines[2], str(data_root.resolve()))
        self.assertEqual(lines[3:], ["1", "1", "1"])

    def test_run_entry_rejects_missing_virtualenv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(PROJECT_ROOT / "run.sh", root / "run.sh")
            result = subprocess.run(
                ["bash", str(root / "run.sh")],
                text=True,
                capture_output=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime is incomplete", result.stderr)


@unittest.skipUnless(os.name == "posix", "runtime archive tests require POSIX symlinks and ELF")
class RuntimePackagingTests(unittest.TestCase):
    def _fixture(self, root: Path) -> None:
        files = {
            "run.sh": "#!/usr/bin/env bash\nexit 0\n",
            "backend/server.py": "print('server')\n",
            "backend/runtime_paths.py": "DATA_ROOT = None\n",
            "backend/test_should_not_ship.py": "raise RuntimeError\n",
            "backend/models_cache/BAAI/bge-small-zh-v1.5/config.json": "{}\n",
            "backend/models_cache/BAAI/bge-small-zh-v1.5/model.safetensors": "model\n",
            "backend/.venv/lib/python3.12/site-packages/example-1.0.dist-info/METADATA": "Name: example\n",
            "frontend/package.json": '{"version":"1.2.3"}\n',
            "frontend/assets/app.js": "asset\n",
            "frontend/mobile/index.html": "mobile\n",
            "frontend/mindos-web/dist/index.html": "<html>MindOS</html>\n",
            "frontend/mindos-web/dist/assets/app.js": "mindos asset\n",
            "frontend/renderer/lan_import.html": "lan\n",
            "frontend/renderer/desktop.html": "desktop\n",
            "frontend/node_modules/example/index.js": "dependency\n",
            "scripts/sync_agent_memories.py": "print('sync')\n",
            "scripts/setup_remote_mcp.sh": "exit 0\n",
            "watch_folder/customer.txt": "customer data\n",
            "memory/USER.md": "private\n",
            ".lan_config.json": "{}\n",
            "file_center.db": "sqlite\n",
            ".git/config": "repository\n",
        }
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (root / "run.sh").chmod(0o755)

        python_bin = root / "backend" / ".venv" / "bin" / "python"
        python_bin.parent.mkdir(parents=True, exist_ok=True)
        ident = b"\x7fELF" + bytes((2, 1, 1)) + b"\0" * 9
        python_bin.write_bytes(ident + struct.pack("<HH", 2, 62) + b"fixture")
        python_bin.chmod(0o755)
        (python_bin.parent / "python3").symlink_to("python")

    def test_runtime_archive_is_single_root_clean_and_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            output = Path(temporary) / "output"
            self._fixture(root)
            command = [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "package_runtime.py"),
                "--source-root", str(root),
                "--output-dir", str(output),
                "--target", "linux-x86_64",
            ]
            subprocess.run(command, check=True, text=True, capture_output=True)
            archive = output / "centaurai-database-1.2.3-linux-x86_64-runtime.tar.gz"
            first_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            subprocess.run(command, check=True, text=True, capture_output=True)
            second_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(first_digest, second_digest)

            with tarfile.open(archive, "r:gz") as package:
                members = package.getmembers()
        top = "centaurai-database-1.2.3-linux-x86_64-runtime"
        names = {member.name for member in members}
        self.assertEqual({PurePosixPath(name).parts[0] for name in names}, {top})
        self.assertTrue(any(name == f"{top}/run.sh" for name in names))
        run_info = next(member for member in members if member.name == f"{top}/run.sh")
        self.assertTrue(run_info.mode & stat.S_IXUSR)
        self.assertFalse(any(member.issym() or member.islnk() for member in members))
        self.assertIn(f"{top}/backend/.venv/bin/python3", names)
        self.assertIn(f"{top}/backend/models_cache/BAAI/bge-small-zh-v1.5/model.safetensors", names)
        self.assertIn(f"{top}/frontend/mobile/index.html", names)
        self.assertIn(f"{top}/frontend/mindos-web/dist/index.html", names)
        self.assertIn(f"{top}/frontend/mindos-web/dist/assets/app.js", names)
        self.assertIn(f"{top}/VERSION", names)
        for forbidden in (
            "watch_folder/customer.txt",
            "memory/USER.md",
            ".lan_config.json",
            "file_center.db",
            ".git/config",
            "backend/test_should_not_ship.py",
            "frontend/node_modules/example/index.js",
            "frontend/renderer/desktop.html",
            "scripts/setup_remote_mcp.sh",
        ):
            self.assertNotIn(f"{top}/{forbidden}", names)

    def test_runtime_packaging_rejects_unprovisioned_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "frontend").mkdir()
            (root / "frontend" / "package.json").write_text('{"version":"1.0.0"}')
            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "package_runtime.py"),
                    "--source-root", str(root),
                    "--output-dir", str(root / "out"),
                    "--target", "linux-x86_64",
                ],
                text=True,
                capture_output=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source tree is incomplete", result.stderr)


if __name__ == "__main__":
    unittest.main()
