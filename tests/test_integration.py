"""
Integration Test Suite for PaymentRouter.

Unlike test_unit.py, these tests use a REAL SQLite database on disk via
SQLiteRepository. Only the third-party payment gateways are mocked, since
those are genuinely external services we cannot call in a test run.
"""
import os
import sqlite3
import tempfile
import threading
from unittest.mock import MagicMock

import pytest

from payment_router import PaymentRouter
from db import SQLiteRepository, BrokenSQLiteRepository

VALID_AMOUNT = 100.0
VALID_RECIPIENT = "+15551234567"


@pytest.fixture(scope="function")
def sqlite_repo():
    """Provisions a temporary on-disk SQLite database per test function,
    and cleanly drops the file afterward regardless of test outcome."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = SQLiteRepository(path)
    yield repo
    repo.close()
    if os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------------------------
# Real Database Integration
# ---------------------------------------------------------------------------

class TestRealDatabaseIntegration:
    def test_successful_transaction_is_persisted_to_real_db(self, monkeypatch, sqlite_repo):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        primary_gw = MagicMock()
        primary_gw.process_payment.return_value = True
        backup_gw = MagicMock()
        router = PaymentRouter(sqlite_repo, primary_gw, backup_gw)

        result = router.execute_transaction("tx-int-001", VALID_AMOUNT, VALID_RECIPIENT)

        assert result == "COMPLETED_PRIMARY"
        stored = sqlite_repo.get_transaction("tx-int-001")
        assert stored is not None
        assert stored["status"] == "SUCCESS"
        assert stored["gateway"] == "PRIMARY"

    def test_idempotency_enforced_against_real_db_on_second_call(self, monkeypatch, sqlite_repo):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        primary_gw = MagicMock()
        primary_gw.process_payment.return_value = True
        backup_gw = MagicMock()
        router = PaymentRouter(sqlite_repo, primary_gw, backup_gw)

        first = router.execute_transaction("tx-int-002", VALID_AMOUNT, VALID_RECIPIENT)
        second = router.execute_transaction("tx-int-002", VALID_AMOUNT, VALID_RECIPIENT)

        assert first == "COMPLETED_PRIMARY"
        assert second == "ALREADY_PROCESSED"
        assert primary_gw.process_payment.call_count == 1


# ---------------------------------------------------------------------------
# Concurrency & Race Conditions
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_simultaneous_calls_with_same_tx_id_only_produce_one_success_row(self, monkeypatch, sqlite_repo):
        """Two threads race to process the same tx_id at once. The application-level
        idempotency check can race, but the tx_id PRIMARY KEY constraint on the real
        database guarantees only one row ever ends up recorded for that tx_id."""
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        primary_gw = MagicMock()
        primary_gw.process_payment.return_value = True
        backup_gw = MagicMock()
        router = PaymentRouter(sqlite_repo, primary_gw, backup_gw)

        tx_id = "tx-race-001"
        errors = []

        def worker():
            try:
                router.execute_transaction(tx_id, VALID_AMOUNT, VALID_RECIPIENT)
            except sqlite3.IntegrityError:
                # Expected under a genuine race: the DB constraint rejected
                # a second INSERT for the same primary key.
                pass
            except Exception as exc:  # pragma: no cover - safety net for unexpected errors
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        cursor = sqlite_repo.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE tx_id = ?", (tx_id,)
        )
        count = cursor.fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# The 'Mock Lie' — Written Proof & Code Challenge
# ---------------------------------------------------------------------------

class TestMockLie:
    """
    WRITTEN PROOF:

    In test_unit.py, `repo` is a MagicMock(). When PaymentRouter calls
    `self.repo.record_transaction(...)`, MagicMock simply records that the
    call happened and returns another MagicMock — it never touches a real
    database, never parses SQL, and therefore has no way to know that the
    SQL is wrong. Every unit test that only checks "was record_transaction
    called with these arguments?" will pass at 100%, even if the real SQL
    behind that method is completely broken.

    BrokenSQLiteRepository below is identical to SQLiteRepository except its
    record_transaction() targets a table called 'tx_history', which does not
    exist in our schema (only 'transactions' does). This is exactly the kind
    of bug a mocked unit test suite is structurally blind to.

    An integration test, by contrast, runs the real SQL against a real
    SQLite connection. The moment record_transaction() executes, SQLite
    raises `sqlite3.OperationalError: no such table: tx_history`, and the
    test correctly fails — proving why both test layers are necessary:
    unit tests verify LOGIC, integration tests verify the SQL actually
    matches the real schema.
    """

    def test_broken_table_name_is_caught_by_real_sqlite(self, monkeypatch, sqlite_repo):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        broken_repo = BrokenSQLiteRepository(sqlite_repo.db_path)
        primary_gw = MagicMock()
        primary_gw.process_payment.return_value = True
        backup_gw = MagicMock()
        router = PaymentRouter(broken_repo, primary_gw, backup_gw)

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            router.execute_transaction("tx-mock-lie-001", VALID_AMOUNT, VALID_RECIPIENT)

        broken_repo.close()

    def test_same_scenario_would_falsely_pass_if_repo_were_mocked(self, monkeypatch):
        """Demonstrates the lie directly: with a MagicMock repo, the exact same
        broken-table scenario cannot even be expressed — the mock has no schema
        to violate, so it always 'succeeds'."""
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        mocked_repo = MagicMock()
        mocked_repo.get_transaction.return_value = None
        primary_gw = MagicMock()
        primary_gw.process_payment.return_value = True
        backup_gw = MagicMock()
        router = PaymentRouter(mocked_repo, primary_gw, backup_gw)

        # No exception is raised here, despite the "real" equivalent being broken.
        result = router.execute_transaction("tx-mock-lie-002", VALID_AMOUNT, VALID_RECIPIENT)
        assert result == "COMPLETED_PRIMARY"