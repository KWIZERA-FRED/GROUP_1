import sqlite3
from payment_router import DatabaseRepository


class SQLiteRepository(DatabaseRepository):
    """Real SQLite-backed implementation of DatabaseRepository.

    tx_id is the PRIMARY KEY, so the database itself enforces
    transaction idempotency at the schema level (not just in application code).
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._create_table()

    def _create_table(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                tx_id TEXT PRIMARY KEY,
                amount REAL NOT NULL,
                recipient TEXT NOT NULL,
                status TEXT NOT NULL,
                gateway TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def get_transaction(self, tx_id: str) -> dict:
        cursor = self.conn.execute(
            "SELECT tx_id, amount, recipient, status, gateway FROM transactions WHERE tx_id = ?",
            (tx_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "tx_id": row[0],
            "amount": row[1],
            "recipient": row[2],
            "status": row[3],
            "gateway": row[4],
        }

    def record_transaction(self, tx_id: str, amount: float, recipient: str, status: str, gateway: str):
        self.conn.execute(
            "INSERT INTO transactions (tx_id, amount, recipient, status, gateway) VALUES (?, ?, ?, ?, ?)",
            (tx_id, amount, recipient, status, gateway),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


class BrokenSQLiteRepository(SQLiteRepository):
    """Deliberately broken repository used ONLY for the 'Mock Lie' proof test.

    It writes to a non-existent table ('tx_history' instead of 'transactions').
    A unit test mocking this class would never notice, because the mock never
    executes real SQL. An integration test using a real SQLite connection
    catches it immediately as an sqlite3.OperationalError.
    """

    def record_transaction(self, tx_id: str, amount: float, recipient: str, status: str, gateway: str):
        self.conn.execute(
            "INSERT INTO tx_history (tx_id, amount, recipient, status, gateway) VALUES (?, ?, ?, ?, ?)",
            (tx_id, amount, recipient, status, gateway),
        )
        self.conn.commit()