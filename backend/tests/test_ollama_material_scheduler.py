import threading
import time
import unittest

from mindos.ollama_material_scheduler import (
    PRIORITY_MANUAL_REGENERATE,
    PRIORITY_VLM_IMAGE,
    OllamaMaterialScheduler,
)


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class OllamaMaterialSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = OllamaMaterialScheduler()

    def tearDown(self):
        self.scheduler.stop()
        wait_for(lambda: not any(worker.is_alive() for worker in self.scheduler._workers))

    def test_pending_request_is_replaced_by_latest_same_material_and_kind(self):
        started = threading.Event()
        release = threading.Event()
        completed = []

        self.scheduler.submit(
            PRIORITY_MANUAL_REGENERATE,
            lambda: (started.set(), release.wait(1.0)),
            material_id="other",
            kind="summary",
        )
        self.assertTrue(started.wait(1.0))
        self.scheduler.submit(PRIORITY_VLM_IMAGE, lambda: completed.append("old"), material_id="m1", kind="vlm")
        self.scheduler.submit(PRIORITY_MANUAL_REGENERATE, lambda: completed.append("new"), material_id="m1", kind="vlm")
        release.set()
        self.assertTrue(wait_for(lambda: completed == ["new"]))

    def test_higher_priority_runs_first_after_current_task(self):
        started = threading.Event()
        release = threading.Event()
        completed = []
        self.scheduler.submit(
            PRIORITY_MANUAL_REGENERATE,
            lambda: (started.set(), release.wait(1.0)),
            material_id="running",
            kind="summary",
        )
        self.assertTrue(started.wait(1.0))
        self.scheduler.submit(PRIORITY_VLM_IMAGE, lambda: completed.append("low"), material_id="low", kind="vlm")
        self.scheduler.submit(PRIORITY_MANUAL_REGENERATE, lambda: completed.append("high"), material_id="high", kind="summary")
        release.set()
        self.assertTrue(wait_for(lambda: completed == ["high", "low"]))

    def test_stop_does_not_allow_restart_until_running_worker_exits(self):
        started = threading.Event()
        release = threading.Event()
        self.scheduler.submit(
            PRIORITY_MANUAL_REGENERATE,
            lambda: (started.set(), release.wait(1.0)),
            material_id="m1",
            kind="summary",
        )
        self.assertTrue(started.wait(1.0))
        self.scheduler.stop()
        self.assertFalse(self.scheduler.start())
        release.set()
        self.assertTrue(wait_for(lambda: not any(worker.is_alive() for worker in self.scheduler._workers)))
        self.assertTrue(self.scheduler.start())


if __name__ == "__main__":
    unittest.main()
