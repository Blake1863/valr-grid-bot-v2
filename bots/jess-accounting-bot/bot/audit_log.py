from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    original_file_name TEXT,
                    document_type TEXT,
                    extracted_json TEXT,
                    final_row_json TEXT,
                    workbook_row_number INTEGER,
                    workbook_output_path TEXT,
                    exported INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    undone_at TEXT
                )
                """
            )

    def create_entry(
        self,
        *,
        telegram_user_id: int,
        original_file_name: str,
        document_type: str,
        extracted_json: dict[str, Any],
        final_row_json: dict[str, Any] | None,
        status: str,
        error_message: str | None = None,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO audit_log (
                    created_at, telegram_user_id, original_file_name, document_type,
                    extracted_json, final_row_json, status, error_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    telegram_user_id,
                    original_file_name,
                    document_type,
                    json.dumps(extracted_json),
                    json.dumps(final_row_json) if final_row_json else None,
                    status,
                    error_message,
                ),
            )
            return int(cursor.lastrowid)

    def update_entry(
        self,
        audit_id: int,
        *,
        final_row_json: dict[str, Any] | None = None,
        workbook_row_number: int | None = None,
        workbook_output_path: str | None = None,
        exported: bool | None = None,
        status: str | None = None,
        error_message: str | None = None,
        undone: bool = False,
    ) -> None:
        assignments: list[str] = []
        params: list[Any] = []

        if final_row_json is not None:
            assignments.append("final_row_json = ?")
            params.append(json.dumps(final_row_json))
        if workbook_row_number is not None:
            assignments.append("workbook_row_number = ?")
            params.append(workbook_row_number)
        if workbook_output_path is not None:
            assignments.append("workbook_output_path = ?")
            params.append(workbook_output_path)
        if exported is not None:
            assignments.append("exported = ?")
            params.append(1 if exported else 0)
        if status is not None:
            assignments.append("status = ?")
            params.append(status)
        if error_message is not None:
            assignments.append("error_message = ?")
            params.append(error_message)
        if undone:
            assignments.append("undone_at = ?")
            params.append(datetime.now(UTC).isoformat())

        if not assignments:
            return

        params.append(audit_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE audit_log SET {', '.join(assignments)} WHERE id = ?",
                params,
            )

    def list_recent(self, telegram_user_id: int, limit: int = 5) -> list[sqlite3.Row]:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT * FROM audit_log
                WHERE telegram_user_id = ?
                  AND status IN ('written', 'previewed')
                ORDER BY id DESC
                LIMIT ?
                """,
                (telegram_user_id, limit),
            )
            return list(cursor.fetchall())

    def last_written_entry(self, telegram_user_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT * FROM audit_log
                WHERE telegram_user_id = ?
                  AND status = 'written'
                  AND undone_at IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (telegram_user_id,),
            )
            return cursor.fetchone()

    def get_entry(self, audit_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            cursor = connection.execute("SELECT * FROM audit_log WHERE id = ?", (audit_id,))
            return cursor.fetchone()
