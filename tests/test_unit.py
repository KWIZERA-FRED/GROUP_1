"""
Unit Test Suite for PaymentRouter.

All external dependencies (DatabaseRepository, PaymentGatewayClient) are
mocked here. These tests verify PaymentRouter's internal logic in isolation
and never touch a real database or network call.
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from payment_router import PaymentRouter

VALID_TX_ID = "tx-001"
VALID_AMOUNT = 100.0
VALID_RECIPIENT = "+15551234567"
INVALID_RECIPIENT = "0044-not-e164"


def make_router(api_key="PROD_TEST_SECRET_KEY_123"):
    repo = MagicMock()
    primary_gw = MagicMock()
    backup_gw = MagicMock()
    with patch.dict("os.environ", {"PAYMENT_GATEWAY_API_KEY": api_key} if api_key else {}, clear=False):
        if api_key is None:
            import os
            os.environ.pop("PAYMENT_GATEWAY_API_KEY", None)
        router = PaymentRouter(repo, primary_gw, backup_gw)
    return router, repo, primary_gw, backup_gw


# ---------------------------------------------------------------------------
# Security & Environment Mocking
# ---------------------------------------------------------------------------

class TestSecurityGuardrail:
    def test_missing_api_key_raises_permission_error(self, monkeypatch):
        monkeypatch.delenv("PAYMENT_GATEWAY_API_KEY", raising=False)
        repo, primary_gw, backup_gw = MagicMock(), MagicMock(), MagicMock()
        router = PaymentRouter(repo, primary_gw, backup_gw)

        with pytest.raises(PermissionError):
            router.execute_transaction(VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT)

    def test_debug_mode_key_raises_permission_error(self, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "DEBUG_MODE_KEY")
        repo, primary_gw, backup_gw = MagicMock(), MagicMock(), MagicMock()
        router = PaymentRouter(repo, primary_gw, backup_gw)

        with pytest.raises(PermissionError):
            router.execute_transaction(VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT)


# ---------------------------------------------------------------------------
# Input Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_zero_amount_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        router = PaymentRouter(MagicMock(), MagicMock(), MagicMock())
        with pytest.raises(ValueError):
            router.execute_transaction(VALID_TX_ID, 0, VALID_RECIPIENT)

    def test_negative_amount_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        router = PaymentRouter(MagicMock(), MagicMock(), MagicMock())
        with pytest.raises(ValueError):
            router.execute_transaction(VALID_TX_ID, -50, VALID_RECIPIENT)

    def test_invalid_phone_format_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        router = PaymentRouter(MagicMock(), MagicMock(), MagicMock())
        with pytest.raises(ValueError):
            router.execute_transaction(VALID_TX_ID, VALID_AMOUNT, INVALID_RECIPIENT)


# ---------------------------------------------------------------------------
# Idempotency & State Prevention
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_already_success_returns_already_processed_without_calling_gateways(self, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        repo = MagicMock()
        repo.get_transaction.return_value = {"status": "SUCCESS"}
        primary_gw, backup_gw = MagicMock(), MagicMock()
        router = PaymentRouter(repo, primary_gw, backup_gw)

        result = router.execute_transaction(VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT)

        assert result == "ALREADY_PROCESSED"
        primary_gw.process_payment.assert_not_called()
        backup_gw.process_payment.assert_not_called()
        repo.record_transaction.assert_not_called()

    def test_existing_non_success_record_still_proceeds_to_gateways(self, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        repo = MagicMock()
        repo.get_transaction.return_value = {"status": "FAILED"}
        primary_gw, backup_gw = MagicMock(), MagicMock()
        primary_gw.process_payment.return_value = True
        router = PaymentRouter(repo, primary_gw, backup_gw)

        result = router.execute_transaction(VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT)

        assert result == "COMPLETED_PRIMARY"
        primary_gw.process_payment.assert_called_once()

    def test_no_existing_record_proceeds_to_gateways(self, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        repo = MagicMock()
        repo.get_transaction.return_value = None
        primary_gw, backup_gw = MagicMock(), MagicMock()
        primary_gw.process_payment.return_value = True
        router = PaymentRouter(repo, primary_gw, backup_gw)

        result = router.execute_transaction(VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT)

        assert result == "COMPLETED_PRIMARY"


# ---------------------------------------------------------------------------
# Flaky Gateway Retry Assertions
# ---------------------------------------------------------------------------

class TestRetryLogic:
    def test_primary_succeeds_on_first_attempt_no_retry_needed(self, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        repo = MagicMock()
        repo.get_transaction.return_value = None
        primary_gw, backup_gw = MagicMock(), MagicMock()
        primary_gw.process_payment.return_value = True
        router = PaymentRouter(repo, primary_gw, backup_gw)

        result = router.execute_transaction(VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT)

        assert result == "COMPLETED_PRIMARY"
        assert primary_gw.process_payment.call_count == 1
        repo.record_transaction.assert_called_once_with(
            VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT, "SUCCESS", "PRIMARY"
        )

    @patch("payment_router.time.sleep")
    def test_primary_throws_on_first_attempt_then_succeeds_on_retry(self, mock_sleep, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        repo = MagicMock()
        repo.get_transaction.return_value = None
        primary_gw, backup_gw = MagicMock(), MagicMock()
        primary_gw.process_payment.side_effect = [TimeoutError("network timeout"), True]
        router = PaymentRouter(repo, primary_gw, backup_gw)

        result = router.execute_transaction(VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT)

        assert result == "COMPLETED_PRIMARY"
        assert primary_gw.process_payment.call_count == 2
        mock_sleep.assert_called_once_with(0.1)
        repo.record_transaction.assert_called_once_with(
            VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT, "SUCCESS", "PRIMARY"
        )

    @patch("payment_router.time.sleep")
    def test_primary_returns_false_twice_falls_back_to_backup(self, mock_sleep, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        repo = MagicMock()
        repo.get_transaction.return_value = None
        primary_gw, backup_gw = MagicMock(), MagicMock()
        primary_gw.process_payment.return_value = False
        backup_gw.process_payment.return_value = True
        router = PaymentRouter(repo, primary_gw, backup_gw)

        result = router.execute_transaction(VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT)

        assert result == "COMPLETED_BACKUP"
        assert primary_gw.process_payment.call_count == 2
        backup_gw.process_payment.assert_called_once()
        repo.record_transaction.assert_called_once_with(
            VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT, "SUCCESS", "BACKUP"
        )

    @patch("payment_router.time.sleep")
    def test_primary_exceptions_both_attempts_backup_succeeds(self, mock_sleep, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        repo = MagicMock()
        repo.get_transaction.return_value = None
        primary_gw, backup_gw = MagicMock(), MagicMock()
        primary_gw.process_payment.side_effect = ConnectionError("down")
        backup_gw.process_payment.return_value = True
        router = PaymentRouter(repo, primary_gw, backup_gw)

        result = router.execute_transaction(VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT)

        assert result == "COMPLETED_BACKUP"
        assert mock_sleep.call_count == 2


# ---------------------------------------------------------------------------
# Complete System Circuit Break
# ---------------------------------------------------------------------------

class TestTotalFailure:
    @patch("payment_router.time.sleep")
    def test_both_gateways_raise_http_500_records_failed_and_raises(self, mock_sleep, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        repo = MagicMock()
        repo.get_transaction.return_value = None
        primary_gw, backup_gw = MagicMock(), MagicMock()
        primary_gw.process_payment.side_effect = ConnectionError("HTTP 500")
        backup_gw.process_payment.side_effect = ConnectionError("HTTP 500")
        router = PaymentRouter(repo, primary_gw, backup_gw)

        with pytest.raises(RuntimeError):
            router.execute_transaction(VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT)

        repo.record_transaction.assert_called_once_with(
            VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT, "FAILED", "NONE"
        )

    def test_both_gateways_return_false_records_failed_and_raises(self, monkeypatch):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "PROD_TEST_SECRET_KEY_123")
        repo = MagicMock()
        repo.get_transaction.return_value = None
        primary_gw, backup_gw = MagicMock(), MagicMock()
        primary_gw.process_payment.return_value = False
        backup_gw.process_payment.return_value = False
        router = PaymentRouter(repo, primary_gw, backup_gw)

        with pytest.raises(RuntimeError):
            router.execute_transaction(VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT)

        repo.record_transaction.assert_called_once_with(
            VALID_TX_ID, VALID_AMOUNT, VALID_RECIPIENT, "FAILED", "NONE"
        )