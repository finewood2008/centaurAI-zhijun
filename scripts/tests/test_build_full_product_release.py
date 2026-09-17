"""Packaging fixtures only: temporary Git repositories and a synthetic ELF header, never runtime data."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tarfile
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "build-full-product-release.py"
spec = importlib.util.spec_from_file_location("full_product_release", SOURCE)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="release-package-fixture-")
        self.root = Path(self.temporary.name).resolve()
        self.zh, self.de = self.root / "zhijun", self.root / "de"
        for path, role in ((self.zh, "zhijun"), (self.de, "de")):
            path.mkdir()
            self.git(path, "init", "-q")
            self.git(path, "config", "user.email", "fixture@example.invalid")
            self.git(path, "config", "user.name", "Packaging Fixture")
            for name, content in {"backend/server.py": "server = True\n", "backend/runtime_paths.py": "isolated = True\n",
                                  "backend/requirements.txt": "fastapi==0.100.0\n",
                                  "backend/tests/test_dummy.py": "secret = 'never-package-tests'\n",
                                  "backend/.env": "REAL_CONFIG_CANARY=never\n",
                                  "backend/data/user.json": '{"user":"never-package"}',
                                  "backend/consumer_api/signing.py": "KEY = '-----BEGIN PRIVATE KEY-----'\n"}.items():
                self.write(path / name, content)
            if role == "de":
                self.write(path / "governance/policy-manifest.json", '{"version":1}')
            self.git(path, "add", ".")
            self.git(path, "commit", "-qm", "fixture")
        for name in ("backend/zhijun_worker/app.py", "backend/zhijun_worker/__main__.py"):
            self.write(self.zh / name, "worker = True\n")
        self.write(self.zh / builder.CATALOG, '{"operations":[]}')
        self.write(self.de / "backend/mindos/zhijun_gateway/router.py", "gateway = True\n")
        self.write(self.de / "backend/unapproved.py", "unapproved = True\n")
        self.binary = self.root / "agent"
        header = bytearray(64)
        header[:7] = b"\x7fELF\x02\x01\x01"
        header[16:20] = struct.pack("<HH", 2, 62)
        header[52:54] = struct.pack("<H", 64)
        self.binary.write_bytes(header)
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text('{"service":"fixture-agent","methods":["GET"]}')

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def args(self, destination="output"):
        return argparse.Namespace(zhijun_root=self.zh, data_engine_root=self.de, agent_binary=self.binary,
                                  agent_manifest=self.manifest, output=self.root / destination,
                                  include_zhijun_new=[], include_de_new=[])

    def test_archive_inventory_permissions_and_reproducibility(self):
        first = builder.build(self.args("one"))
        second = builder.build(self.args("two"))
        self.assertEqual(first, second)
        self.assertTrue(first["candidate"])
        self.assertFalse(first["deployed"])
        self.assertIn("zhijun/backend/zhijun_worker/app.py", first["files"])
        self.assertIn("de/governance/policy-manifest.json", first["files"])
        self.assertNotIn("de/backend/unapproved.py", first["files"])
        self.assertTrue(first["repositories"]["de"]["excludedUntrackedSourceCount"])
        archive = self.root / "one/full-product-release.tar.gz"
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), first["archive"]["sha256"])
        with tarfile.open(archive) as tar:
            for member in tar:
                self.assertNotIn("tests", Path(member.name).parts)
                self.assertNotIn("data", Path(member.name).parts)
                self.assertNotIn(".env", Path(member.name).parts)
                self.assertNotIn("consumer_api", Path(member.name).parts)
                if member.isdir():
                    self.assertEqual(member.mode, 0o700)
                else:
                    self.assertTrue(member.isfile())
                    raw = tar.extractfile(member).read()
                    self.assertNotIn(b"REAL_CONFIG_CANARY", raw)
                    self.assertNotIn(b"BEGIN PRIVATE KEY", raw)
            self.assertEqual(tar.getmember("agent/nexusagent").mode, 0o755)
            self.assertEqual(tar.getmember("agent/manifest.json").mode, 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "one").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)

    def test_explicit_new_file_and_dirty_diff_are_recorded(self):
        args = self.args()
        args.include_de_new = ["backend/unapproved.py"]
        self.write(self.de / "backend/server.py", "server = 'changed'\n")
        receipt = builder.build(args)
        self.assertEqual(receipt["files"]["de/backend/unapproved.py"]["origin"], "explicit-candidate")
        self.assertNotEqual(receipt["repositories"]["de"]["packagedTrackedDiffSha256"], hashlib.sha256(b"").hexdigest())

    def test_private_key_and_credential_in_selected_source_are_rejected(self):
        for value in ("key = '-----BEGIN PRIVATE KEY-----'\n", 'api_key = "abcdefghijklmnopqrstuvwxyz012345"\n'):
            self.write(self.zh / "backend/zhijun_worker/unsafe.py", value)
            with self.assertRaises(builder.BuildError):
                builder.build(self.args())
        self.assertFalse(self.args().output.exists())

    def test_source_symlink_and_symlink_directory_are_rejected(self):
        target = self.zh / "backend/server.py"
        target.unlink()
        target.symlink_to(self.de / "backend/server.py")
        with self.assertRaises((builder.BuildError, OSError)):
            builder.build(self.args())
        target.unlink()
        self.write(target, "server=True\n")
        linked = self.zh / "backend/linked"
        linked.symlink_to(self.de / "backend", target_is_directory=True)
        with self.assertRaises(OSError):
            builder.safe_read(linked / "server.py", builder.MAX_SOURCE)

    def test_large_file_relative_input_and_existing_output_rejected(self):
        large = self.zh / "backend/zhijun_worker/large.py"
        with large.open("wb") as file:
            file.truncate(builder.MAX_SOURCE + 1)
        with self.assertRaises(builder.BuildError):
            builder.build(self.args())
        large.unlink()
        with self.assertRaises(builder.BuildError):
            builder.safe_read(Path("relative.py"), 100)
        self.args().output.mkdir()
        with self.assertRaises(builder.BuildError):
            builder.build(self.args())

    def test_unsafe_explicit_sources_cannot_override_exclusions(self):
        args = self.args()
        for name in ("../outside.py", "backend/.env", "backend/data/user.json"):
            args.include_de_new = [name]
            with self.assertRaises(builder.BuildError):
                builder.build(args)

    def test_yaml_manifest_and_credential_fields(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("Use backend Python for YAML manifest coverage")
        manifest = self.root / "manifest.yaml"
        manifest.write_text("schemaVersion: 1\napplications:\n  - applicationId: fixture\n    protocol: http\n")
        args = self.args()
        args.agent_manifest = manifest
        receipt = builder.build(args)
        self.assertEqual(receipt["agent"]["manifestPath"], "agent/manifest.yaml")
        manifest.write_text("password: short\n")
        with self.assertRaises(builder.BuildError):
            builder.agent_inputs(self.binary, manifest)

    def test_archive_verification_rejects_traversal_and_symlink(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            archive = self.root / "malformed.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                member = tarfile.TarInfo(name)
                member.type, member.mode, member.linkname = kind, 0o644, "/tmp/escape"
                output.addfile(member)
            with self.assertRaises(builder.BuildError):
                builder.verify_archive(archive, {})

    def test_runtime_named_python_packages_are_included_without_configuration(self):
        self.write(self.de / "backend/models/__init__.py", "")
        self.write(self.de / "backend/models/runtime.py", "ready = True\n")
        self.write(self.de / "backend/models/config.json", '{"runtime":"not-for-release"}')
        self.write(self.de / "backend/server.py", "from models import runtime\n")
        self.git(self.de, "add", "backend/models")
        receipt = builder.build(self.args())
        self.assertIn("de/backend/models/__init__.py", receipt["files"])
        self.assertIn("de/backend/models/runtime.py", receipt["files"])
        self.assertNotIn("de/backend/models/config.json", receipt["files"])
        self.assertGreater(receipt["repositories"]["de"]["sourceAudit"]["localImportTargetsVerified"], 0)

    def test_excluded_local_import_and_missing_static_resource_fail_closed(self):
        self.write(self.de / "backend/server.py", "from consumer_api import signing\n")
        with self.assertRaisesRegex(builder.BuildError, "excluded repository import"):
            builder.build(self.args())
        self.write(self.de / "backend/server.py", "from pathlib import Path\nRESOURCE = Path(__file__).parent / 'required.bin'\n")
        self.write(self.de / "backend/required.bin", "required source resource")
        self.git(self.de, "add", "backend/required.bin")
        with self.assertRaisesRegex(builder.BuildError, "missing repository resource"):
            builder.build(self.args())


if __name__ == "__main__":
    unittest.main()
