"""The module-isolated launcher must never turn failing children into success."""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("zhijun_test_launcher", Path(__file__).resolve().parents[2] / "scripts/run_tests.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class IsolatedRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        (root / "tests").mkdir()
        for name in ("test_a.py", "test_b.py"):
            (root / "tests" / name).touch()
        self.root = root
        self.patcher = patch.object(runner, "BACKEND_ROOT", root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def execute(self, codes, reports=None, options=None):
        def child(command, **kwargs):
            self.assertEqual(kwargs["cwd"], self.root)
            self.assertEqual(command[1:3], ["-m", "pytest"])
            index = fake.call_count - 1
            report = reports[index] if reports else '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"/></testsuites>'
            if report is not None:
                Path(command[-1].split("=", 1)[1]).write_text(report)
            return subprocess.CompletedProcess(command, codes[index])
        output = io.StringIO()
        with patch.object(runner.subprocess, "run", side_effect=child) as fake, redirect_stdout(output):
            code = runner.isolated_modules(options or ["--", "-q"])
        return code, fake.call_count, output.getvalue()

    def test_group_node_selection_and_dedup_without_importing_tests(self):
        self.assertEqual(runner.module_groups(["tests/test_a.py::Test::test_one", "tests/test_a.py", "tests/test_a.py::Test::test_two"]), {"tests/test_a.py": ["tests/test_a.py"]})
        self.assertEqual(len(runner.module_groups([])), 2)
        with self.assertRaises(ValueError):
            runner.module_groups(["../outside.py"])
        with self.assertRaises(ValueError):
            runner.isolated_modules(["-k", "condition"])

    def test_failure_does_not_stop_remaining_modules_or_exit_success(self):
        code, calls, output = self.execute([1, 0])
        self.assertEqual((code, calls), (1, 2))
        self.assertIn("FAILED (1): tests/test_a.py", output)
        self.assertIn("失败 1", output)

    def test_empty_filter_is_visible_but_does_not_mask_failure(self):
        self.assertEqual(self.execute([5, 0])[0], 0)
        self.assertEqual(self.execute([5, 5])[0], 5)
        self.assertEqual(self.execute([5, 1])[0], 1)

    def test_missing_or_corrupt_report_cannot_claim_success(self):
        self.assertEqual(self.execute([0, 0], [None, "invalid xml"])[0], 3)

    def test_interruption_stops_later_modules_and_is_nonzero(self):
        code, calls, output = self.execute([130, 0])
        self.assertEqual((code, calls), (130, 1))
        self.assertIn("1/2 个模块", output)

    def test_collection_failure_is_reported_and_remaining_modules_run(self):
        code, calls, _ = self.execute([2, 0])
        self.assertEqual((code, calls), (2, 2))


if __name__ == "__main__":
    unittest.main()
