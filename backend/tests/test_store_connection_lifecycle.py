import sqlite3
import tempfile
import unittest
from pathlib import Path
from mindos.stores.conversation_store import ConversationStore
from mindos.stores.ontology_store import OntologyStore
from mindos.stores.growth_store import GrowthStore


class ConnectionLifecycleTests(unittest.TestCase):
    def test_store_transactions_close_commit_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in (ConversationStore, OntologyStore, GrowthStore):
                with self.subTest(store=kind.__name__):
                    store = kind(Path(directory) / (kind.__name__ + ".db"))
                    with store._connect() as db:
                        db.execute("CREATE TABLE qa_transaction (value TEXT)")
                        db.execute("INSERT INTO qa_transaction VALUES ('kept')")
                    with self.assertRaises(sqlite3.ProgrammingError):
                        db.execute("SELECT 1")
                    with self.assertRaisesRegex(ValueError, "rollback"):
                        with store._connect() as failed:
                            failed.execute("INSERT INTO qa_transaction VALUES ('discarded')")
                            raise ValueError("rollback")
                    with self.assertRaises(sqlite3.ProgrammingError):
                        failed.execute("SELECT 1")
                    with store._connect() as verify:
                        self.assertEqual([r[0] for r in verify.execute("SELECT value FROM qa_transaction")], ["kept"])

    def test_repeated_reads_do_not_wait_for_garbage_collection_to_close(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OntologyStore(Path(directory) / "ontology.db")
            connections = []
            for _ in range(200):
                with store._connect() as db:
                    db.execute("SELECT 1").fetchone()
                    connections.append(db)
            for db in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    db.execute("SELECT 1")
