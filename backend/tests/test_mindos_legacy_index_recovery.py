from unittest.mock import Mock, patch

import watcher


def test_infrastructure_recovery_never_replays_mindos_uploads():
    store = Mock()
    store.requeue_recoverable_failures.return_value = [{
        "source_path": "C:/data/.mindos_uploads/material.md",
        "force": False,
        "strategy_id": None,
        "submit_wiki": False,
    }]
    with patch.object(watcher.JobStore, "instance", return_value=store), \
         patch.object(watcher, "_submit_index_worker") as submit:
        assert watcher.recover_infrastructure_failures() == 0
    submit.assert_not_called()


def test_transient_recovery_never_replays_mindos_uploads():
    store = Mock()
    store.requeue_recoverable_failures.return_value = [{
        "source_path": "C:/data/.mindos_uploads/material.md",
        "force": False,
        "strategy_id": None,
        "submit_wiki": False,
    }]
    with patch.object(watcher.JobStore, "instance", return_value=store), \
         patch.object(watcher, "_submit_index_worker") as submit:
        assert watcher.replay_due_transient_failures() == 0
    submit.assert_not_called()
