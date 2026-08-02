import re
import os
import time


class DatabaseRepository:
    """Interface for payment transaction records."""

    def get_transaction(self, tx_id: str) -> dict:
        raise NotImplementedError

    def record_transaction(self, tx_id: str, amount: float, recipient: str, status: str, gateway: str):
        raise NotImplementedError


class PaymentGatewayClient:
    """Interface for third-party payment processors."""

    def process_payment(self, tx_id: str, amount: float, recipient: str) -> bool:
        raise NotImplementedError


class PaymentRouter:
    """Core Business Engine under verification."""

    def __init__(self, repo: DatabaseRepository, primary_gw: PaymentGatewayClient, backup_gw: PaymentGatewayClient):
        self.repo = repo
        self.primary_gw = primary_gw
        self.backup_gw = backup_gw
        self.api_key = os.getenv("PAYMENT_GATEWAY_API_KEY")

    def execute_transaction(self, tx_id: str, amount: float, recipient: str) -> str:
        # Security Guardrail: Requires environment variable in execution environment
        if not self.api_key or self.api_key == "DEBUG_MODE_KEY":
            raise PermissionError("Unauthorized: Production API Key missing or set to insecure default.")

        # Validation Rule: Amount must be positive & phone format valid
        if amount <= 0:
            raise ValueError("Invalid transaction amount")
        if not re.match(r"^\+[1-9]\d{1,14}$", recipient):
            raise ValueError("Invalid E.164 phone number format")

        # Idempotency Guard
        existing = self.repo.get_transaction(tx_id)
        if existing and existing.get("status") == "SUCCESS":
            return "ALREADY_PROCESSED"

        # Execution Phase: Primary Gateway with Retry
        for attempt in range(2):
            try:
                if self.primary_gw.process_payment(tx_id, amount, recipient):
                    self.repo.record_transaction(tx_id, amount, recipient, "SUCCESS", "PRIMARY")
                    return "COMPLETED_PRIMARY"
            except Exception:
                time.sleep(0.1)  # Brief delay before retry

        # Execution Phase: Fallback Gateway
        try:
            if self.backup_gw.process_payment(tx_id, amount, recipient):
                self.repo.record_transaction(tx_id, amount, recipient, "SUCCESS", "BACKUP")
                return "COMPLETED_BACKUP"
        except Exception:
            pass

        # Final Failure Logging
        self.repo.record_transaction(tx_id, amount, recipient, "FAILED", "NONE")
        raise RuntimeError("Payment routing failed across all gateways")