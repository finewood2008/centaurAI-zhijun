"""Already committed extraction must survive a denied downstream registration."""
import unittest
from unittest.mock import patch

from tests.test_memory_charter_extraction import MemoryCharterExtractionTests
from mindos.zhijun import jobs
from zhijun_worker.background import BackgroundEnqueueError
from zhijun_worker.capabilities import CapabilityError


class MemoryFollowupFailureTests(MemoryCharterExtractionTests):
    def test_denied_projection_retains_created_candidate_and_failed_child(self):
        self.setup_online()
        message = self.add_user()
        original = self.onto.enqueue_job

        def enqueue(kind, owner_id, **kwargs):
            if kind != "project":
                return original(kind, owner_id, **kwargs)
            with patch("zhijun_worker.background.register", side_effect=CapabilityError("WORKSPACE_EXECUTION_REQUIRED", 403)):
                return original(kind, owner_id, **kwargs)

        with patch.object(self.onto, "enqueue_job", side_effect=enqueue):
            result = self.run_extract(message)
        self.assert_candidate(result)
        failures = result["followupFailures"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["kind"], "project")
        self.assertEqual(failures[0]["code"], "WORKSPACE_EXECUTION_REQUIRED")
        child = self.onto.get_job(failures[0]["jobId"])
        self.assertEqual(child["state"], "failed")
        self.assertEqual(child["attempts"], 0)

    def test_unclassified_projection_error_is_not_hidden(self):
        self.setup_online()
        message = self.add_user()
        with patch.object(jobs, "enqueue_projection", side_effect=RuntimeError("synthetic")):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                self.run_extract(message)


if __name__ == "__main__":
    unittest.main()
